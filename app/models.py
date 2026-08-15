import hashlib
import uuid
from datetime import datetime, timezone, timedelta
from app.database import get_db


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
                 ip, ua, ua_hash_val, province, city, is_dup=0):
    db = get_db()
    db.execute(
        """INSERT INTO images (filename, thumb_filename, original_size, compressed_size,
           width, height, description, author, content_md5, ip, ua, ua_hash,
           province, city, status, created_at, is_duplicate)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)""",
        (filename, thumb_filename, original_size, compressed_size,
         width, height, description, author, content_md5, ip, ua, ua_hash_val,
         province, city, now_iso(), is_dup)
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_image_by_id(image_id):
    return get_db().execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()


def get_approved_image_by_id(image_id):
    return get_db().execute(
        "SELECT * FROM images WHERE id=? AND status='approved'", (image_id,)
    ).fetchone()


def get_images_by_status(status, limit=24, offset=0, order_by="created_at DESC"):
    return get_db().execute(
        f"SELECT * FROM images WHERE status=? ORDER BY {order_by} LIMIT ? OFFSET ?",
        (status, limit, offset)
    ).fetchall()


def count_images_by_status(status):
    return get_db().execute(
        "SELECT COUNT(*) FROM images WHERE status=?", (status,)
    ).fetchone()[0]


def get_random_approved_images(limit=12):
    """随机获取已通过图片，避免 ORDER BY RANDOM()"""
    db = get_db()
    total = count_images_by_status("approved")
    if total == 0:
        return []
    if total <= limit:
        return db.execute(
            "SELECT * FROM images WHERE status='approved' ORDER BY reviewed_at DESC"
        ).fetchall()
    # 随机种子取数
    import random
    step = max(1, total // limit)
    start = random.randint(0, step - 1)
    return db.execute(
        "SELECT * FROM images WHERE status='approved' LIMIT ? OFFSET ?",
        (limit, start)
    ).fetchall()


def get_latest_approved_images(limit=12):
    return get_db().execute(
        "SELECT * FROM images WHERE status='approved' ORDER BY reviewed_at DESC LIMIT ?",
        (limit,)
    ).fetchall()


def search_approved_images(query, limit=24, offset=0):
    db = get_db()
    q = f"%{query}%"
    return db.execute(
        """SELECT * FROM images WHERE status='approved'
           AND (description LIKE ? OR author LIKE ?)
           ORDER BY reviewed_at DESC LIMIT ? OFFSET ?""",
        (q, q, limit, offset)
    ).fetchall()


def count_search_approved_images(query):
    db = get_db()
    q = f"%{query}%"
    return db.execute(
        """SELECT COUNT(*) FROM images WHERE status='approved'
           AND (description LIKE ? OR author LIKE ?)""",
        (q, q)
    ).fetchone()[0]


def update_image_status(image_id, status, reject_reason=None, admin_id=None,
                        admin_name=None, description=None, author=None):
    db = get_db()
    now = now_iso()
    admin_edited = 0
    if description is not None or author is not None:
        admin_edited = 1
    fields = ["status=?", "reviewed_at=?", "admin_edited=?"]
    params = [status, now, admin_edited]
    if reject_reason is not None:
        fields.append("reject_reason=?")
        params.append(reject_reason)
    if description is not None:
        fields.append("description=?")
        params.append(description)
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


def update_image_description(image_id, description, admin_id=None, admin_name=None):
    """更新图片简介并标记为管理员编辑过"""
    db = get_db()
    db.execute(
        "UPDATE images SET description=?, admin_edited=1 WHERE id=?",
        (description, image_id)
    )
    db.commit()
    if admin_id:
        insert_admin_log(admin_id, admin_name, "edit", "编辑图片简介", f"image#{image_id}")


def delete_image(image_id):
    db = get_db()
    img = db.execute("SELECT filename, thumb_filename FROM images WHERE id=?", (image_id,)).fetchone()
    db.execute("DELETE FROM images WHERE id=?", (image_id,))
    db.commit()
    return img


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
        "UPDATE images SET like_count = MAX(0, like_count - ?) WHERE id=?",
        (count, image_id)
    )
    db.commit()


def check_content_md5_duplicate(content_md5, ip, window_hours=24, window_days=30):
    """检查内容哈希重复"""
    db = get_db()
    # 同一 IP 24小时内
    from datetime import datetime, timezone, timedelta
    cutoff_ip = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    same_ip = db.execute(
        "SELECT id FROM images WHERE content_md5=? AND ip=? AND created_at > ?",
        (content_md5, ip, cutoff_ip)
    ).fetchone()
    if same_ip:
        return True
    # 全站 30天内
    cutoff_site = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
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
    except Exception:
        return False  # 唯一索引冲突，已赞过


def has_liked(image_id, ip, ua_hash_val):
    return get_db().execute(
        "SELECT id FROM likes WHERE image_id=? AND ip=? AND ua_hash=?",
        (image_id, ip, ua_hash_val)
    ).fetchone() is not None


def get_likes_by_image(image_id, limit=20, offset=0):
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
    """批量删除点赞记录"""
    db = get_db()
    id_list = ",".join("?" for _ in like_ids)
    likes = db.execute(
        f"SELECT image_id, COUNT(*) as cnt FROM likes WHERE id IN ({id_list}) GROUP BY image_id",
        like_ids
    ).fetchall()
    db.execute(f"DELETE FROM likes WHERE id IN ({id_list})", like_ids)
    for row in likes:
        decrement_like_count(row["image_id"], row["cnt"])
    db.commit()


def count_likes_by_ip_last_minute(ip):
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    return get_db().execute(
        "SELECT COUNT(*) FROM likes WHERE ip=? AND created_at > ?", (ip, cutoff)
    ).fetchone()[0]


# ========== ban_list ==========

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
    # 检查 CIDR 封禁
    rows = db.execute(
        "SELECT target FROM ban_list WHERE type='cidr' AND is_active=1"
    ).fetchall()
    for r in rows:
        if ip_in_cidr(ip, r["target"]):
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


def remove_ban(ban_id):
    db = get_db()
    db.execute("UPDATE ban_list SET is_active=0 WHERE id=?", (ban_id,))
    db.commit()


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
    db = get_db()
    db.execute("UPDATE admins SET password_hash=? WHERE id=?", (password_hash, admin_id))
    db.commit()


# ========== settings ==========

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=?",
        (key, str(value), str(value))
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
        "INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (date_str,)
    )
    db.commit()


def increment_daily_stat(date_str, field):
    ensure_daily_stats(date_str)
    db = get_db()
    db.execute(f"UPDATE daily_stats SET {field} = {field} + 1 WHERE date=?", (date_str,))
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
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
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
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    return get_db().execute(
        "SELECT COUNT(DISTINCT ip) FROM likes WHERE created_at > ?", (cutoff,)
    ).fetchone()[0]


def get_ban_count():
    return get_db().execute(
        "SELECT COUNT(*) FROM ban_list WHERE is_active=1"
    ).fetchone()[0]


# ========== admin_logs ==========

def insert_admin_log(admin_id, admin_name, action, detail="", target=""):
    db = get_db()
    db.execute(
        "INSERT INTO admin_logs (admin_id, admin_name, action, detail, target, created_at) VALUES (?,?,?,?,?,?)",
        (admin_id, admin_name, action, detail, target, now_iso())
    )
    db.commit()


def get_admin_logs(limit=50, offset=0):
    return get_db().execute(
        "SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()


def count_admin_logs():
    return get_db().execute("SELECT COUNT(*) FROM admin_logs").fetchone()[0]


# ========== view_sessions ==========

def should_count_view(image_id, ip):
    """检查是否10分钟内重复浏览"""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%S")
    db = get_db()
    existing = db.execute(
        "SELECT id FROM view_sessions WHERE image_id=? AND ip=? AND viewed_at > ?",
        (image_id, ip, cutoff)
    ).fetchone()
    if existing:
        return False
    db.execute(
        "INSERT INTO view_sessions (image_id, ip, viewed_at) VALUES (?,?,?)",
        (image_id, ip, now_iso())
    )
    db.commit()
    return True


# ========== cooling ==========

def get_cooldown_multiplier(ip):
    row = get_db().execute(
        "SELECT cooldown_multiplier FROM cooling WHERE ip=?", (ip,)
    ).fetchone()
    return row["cooldown_multiplier"] if row else 1


def increment_reject_cooldown(ip):
    db = get_db()
    now = now_iso()
    row = db.execute("SELECT reject_count, cooldown_multiplier FROM cooling WHERE ip=?", (ip,)).fetchone()
    if row:
        new_count = row["reject_count"] + 1
        new_mult = min(row["cooldown_multiplier"] * 2, 16)
        db.execute(
            "UPDATE cooling SET reject_count=?, cooldown_multiplier=?, updated_at=? WHERE ip=?",
            (new_count, new_mult, now, ip)
        )
    else:
        db.execute(
            "INSERT INTO cooling (ip, reject_count, cooldown_multiplier, updated_at) VALUES (?,1,1,?)",
            (ip, now)
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
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
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
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
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


# ========== 后台搜索 ==========

def search_images_admin(status=None, query=None, limit=24, offset=0, order_by="created_at DESC"):
    db = get_db()
    conditions = []
    params = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if query:
        conditions.append("(description LIKE ? OR author LIKE ? OR ip LIKE ? OR province LIKE ? OR city LIKE ?)")
        q = f"%{query}%"
        params.extend([q, q, q, q, q])
    where = " AND ".join(conditions) if conditions else "1=1"
    return db.execute(
        f"SELECT * FROM images WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()


def count_images_admin(status=None, query=None):
    db = get_db()
    conditions = []
    params = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if query:
        conditions.append("(description LIKE ? OR author LIKE ? OR ip LIKE ? OR province LIKE ? OR city LIKE ?)")
        q = f"%{query}%"
        params.extend([q, q, q, q, q])
    where = " AND ".join(conditions) if conditions else "1=1"
    return db.execute(f"SELECT COUNT(*) FROM images WHERE {where}", params).fetchone()[0]


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
    return get_db().execute(
        """SELECT ip, province, city,
           COUNT(*) as upload_count,
           SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as approved,
           SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
           SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) as rejected,
           MAX(created_at) as last_active
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