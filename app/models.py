import hashlib
import logging
import re
import time
import uuid
import contextvars
from datetime import datetime, timezone, timedelta
from app.database import (
    get_db, transaction, IntegrityError, DatabaseError,
    acquire_named_lock, release_named_lock,
    upsert_suffix, random_unit_expr, least_expr, greatest_expr,
    IS_SQLITE,
)
from app.config import VIEW_DEDUP_WINDOW

_logger = logging.getLogger(__name__)

# 当前请求的管理员 IP（由 admin 路由设置，供日志记录使用）
_current_admin_ip = contextvars.ContextVar("current_admin_ip", default="")


def set_current_admin_request(request):
    """在后台请求上下文中记录当前管理员 IP。"""
    from app.ip_geo import get_client_ip
    _current_admin_ip.set(get_client_ip(request))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_dt(s: str) -> datetime:
    """Parse an ISO datetime string into a timezone-aware UTC datetime.
    Accepts both naive strings (assumed UTC) and aware strings (converted to UTC).
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ua_hash(ua_string: str) -> str:
    return hashlib.sha256(ua_string.encode()).hexdigest()[:16]


def generate_filename() -> str:
    return uuid.uuid4().hex


# ========== images ==========

def insert_image(filename, thumb_filename, original_size, compressed_size,
                 width, height, description, author, content_md5,
                 ip, ua, ua_hash_val, province, city, is_dup=0, title="", region=""):
    db = get_db()
    cursor = db.execute(
        """INSERT INTO images (filename, thumb_filename, original_size, compressed_size,
           width, height, description, title, author, content_md5, ip, ua, ua_hash,
           province, city, region, status, created_at, is_duplicate)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)""",
        (filename, thumb_filename, original_size, compressed_size,
         width, height, description, title, author, content_md5, ip, ua, ua_hash_val,
         province, city, region, now_iso(), is_dup)
    )
    # 必须在 commit 之前取 lastrowid：committed 之后在同一连接上再查询虽然可行，
    # 但多 worker/多连接场景下语义不明确，直接使用 cursor.lastrowid 更可靠。
    new_id = cursor.lastrowid
    db.commit()
    return new_id


def get_image_by_id(image_id):
    return get_db().execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()


def get_approved_image_by_id(image_id):
    return get_db().execute(
        "SELECT * FROM images WHERE id=? AND status='approved'", (image_id,)
    ).fetchone()


# order_by 白名单：防止 SQL 注入（拒绝任意字符串拼接）
_ALLOWED_ORDER_BY = {
    "created_at DESC",
    "created_at ASC",
    "like_count DESC",
    "views DESC",
}

# 分页参数统一夹取上限：避免 page=-1 产生负 OFFSET（SQLite 会当 0 处理，
# 但调用方的 total_pages 计算会出现负数页），以及 limit 过大导致一次扫描整表。
_MAX_PAGE_LIMIT = 200


def _clamp_page(limit, offset, max_limit=_MAX_PAGE_LIMIT):
    """把 limit/offset 收敛到安全范围，非法值回退到默认。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 24
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, max_limit)), max(0, offset)


def get_images_by_status(status, limit=24, offset=0, order_by="created_at DESC"):
    if order_by not in _ALLOWED_ORDER_BY:
        order_by = "created_at DESC"
    limit, offset = _clamp_page(limit, offset)
    return get_db().execute(
        f"SELECT * FROM images WHERE status=? ORDER BY {order_by} LIMIT ? OFFSET ?",
        (status, limit, offset)
    ).fetchall()


def count_images_by_status(status):
    return get_db().execute(
        "SELECT COUNT(*) FROM images WHERE status=?", (status,)
    ).fetchone()[0]


def count_images_total():
    return get_db().execute("SELECT COUNT(*) FROM images").fetchone()[0]


def get_random_approved_images(limit=12):
    """随机获取已通过图片，避免 ORDER BY RAND()"""
    db = get_db()
    total = count_images_by_status("approved")
    if total == 0:
        return []
    if total <= limit:
        return db.execute(
            "SELECT * FROM images WHERE status='approved' ORDER BY reviewed_at DESC"
        ).fetchall()
    # 随机起点覆盖全部可选的连续窗口；旧实现只在首段取样，后面的图片几乎不会出现在首页
    import random
    start = random.randint(0, total - limit)
    return db.execute(
        "SELECT * FROM images WHERE status='approved' LIMIT ? OFFSET ?",
        (limit, start)
    ).fetchall()


def get_latest_approved_images(limit=12):
    return get_db().execute(
        "SELECT * FROM images WHERE status='approved' ORDER BY reviewed_at DESC LIMIT ?",
        (limit,)
    ).fetchall()


def get_random_image_prefer_low_heat(exclude_id=None):
    """随机返回一张已通过图片，权重向热度（点赞数）较低的图片倾斜。

    加权抽样在数据库端完成（仅返回一行），避免把全部图片记录加载到
    Python 内存中。权重 = 1/(like_count+1)，用均匀随机数的倒数做竞速排序。
    随机数表达式由 database.random_unit_expr() 按当前后端生成。
    """
    db = get_db()
    condition = "status='approved'"
    params = []
    if exclude_id is not None:
        condition += " AND id != ?"
        params.append(exclude_id)
    row = db.execute(
        f"""SELECT id FROM images WHERE {condition}
            ORDER BY (COALESCE(like_count, 0) + 1) / {random_unit_expr()}
            ASC LIMIT 1""",
        params
    ).fetchone()
    return row


# 搜索关键词长度上限：与标题长度（100）对齐，避免超长输入构造巨大 LIKE 模式。
MAX_SEARCH_QUERY_LENGTH = 100
# 多关键词搜索的词数上限：避免一次搜索构造过多 LIKE 条件拖慢查询
MAX_SEARCH_TERMS = 5
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_search_query(raw) -> str:
    """规范化搜索关键词：去首尾空白、合并连续空白、截断超长输入。

    用户在搜索框里粘贴带换行/多个空格的文本时，直接拼进 LIKE 模式会
    因为多余空白而匹配不到；统一收敛为单个空格更符合直觉。
    """
    if not raw:
        return ""
    text = _WHITESPACE_RE.sub(" ", str(raw)).strip()
    return text[:MAX_SEARCH_QUERY_LENGTH]


def split_search_terms(q):
    """把规范化后的关键词按空格拆分为检索词，最多取前 5 个。

    单个词时保持原有的“整段子串匹配”；多个词时按 AND 语义处理，
    每个词都必须命中标题/简介/署名之一。
    """
    return [term for term in q.split(" ") if term][:MAX_SEARCH_TERMS]


def _like_pattern(text):
    """把关键词转成 LIKE 模式，并转义 ! % _ 三个通配/转义字符。

    必须配合 SQL 中的 ESCAPE '!' 使用，否则用户输入 "%" 会匹配全部记录。
    使用 "!" 作为转义符而不用反斜杠，是为了避免 MySQL 字符串字面量把
    反斜杠当作转义符（NO_BACKSLASH_ESCAPES 模式下行为还会变化）。
    """
    escaped = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def _multi_term_condition(patterns):
    """多关键词的 AND 条件：每个词都必须命中标题/简介/署名之一。"""
    return " AND ".join(
        "(title LIKE ? ESCAPE '!' OR description LIKE ? ESCAPE '!' "
        "OR author LIKE ? ESCAPE '!')"
        for _ in patterns
    )


def search_approved_images(query, limit=24, offset=0):
    db = get_db()
    limit, offset = _clamp_page(limit, offset)
    q = normalize_search_query(query)
    if not q:
        # 空关键词等价于浏览全部已通过图片（与旧版 LIKE '%%' 的行为保持一致）
        return db.execute(
            """SELECT * FROM images WHERE status='approved'
               ORDER BY reviewed_at DESC, id DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
    terms = split_search_terms(q)
    if len(terms) > 1:
        patterns = [_like_pattern(term) for term in terms]
        # 相关度：标题命中词数多的优先；有署名命中的排在仅简介命中之前
        title_hits = " + ".join(
            "CASE WHEN title LIKE ? ESCAPE '!' THEN 1 ELSE 0 END" for _ in patterns
        )
        author_hit = " OR ".join("author LIKE ? ESCAPE '!'" for _ in patterns)
        sql = (
            f"SELECT * FROM images WHERE status='approved' "
            f"AND {_multi_term_condition(patterns)} "
            f"ORDER BY ({title_hits}) DESC, "
            f"CASE WHEN ({author_hit}) THEN 1 ELSE 0 END DESC, "
            f"reviewed_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params = []
        for pattern in patterns:
            params.extend([pattern, pattern, pattern])
        params.extend(patterns)
        params.extend(patterns)
        params.extend([limit, offset])
        return db.execute(sql, params).fetchall()
    pattern = _like_pattern(q)
    # 排序：标题命中 > 署名命中 > 简介命中；同层内按审核时间倒序，
    # 并以 id 兜底，保证同一审核时间（批量通过）下翻页不会重复或漏行。
    return db.execute(
        """SELECT * FROM images WHERE status='approved'
           AND (title LIKE ? ESCAPE '!' OR description LIKE ? ESCAPE '!'
                OR author LIKE ? ESCAPE '!')
           ORDER BY
             CASE
               WHEN title LIKE ? ESCAPE '!' THEN 3
               WHEN author LIKE ? ESCAPE '!' THEN 2
               ELSE 1
             END DESC,
             reviewed_at DESC, id DESC
           LIMIT ? OFFSET ?""",
        (pattern, pattern, pattern, pattern, pattern, limit, offset)
    ).fetchall()


def count_search_approved_images(query):
    db = get_db()
    q = normalize_search_query(query)
    if not q:
        return count_images_by_status("approved")
    terms = split_search_terms(q)
    if len(terms) > 1:
        patterns = [_like_pattern(term) for term in terms]
        params = []
        for pattern in patterns:
            params.extend([pattern, pattern, pattern])
        return db.execute(
            "SELECT COUNT(*) FROM images WHERE status='approved' "
            f"AND {_multi_term_condition(patterns)}",
            params,
        ).fetchone()[0]
    pattern = _like_pattern(q)
    return db.execute(
        """SELECT COUNT(*) FROM images WHERE status='approved'
           AND (title LIKE ? ESCAPE '!' OR description LIKE ? ESCAPE '!'
                OR author LIKE ? ESCAPE '!')""",
        (pattern, pattern, pattern)
    ).fetchone()[0]


def update_image_status(image_id, status, reject_reason=None, admin_id=None,
                        admin_name=None, description=None, author=None, title=None):
    db = get_db()
    now = now_iso()
    admin_edited = 0
    if description is not None or author is not None or title is not None:
        admin_edited = 1
    fields = ["status=?", "reviewed_at=?", "admin_edited=?"]
    params = [status, now, admin_edited]
    if reject_reason is not None:
        fields.append("reject_reason=?")
        params.append(reject_reason)
    if description is not None:
        fields.append("description=?")
        params.append(description)
    if title is not None:
        fields.append("title=?")
        params.append(title)
    if author is not None:
        fields.append("author=?")
        params.append(author)
    params.append(image_id)
    db.execute(f"UPDATE images SET {', '.join(fields)} WHERE id=?", params)
    db.commit()
    # 记录日志
    if admin_id:
        action = "approve" if status == "approved" else "reject"
        detail = ""
        if reject_reason:
            detail = f"拒绝理由: {reject_reason}"
        if admin_edited:
            detail = (detail + " " if detail else "") + "编辑了内容"
        insert_admin_log(admin_id, admin_name, action, detail, f"image#{image_id}")


def rollback_image_to_pending(image_id):
    """文件移动失败时把审核状态回滚为待审核，避免出现已发布但文件不可用。"""
    db = get_db()
    db.execute(
        "UPDATE images SET status='pending', reviewed_at=NULL WHERE id=?",
        (image_id,),
    )
    db.commit()


def try_approve_image(image_id, admin_id=None, admin_name=None,
                      description=None, author=None, title=None):
    """条件审核通过：仅当图片当前不是 approved 时才更新，返回是否发生状态转换。

    用 SQL 条件更新代替“先查后改”，多个管理员/多个 worker 并发审核同一张图片时，
    只有一个请求会得到 True，从而避免重复移动文件与重复统计。
    """
    db = get_db()
    now = now_iso()
    admin_edited = 1 if (description is not None or author is not None or title is not None) else 0
    fields = ["status=?", "reviewed_at=?", "admin_edited=?"]
    params = ["approved", now, admin_edited]
    if description is not None:
        fields.append("description=?")
        params.append(description)
    if title is not None:
        fields.append("title=?")
        params.append(title)
    if author is not None:
        fields.append("author=?")
        params.append(author)
    params.append(image_id)
    cursor = db.execute(
        f"UPDATE images SET {', '.join(fields)} WHERE id=? AND status != 'approved'",
        params,
    )
    db.commit()
    changed = cursor.rowcount > 0
    if changed and admin_id:
        detail = "编辑了内容" if admin_edited else ""
        insert_admin_log(admin_id, admin_name, "approve", detail, f"image#{image_id}")
    return changed


def update_image_content(image_id, title, description, admin_id=None, admin_name=None,
                         author=None):
    """更新图片标题、简介（可选作者）并标记为管理员编辑过"""
    db = get_db()
    if author is None:
        db.execute(
            "UPDATE images SET title=?, description=?, admin_edited=1 WHERE id=?",
            (title, description, image_id)
        )
    else:
        db.execute(
            "UPDATE images SET title=?, description=?, author=?, admin_edited=1 WHERE id=?",
            (title, description, author, image_id)
        )
    db.commit()
    if admin_id:
        insert_admin_log(admin_id, admin_name, "edit", "编辑图片标题/简介", f"image#{image_id}")


def delete_image(image_id):
    """删除图片记录；返回被删除行的 (filename, thumb_filename) 或 None。

    返回 None 表示记录已不存在（例如被另一个管理员并发删除），
    调用方据此避免重复统计和重复删除文件。
    """
    db = get_db()
    img = db.execute("SELECT filename, thumb_filename FROM images WHERE id=?", (image_id,)).fetchone()
    cursor = db.execute("DELETE FROM images WHERE id=?", (image_id,))
    db.commit()
    return img if cursor.rowcount > 0 else None


def increment_view(image_id):
    db = get_db()
    db.execute("UPDATE images SET views = views + 1 WHERE id=?", (image_id,))
    db.commit()


def increment_like_count(image_id):
    db = get_db()
    db.execute("UPDATE images SET like_count = like_count + 1 WHERE id=?", (image_id,))
    db.commit()


def decrement_like_count(image_id, count=1):
    db = get_db()
    db.execute(
        # SQLite 没有 GREATEST 函数，通过方言助手生成对应表达式
        f"UPDATE images SET like_count = {greatest_expr('0', 'like_count - ?')} WHERE id=?",
        (count, image_id)
    )
    db.commit()


def check_content_md5_duplicate(content_md5, ip, window_hours=24, window_days=30):
    """检查内容哈希重复"""
    db = get_db()
    # 同一 IP 24小时内
    from datetime import datetime, timezone, timedelta
    cutoff_ip = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    same_ip = db.execute(
        "SELECT id FROM images WHERE content_md5=? AND ip=? AND created_at > ?",
        (content_md5, ip, cutoff_ip)
    ).fetchone()
    if same_ip:
        return True
    # 全站 30天内
    cutoff_site = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat(timespec="seconds")
    site_dup = db.execute(
        "SELECT id FROM images WHERE content_md5=? AND created_at > ?",
        (content_md5, cutoff_site)
    ).fetchone()
    return site_dup is not None


# ========== likes ==========

def insert_like(image_id, ip, ua_hash_val, province, city):
    db = get_db()
    now = now_iso()
    try:
        db.execute(
            "INSERT INTO likes (image_id, ip, ua_hash, province, city, created_at) VALUES (?,?,?,?,?,?)",
            (image_id, ip, ua_hash_val, province, city, now)
        )
        db.commit()
        return True
    except IntegrityError:
        # 唯一索引冲突：同一 IP + UA 已赞过。
        # 必须回滚，否则线程本地连接上会残留未结束的写事务并长期持有写锁。
        db.rollback()
        return False


def add_like_atomic(image_id, ip, ua_hash_val, province, city, date_str=None):
    """点赞写库的原子版本：点赞行、图片计数、日统计在同一事务内提交。

    任何一步失败都会整体回滚，避免“有点赞行但计数没增加”之类的漂移。
    唯一索引冲突（重复点赞）时返回 False，不影响连接上的其他操作；
    并发写入出现死锁时由 run_in_transaction_with_retry 整体重试事务。
    """
    from app.database import run_in_transaction_with_retry

    if date_str is None:
        date_str = today_str()

    def _write(conn):
        try:
            conn.execute(
                "INSERT INTO likes (image_id, ip, ua_hash, province, city, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (image_id, ip, ua_hash_val, province, city, now_iso())
            )
        except IntegrityError:
            return False
        conn.execute(
            "UPDATE images SET like_count = like_count + 1 WHERE id=?", (image_id,)
        )
        conn.execute(
            "INSERT INTO daily_stats (date, like_count) VALUES (?, 1) "
            + upsert_suffix(
                "ON DUPLICATE KEY UPDATE like_count = like_count + 1",
                "ON CONFLICT(`date`) DO UPDATE SET like_count = daily_stats.like_count + 1",
            ),
            (date_str,),
        )
        return True

    return run_in_transaction_with_retry(_write)


def has_liked(image_id, ip, ua_hash_val):
    return get_db().execute(
        "SELECT id FROM likes WHERE image_id=? AND ip=? AND ua_hash=?",
        (image_id, ip, ua_hash_val)
    ).fetchone() is not None


def get_likes_by_image(image_id, limit=20, offset=0):
    limit, offset = _clamp_page(limit, offset)
    return get_db().execute(
        "SELECT * FROM likes WHERE image_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (image_id, limit, offset)
    ).fetchall()


def count_likes_by_image(image_id):
    return get_db().execute(
        "SELECT COUNT(*) FROM likes WHERE image_id=?", (image_id,)
    ).fetchone()[0]


def delete_like(like_id):
    db = get_db()
    like = db.execute("SELECT image_id FROM likes WHERE id=?", (like_id,)).fetchone()
    if like:
        db.execute("DELETE FROM likes WHERE id=?", (like_id,))
        decrement_like_count(like["image_id"])
        db.commit()
        return like["image_id"]
    return None


def delete_likes_by_ids(like_ids):
    """批量删除点赞记录，并在同一事务内同步 images.like_count。

    旧实现逐条调用 decrement_like_count（内部 commit），删除与计数更新不是
    一个原子操作；这里改为单事务内完成，避免中途失败造成计数漂移。
    """
    if not like_ids:
        return
    from app.database import run_in_transaction_with_retry

    id_list = ",".join("?" for _ in like_ids)

    def _delete(conn):
        likes = conn.execute(
            f"SELECT image_id, COUNT(*) as cnt FROM likes WHERE id IN ({id_list}) "
            "GROUP BY image_id",
            like_ids
        ).fetchall()
        conn.execute(f"DELETE FROM likes WHERE id IN ({id_list})", like_ids)
        for row in likes:
            conn.execute(
                # SQLite 没有 GREATEST 函数，通过方言助手生成对应表达式
                f"UPDATE images SET like_count = {greatest_expr('0', 'like_count - ?')} WHERE id=?",
                (row["cnt"], row["image_id"]),
            )

    run_in_transaction_with_retry(_delete)


def count_likes_by_ip_last_minute(ip):
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    return get_db().execute(
        "SELECT COUNT(*) FROM likes WHERE ip=? AND created_at > ?", (ip, cutoff)
    ).fetchone()[0]


# ========== ban_list ==========

# CIDR 封禁缓存：避免每次请求都查询并遍历全部 CIDR 规则
_cidr_cache = {"targets": None, "ts": 0}
_CIDR_CACHE_TTL = 60


def _get_active_cidr_targets():
    now = time.time()
    if _cidr_cache["targets"] is None or (now - _cidr_cache["ts"]) > _CIDR_CACHE_TTL:
        rows = get_db().execute(
            "SELECT target FROM ban_list WHERE type='cidr' AND is_active=1"
        ).fetchall()
        _cidr_cache["targets"] = [r["target"] for r in rows]
        _cidr_cache["ts"] = now
    return _cidr_cache["targets"]


def _invalidate_cidr_cache():
    _cidr_cache["targets"] = None


def is_banned(ip, ua_hash_val):
    db = get_db()
    # 检查精确 IP 封禁
    row = db.execute(
        "SELECT id FROM ban_list WHERE target=? AND type='single' AND is_active=1", (ip,)
    ).fetchone()
    if row:
        return True
    # 检查 UA 哈希封禁
    row = db.execute(
        "SELECT id FROM ban_list WHERE target=? AND type='ua' AND is_active=1", (ua_hash_val,)
    ).fetchone()
    if row:
        return True
    # 检查 CIDR 封禁（带缓存）
    for target in _get_active_cidr_targets():
        if ip_in_cidr(ip, target):
            return True
    return False


def ip_in_cidr(ip, cidr):
    import ipaddress
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def get_ban_list():
    return get_db().execute(
        "SELECT * FROM ban_list ORDER BY created_at DESC"
    ).fetchall()


def add_ban(target, ban_type, reason, admin_id):
    db = get_db()
    db.execute(
        "INSERT INTO ban_list (target, type, reason, admin_id, created_at, is_active) VALUES (?,?,?,?,?,1)",
        (target, ban_type, reason, admin_id, now_iso())
    )
    db.commit()
    _invalidate_cidr_cache()


def remove_ban(ban_id):
    db = get_db()
    db.execute("UPDATE ban_list SET is_active=0 WHERE id=?", (ban_id,))
    db.commit()
    _invalidate_cidr_cache()


def get_image_by_any_filename(value):
    """按原图或缩略图相对路径查询图片记录（供受控文件访问使用）。"""
    if not value:
        return None
    return get_db().execute(
        "SELECT id, filename, thumb_filename, status "
        "FROM images WHERE filename=? OR thumb_filename=? LIMIT 1",
        (value, value),
    ).fetchone()


# ========== admins ==========

def get_admin_by_username(username):
    return get_db().execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()


def get_admin_by_id(admin_id):
    return get_db().execute("SELECT * FROM admins WHERE id=?", (admin_id,)).fetchone()


def update_admin_login_attempts(admin_id, attempts, locked_until=None):
    db = get_db()
    db.execute(
        "UPDATE admins SET login_attempts=?, locked_until=? WHERE id=?",
        (attempts, locked_until, admin_id)
    )
    db.commit()


def update_admin_password(admin_id, password_hash):
    """更新密码哈希，同时清除失败统计与旧锁定状态。"""
    db = get_db()
    db.execute(
        "UPDATE admins SET password_hash=?, login_attempts=0, locked_until=NULL WHERE id=?",
        (password_hash, admin_id),
    )
    db.commit()


# ========== settings ==========

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE `key`=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (`key`, value) VALUES (?,?) "
        + upsert_suffix(
            "ON DUPLICATE KEY UPDATE value = VALUES(value)",
            "ON CONFLICT(`key`) DO UPDATE SET value = excluded.value",
        ),
        (key, str(value))
    )
    db.commit()


def get_all_settings():
    rows = get_db().execute("SELECT * FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def get_setting_int(key, default=0):
    val = get_setting(key, str(default))
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ========== pages ==========

def get_visible_pages():
    return get_db().execute(
        "SELECT * FROM pages WHERE is_visible=1 ORDER BY sort_order"
    ).fetchall()


def get_page_by_slug(slug):
    return get_db().execute(
        "SELECT * FROM pages WHERE slug=? AND is_visible=1", (slug,)
    ).fetchone()


def get_page_by_slug_any(slug):
    """按 slug 查询页面（不限制可见状态），供后台校验唯一性使用。"""
    return get_db().execute(
        "SELECT * FROM pages WHERE slug=?", (slug,)
    ).fetchone()


def get_all_pages():
    return get_db().execute("SELECT * FROM pages ORDER BY sort_order").fetchall()


def get_page_by_id(page_id):
    return get_db().execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()


def upsert_page(page_id, slug, title, content, sort_order, is_visible):
    db = get_db()
    now = now_iso()
    if page_id:
        db.execute(
            "UPDATE pages SET slug=?, title=?, content=?, sort_order=?, is_visible=?, updated_at=? WHERE id=?",
            (slug, title, content, sort_order, is_visible, now, page_id)
        )
    else:
        db.execute(
            "INSERT INTO pages (slug, title, content, sort_order, is_visible, updated_at) VALUES (?,?,?,?,?,?)",
            (slug, title, content, sort_order, is_visible, now)
        )
    db.commit()


def delete_page(page_id):
    get_db().execute("DELETE FROM pages WHERE id=?", (page_id,))
    get_db().commit()


# ========== friend_links ==========

def get_visible_links():
    return get_db().execute(
        "SELECT * FROM friend_links WHERE is_visible=1 ORDER BY sort_order"
    ).fetchall()


def get_all_links():
    return get_db().execute("SELECT * FROM friend_links ORDER BY sort_order").fetchall()


def get_link_by_id(link_id):
    return get_db().execute("SELECT * FROM friend_links WHERE id=?", (link_id,)).fetchone()


def upsert_link(link_id, name, url, sort_order, is_visible):
    db = get_db()
    if link_id:
        db.execute(
            "UPDATE friend_links SET name=?, url=?, sort_order=?, is_visible=? WHERE id=?",
            (name, url, sort_order, is_visible, link_id)
        )
    else:
        db.execute(
            "INSERT INTO friend_links (name, url, sort_order, is_visible, created_at) VALUES (?,?,?,?,?)",
            (name, url, sort_order, is_visible, now_iso())
        )
    db.commit()


def delete_link(link_id):
    get_db().execute("DELETE FROM friend_links WHERE id=?", (link_id,))
    get_db().commit()


# ========== daily_stats ==========

def ensure_daily_stats(date_str=None):
    if date_str is None:
        date_str = today_str()
    db = get_db()
    db.execute(
        "INSERT INTO daily_stats (date) VALUES (?) "
        + upsert_suffix(
            "ON DUPLICATE KEY UPDATE date = date",
            "ON CONFLICT(`date`) DO NOTHING",
        ),
        (date_str,),
    )
    db.commit()


# daily_stats 可递增的字段白名单：防止 SQL 注入
_ALLOWED_STAT_FIELDS = {"upload_count", "approved_count", "rejected_count", "like_count", "view_count"}


def increment_daily_stat(date_str, field):
    """原子递增日统计：单条 UPSERT 完成“建当天行 + 加一”，避免两次提交的竞态。"""
    if field not in _ALLOWED_STAT_FIELDS:
        raise ValueError(f"非法统计字段: {field}")
    db = get_db()
    # 字段名来自白名单校验，值用参数传递，不存在 SQL 注入面
    db.execute(
        f"INSERT INTO daily_stats (date, {field}) VALUES (?, 1) "
        + upsert_suffix(
            f"ON DUPLICATE KEY UPDATE {field} = {field} + 1",
            f"ON CONFLICT(`date`) DO UPDATE SET {field} = daily_stats.{field} + 1",
        ),
        (date_str,),
    )
    db.commit()


def get_daily_stats(date_str):
    return get_db().execute(
        "SELECT * FROM daily_stats WHERE date=?", (date_str,)
    ).fetchone()


def get_stats_range(days):
    from datetime import datetime, timezone, timedelta
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return get_db().execute(
        "SELECT * FROM daily_stats WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end)
    ).fetchall()


def get_top_like_provinces(limit=10):
    return get_db().execute(
        "SELECT province, COUNT(*) as cnt FROM likes WHERE province != '' GROUP BY province ORDER BY cnt DESC LIMIT ?",
        (limit,)
    ).fetchall()


def get_top_images_by_likes(limit=10, window_days=None):
    if window_days:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat(timespec="seconds")
        return get_db().execute(
            """SELECT i.*, COUNT(l.id) as window_likes
               FROM images i
               INNER JOIN likes l ON i.id = l.image_id AND l.created_at >= ?
               WHERE i.status='approved'
               GROUP BY i.id
               ORDER BY window_likes DESC, MAX(l.created_at) DESC
               LIMIT ?""",
            (cutoff, limit)
        ).fetchall()
    else:
        return get_db().execute(
            "SELECT * FROM images WHERE status='approved' ORDER BY like_count DESC LIMIT ?",
            (limit,)
        ).fetchall()


def get_active_ip_count(days=7):
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return get_db().execute(
        "SELECT COUNT(DISTINCT ip) FROM likes WHERE created_at > ?", (cutoff,)
    ).fetchone()[0]


def get_ban_count():
    return get_db().execute(
        "SELECT COUNT(*) FROM ban_list WHERE is_active=1"
    ).fetchone()[0]


# ========== admin_logs ==========

def insert_admin_log(admin_id, admin_name, action, detail="", target="", ip=None, location=None):
    db = get_db()
    if ip is None:
        ip = _current_admin_ip.get()
    db.execute(
        "INSERT INTO admin_logs (admin_id, admin_name, action, detail, target, ip, created_at) VALUES (?,?,?,?,?,?,?)",
        (admin_id, admin_name, action, detail, target, ip, now_iso())
    )
    db.commit()


def _admin_log_where(action=None, ip=None, query=None, date_from=None, date_to=None):
    """构造操作日志筛选条件，搜索与计数共用，避免两处条件不一致。"""
    conditions = []
    params = []
    if action:
        conditions.append("action = ?")
        params.append(action)
    ip_text = normalize_search_query(ip)
    if ip_text:
        conditions.append("ip LIKE ? ESCAPE '!'")
        params.append(_like_pattern(ip_text))
    q = normalize_search_query(query)
    if q:
        pattern = _like_pattern(q)
        conditions.append(
            "(detail LIKE ? ESCAPE '!' OR target LIKE ? ESCAPE '!' "
            "OR admin_name LIKE ? ESCAPE '!')"
        )
        params.extend([pattern, pattern, pattern])
    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= ?")
        params.append(date_to)
    return (" AND ".join(conditions) if conditions else "1=1"), params


def search_admin_logs(action=None, ip=None, query=None,
                      date_from=None, date_to=None, limit=50, offset=0):
    """搜索操作日志。

    query 为通用关键词，对 detail / target / admin_name 做部分匹配，
    便于通过图片编号（如 image#123）、操作详情等灵活检索。
    """
    db = get_db()
    where, params = _admin_log_where(action, ip, query, date_from, date_to)
    limit, offset = _clamp_page(limit, offset)
    # 同一秒内的多条日志以 id 倒序兜底，避免翻页时重复或漏行
    return db.execute(
        f"SELECT * FROM admin_logs WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()


def count_admin_logs(action=None, ip=None, query=None,
                     date_from=None, date_to=None):
    db = get_db()
    where, params = _admin_log_where(action, ip, query, date_from, date_to)
    return db.execute(f"SELECT COUNT(*) FROM admin_logs WHERE {where}", params).fetchone()[0]


# ========== view_sessions ==========

def should_count_view(image_id, ip):
    """检查是否在去重窗口内重复浏览。

    先做一次只读判断：窗口内已浏览过则直接返回 False，避免高频重复浏览
    每次都开启写事务（WAL 下每次提交都要落盘）。只有需要计数时才执行
    UPSERT，仍由 (image_id, ip) 唯一索引保证并发请求只有一个成功。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=VIEW_DEDUP_WINDOW)).isoformat(timespec="seconds")
    db = get_db()
    existing = db.execute(
        "SELECT viewed_at FROM view_sessions WHERE image_id=? AND ip=?",
        (image_id, ip),
    ).fetchone()
    if existing and str(existing["viewed_at"]) > cutoff:
        return False
    cursor = db.execute(
        "INSERT INTO view_sessions (image_id, ip, viewed_at) VALUES (?,?,?) "
        + upsert_suffix(
            "ON DUPLICATE KEY UPDATE viewed_at = "
            "IF(viewed_at <= ?, VALUES(viewed_at), viewed_at)",
            # SQLite 用条件 UPDATE：不满足条件时不改行，rowcount 与 MySQL 语义一致
            "ON CONFLICT(image_id, ip) DO UPDATE SET viewed_at = excluded.viewed_at "
            "WHERE view_sessions.viewed_at <= ?",
        ),
        (image_id, ip, now_iso(), cutoff),
    )
    db.commit()
    return cursor.rowcount > 0


# ========== cooling ==========

def get_cooldown_multiplier(ip):
    row = get_db().execute(
        "SELECT cooldown_multiplier FROM cooling WHERE ip=?", (ip,)
    ).fetchone()
    return row["cooldown_multiplier"] if row else 1


def increment_reject_cooldown(ip):
    """拒绝冷却 +1，倍率翻倍（上限 16）。

    使用 SQL 侧 UPSERT 算术，避免“先 SELECT 再 UPDATE”在多 worker 下丢更新。
    """
    db = get_db()
    now = now_iso()
    db.execute(
        "INSERT INTO cooling (ip, reject_count, cooldown_multiplier, updated_at) "
        "VALUES (?, 1, 1, ?) "
        + upsert_suffix(
            "ON DUPLICATE KEY UPDATE "
            "reject_count = reject_count + 1, "
            "cooldown_multiplier = LEAST(cooldown_multiplier * 2, 16), "
            "updated_at = VALUES(updated_at)",
            "ON CONFLICT(ip) DO UPDATE SET "
            "reject_count = cooling.reject_count + 1, "
            f"cooldown_multiplier = {least_expr('cooling.cooldown_multiplier * 2', '16')}, "
            "updated_at = excluded.updated_at",
        ),
        (ip, now),
    )
    db.commit()


def reset_cooldown(ip):
    get_db().execute("DELETE FROM cooling WHERE ip=?", (ip,))
    get_db().commit()


def get_all_cooling():
    return get_db().execute("SELECT * FROM cooling ORDER BY updated_at DESC").fetchall()


# ========== 热度榜 ==========

def get_top_images(window_days, show_count, limit=None, offset=0):
    """获取窗口内热度榜数据"""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat(timespec="seconds")
    db = get_db()
    if limit is None:
        limit = show_count
    rows = db.execute(
        """SELECT i.*, COUNT(l.id) as window_likes, MAX(l.created_at) as last_like_time
           FROM images i
           INNER JOIN likes l ON i.id = l.image_id AND l.created_at >= ?
           WHERE i.status='approved'
           GROUP BY i.id
           ORDER BY window_likes DESC, last_like_time DESC
           LIMIT ? OFFSET ?""",
        (cutoff, limit, offset)
    ).fetchall()
    return rows


def count_top_images(window_days):
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat(timespec="seconds")
    return get_db().execute(
        """SELECT COUNT(DISTINCT i.id)
           FROM images i
           INNER JOIN likes l ON i.id = l.image_id AND l.created_at >= ?
           WHERE i.status='approved'""",
        (cutoff,)
    ).fetchone()[0]


# ========== 上传计数 ==========

def count_uploads_today_by_ip(ip):
    return get_db().execute(
        "SELECT COUNT(*) FROM images WHERE ip=? AND created_at >= ?",
        (ip, today_str())
    ).fetchone()[0]


def count_uploads_today_by_ua(ua_hash_val):
    return get_db().execute(
        "SELECT COUNT(*) FROM images WHERE ua_hash=? AND created_at >= ?",
        (ua_hash_val, today_str())
    ).fetchone()[0]


def count_uploads_today_total():
    return get_db().execute(
        "SELECT COUNT(*) FROM images WHERE created_at >= ?",
        (today_str(),)
    ).fetchone()[0]


def get_last_upload_time(ip):
    row = get_db().execute(
        "SELECT created_at FROM images WHERE ip=? ORDER BY created_at DESC LIMIT 1",
        (ip,)
    ).fetchone()
    return row["created_at"] if row else None


# ========== 上传额度原子预约 ==========

# 额度预约的保留时长（秒）：上传处理通常在数秒内结束，
# 超过该时长的预约视为异常残留，在下次预约时惰性清理。
UPLOAD_RESERVATION_TTL_SECONDS = 600


class _UploadLimitDenied(Exception):
    """限流拒绝信号，携带面向用户的提示文案。"""


def reserve_upload_quota(ip, ua_hash_val):
    """原子地检查并占用上传额度（检查与占用在同一互斥区内完成）。

    使用命名锁串行化并发预约（MySQL 为 GET_LOCK，SQLite 为进程内互斥锁）：
    同一时刻只有一个连接能执行检查+插入，后续请求在锁上排队，锁释放后一定
    能读到先前请求占用的额度，因此高并发下也不会超额通过限流检查。

    预约成功后调用方必须在上传流程结束时（无论成败）调用
    release_upload_reservation：成功时图片已入库计入额度，失败时归还额度。

    返回 (allowed, error_message, reservation_id)。
    """
    from app.config import (
        DEFAULT_UPLOAD_PER_IP, DEFAULT_UPLOAD_PER_UA,
        DEFAULT_UPLOAD_DAILY_TOTAL, DEFAULT_UPLOAD_INTERVAL,
        MYSQL_QUOTA_LOCK_TIMEOUT,
    )

    # 后台正在切换数据库（或切换完成等待重启）时拒绝新的上传，
    # 避免数据只写入即将被替换的旧库
    from app.db_migration import switch_blocking_writes
    if switch_blocking_writes():
        return (False, "系统正在切换数据库，请稍后再试", None)

    conn = get_db()
    if not acquire_named_lock(conn, "amig_upload_quota", MYSQL_QUOTA_LOCK_TIMEOUT):
        _logger.warning("获取上传额度串行化锁超时，拒绝本次上传")
        return (False, "系统繁忙，请稍后再试", None)

    try:
        # 惰性清理过期预约，避免异常残留长期占用额度
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=UPLOAD_RESERVATION_TTL_SECONDS)).isoformat()
        conn.execute("DELETE FROM upload_reservations WHERE created_at < ?", (cutoff,))

        per_ip = get_setting_int("upload_limit_per_ip", DEFAULT_UPLOAD_PER_IP)
        per_ua = get_setting_int("upload_limit_per_ua", DEFAULT_UPLOAD_PER_UA)
        daily_total = get_setting_int("upload_limit_daily_total", DEFAULT_UPLOAD_DAILY_TOTAL)
        interval = get_setting_int("upload_interval_seconds", DEFAULT_UPLOAD_INTERVAL)

        today = today_str()

        # 全站上限：已入库图片 + 未完成入库的预约
        total = (
            conn.execute("SELECT COUNT(*) FROM images WHERE created_at >= ?", (today,)).fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM upload_reservations WHERE created_at >= ?", (today,)).fetchone()[0]
        )
        if total >= daily_total:
            raise _UploadLimitDenied("今日上传名额已满，请明日再来")

        # IP 上限
        ip_count = (
            conn.execute(
                "SELECT COUNT(*) FROM images WHERE ip=? AND created_at >= ?", (ip, today)
            ).fetchone()[0]
            + conn.execute(
                "SELECT COUNT(*) FROM upload_reservations WHERE ip=? AND created_at >= ?", (ip, today)
            ).fetchone()[0]
        )
        if ip_count >= per_ip:
            raise _UploadLimitDenied(f"今日上传次数已达上限（{per_ip}次）")

        # UA 上限
        if per_ua > 0:
            ua_count = (
                conn.execute(
                    "SELECT COUNT(*) FROM images WHERE ua_hash=? AND created_at >= ?", (ua_hash_val, today)
                ).fetchone()[0]
                + conn.execute(
                    "SELECT COUNT(*) FROM upload_reservations WHERE ua_hash=? AND created_at >= ?", (ua_hash_val, today)
                ).fetchone()[0]
            )
            if ua_count >= per_ua:
                raise _UploadLimitDenied("今日上传次数已达上限")

        # 上传间隔：已入库图片与进行中的预约都视为最近活动
        mult = get_cooldown_multiplier(ip)
        effective_interval = interval * mult
        last_times = []
        row = conn.execute(
            "SELECT created_at FROM images WHERE ip=? ORDER BY created_at DESC LIMIT 1", (ip,)
        ).fetchone()
        if row:
            last_times.append(row[0])
        row = conn.execute(
            "SELECT MAX(created_at) FROM upload_reservations WHERE ip=?", (ip,)
        ).fetchone()
        if row and row[0]:
            last_times.append(row[0])
        if last_times:
            last_dt = max(parse_dt(t) for t in last_times)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if elapsed < effective_interval:
                remaining = int(effective_interval - elapsed)
                if mult > 1:
                    raise _UploadLimitDenied(f"操作过于频繁，请 {remaining} 秒后再试（冷却中）")
                raise _UploadLimitDenied(f"操作过于频繁，请 {remaining} 秒后再试")

        cur = conn.execute(
            "INSERT INTO upload_reservations (ip, ua_hash, created_at) VALUES (?, ?, ?)",
            (ip, ua_hash_val, now_iso()),
        )
        conn.commit()
        return (True, "", cur.lastrowid)
    except _UploadLimitDenied as exc:
        conn.rollback()
        return (False, str(exc), None)
    except DatabaseError:
        _logger.exception("上传额度预约失败")
        try:
            conn.rollback()
        except DatabaseError:
            pass
        return (False, "系统繁忙，请稍后再试", None)
    finally:
        release_named_lock(conn, "amig_upload_quota")


def release_upload_reservation(reservation_id):
    """释放上传额度预约（图片已入库或上传失败后调用）。"""
    if not reservation_id:
        return
    try:
        conn = get_db()
        conn.execute("DELETE FROM upload_reservations WHERE id=?", (reservation_id,))
        conn.commit()
    except DatabaseError:
        _logger.warning("释放上传额度预约失败 id=%s", reservation_id, exc_info=True)


# ========== 后台搜索 ==========

def search_images_admin(status=None, query=None, limit=24, offset=0, order_by="created_at DESC"):
    if order_by not in _ALLOWED_ORDER_BY:
        order_by = "created_at DESC"
    limit, offset = _clamp_page(limit, offset)
    db = get_db()
    conditions = []
    params = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    cond, cond_params = _admin_image_search_condition(query)
    if cond:
        conditions.append(cond)
        params.extend(cond_params)
    where = " AND ".join(conditions) if conditions else "1=1"
    # 以 id 兜底排序，保证同秒创建的多张图片翻页时顺序稳定
    return db.execute(
        f"SELECT * FROM images WHERE {where} ORDER BY {order_by}, id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()


def count_images_admin(status=None, query=None):
    db = get_db()
    conditions = []
    params = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    cond, cond_params = _admin_image_search_condition(query)
    if cond:
        conditions.append(cond)
        params.extend(cond_params)
    where = " AND ".join(conditions) if conditions else "1=1"
    return db.execute(f"SELECT COUNT(*) FROM images WHERE {where}", params).fetchone()[0]


# 后台图片搜索字段：标题/简介/署名/上传 IP/归属地
_ADMIN_IMAGE_SEARCH_COLUMNS = ("title", "description", "author", "ip", "province", "city")


def _admin_image_search_condition(query):
    """构造后台图片搜索的 WHERE 片段，搜索与计数共用，避免两处条件不一致。"""
    q = normalize_search_query(query)
    if not q:
        return None, []
    pattern = _like_pattern(q)
    condition = "(" + " OR ".join(
        f"{col} LIKE ? ESCAPE '!'" for col in _ADMIN_IMAGE_SEARCH_COLUMNS
    ) + ")"
    return condition, [pattern] * len(_ADMIN_IMAGE_SEARCH_COLUMNS)


def batch_update_status(ids, status, admin_id, admin_name):
    db = get_db()
    now = now_iso()
    id_list = ",".join("?" for _ in ids)
    db.execute(
        f"UPDATE images SET status=?, reviewed_at=? WHERE id IN ({id_list})",
        [status, now] + ids
    )
    db.commit()
    insert_admin_log(admin_id, admin_name, f"batch_{status}", f"批量操作 {len(ids)} 张图片", str(ids))


def get_recent_upload_ips(limit=50):
    """获取最近上传 IP 的统计，点赞数通过子查询一并返回，避免 N+1 查询"""
    # ANY_VALUE 是 MySQL 8 专属函数；SQLite 不启用 ONLY_FULL_GROUP_BY，
    # 可直接选取非聚合列，两种后端行为一致。
    if IS_SQLITE:
        province_sel, city_sel = "province as province", "city as city"
    else:
        province_sel, city_sel = (
            "ANY_VALUE(province) as province",
            "ANY_VALUE(city) as city",
        )
    return get_db().execute(
        f"""SELECT ip,
           {province_sel},
           {city_sel},
           COUNT(*) as upload_count,
           SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as approved,
           SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
           SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) as rejected,
           MAX(created_at) as last_active,
           (SELECT COUNT(*) FROM likes l WHERE l.ip = images.ip) as like_count
           FROM images
           GROUP BY ip
           ORDER BY last_active DESC
           LIMIT ?""",
        (limit,)
    ).fetchall()


def get_ip_like_count(ip):
    return get_db().execute(
        "SELECT COUNT(*) FROM likes WHERE ip=?", (ip,)
    ).fetchone()[0]