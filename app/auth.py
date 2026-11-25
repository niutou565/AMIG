"""后台认证与 Session 管理（第二阶段安全加固）

安全要点：
- 密码使用 bcrypt，输入超过 72 字节直接拒绝，绝不静默截断；
- Session 使用服务端存储，支持绝对有效期与空闲超时；
- 每个 Session 绑定独立的 CSRF 令牌；
- 登录成功轮换 Session/Cookie，退出、改密可真正撤销服务端会话；
- 登录失败限流存放在数据库，多 worker 共享；不再使用全局账户锁定，
  避免攻击者用多个 IP 把管理员锁死。
"""
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta

from app.models import (
    get_admin_by_username, update_admin_login_attempts, get_admin_by_id,
    now_iso, parse_dt,
)
from app.database import get_db, upsert_suffix
from app.config import (
    LOGIN_IP_MAX_ATTEMPTS, LOGIN_IP_WINDOW_MINUTES,
    SESSION_TTL_HOURS, SESSION_IDLE_MINUTES,
)
from app.password_policy import MAX_PASSWORD_BYTES

# 过期 Session 清理间隔（秒），避免每次 get_session 都执行 DELETE 写操作
SESSION_CLEANUP_INTERVAL = 600
_last_session_cleanup = 0.0
_session_cleanup_lock = threading.Lock()

# 最近活动时间写入节流：同一会话 60 秒内最多写一次，降低数据库写压力
_SESSION_TOUCH_INTERVAL = 60
_session_touch = {}  # session_id -> monotonic time
_session_touch_lock = threading.Lock()

# 未命中用户名时也执行一次 bcrypt，避免通过响应时间枚举管理员账号
_DUMMY_PASSWORD_HASH = None


def hash_password(password: str) -> str:
    import bcrypt
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError("密码超过 bcrypt 支持的 72 字节上限")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode())
    except (ValueError, TypeError):
        # 数据库中哈希格式异常或密码超长时按验证失败处理，避免登录接口 500
        return False


def _get_dummy_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password("amig-dummy-password-for-timing")
    return _DUMMY_PASSWORD_HASH


# ========== Session ==========

def _delete_session_row(session_id: str):
    db = get_db()
    db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    db.commit()
    with _session_touch_lock:
        _session_touch.pop(session_id, None)


def create_session(admin_id: int, username: str) -> str:
    """创建会话并持久化到数据库（多 worker 进程间共享）。"""
    session_id = secrets.token_hex(32)
    csrf_token = secrets.token_hex(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=SESSION_TTL_HOURS)
    db = get_db()
    db.execute(
        "INSERT INTO sessions "
        "(session_id, admin_id, username, created_at, expires_at, last_seen_at, csrf_token) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            session_id, admin_id, username, now_iso(),
            expires.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            csrf_token,
        ),
    )
    db.commit()
    return session_id


def get_session(session_id: str):
    """读取会话；过期、空闲超时或不存在时返回 None。

    旧数据库中的会话可能缺少 last_seen_at / csrf_token，这里会惰性补齐，
    保证升级后已登录的管理员不会立刻失效。
    """
    _maybe_cleanup_sessions()
    session_id = str(session_id or "")
    if not session_id:
        return None

    db = get_db()
    row = db.execute(
        "SELECT admin_id, username, created_at, expires_at, last_seen_at, csrf_token "
        "FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if not row:
        return None

    now = datetime.now(timezone.utc)
    try:
        expires_at = parse_dt(row["expires_at"])
    except (ValueError, TypeError):
        _delete_session_row(session_id)
        return None
    if expires_at <= now:
        _delete_session_row(session_id)
        return None

    last_seen_raw = row["last_seen_at"] or row["created_at"]
    try:
        last_seen = parse_dt(last_seen_raw)
    except (ValueError, TypeError):
        last_seen = None
    idle_cutoff = now - timedelta(minutes=SESSION_IDLE_MINUTES)
    if last_seen is None or last_seen < idle_cutoff:
        _delete_session_row(session_id)
        return None

    csrf_token = row["csrf_token"] or ""
    if not csrf_token:
        # 旧库迁移：多个 worker 可能同时为同一会话生成令牌。使用条件更新，
        # 只有第一个写入生效，随后统一重新读取数据库中的最终值，避免返回
        # 一个已被其他 worker 覆盖的令牌导致表单校验随机失败。
        candidate = secrets.token_hex(32)
        db.execute(
            "UPDATE sessions SET csrf_token=? "
            "WHERE session_id=? AND (csrf_token='' OR csrf_token IS NULL)",
            (candidate, session_id),
        )
        db.commit()
        refreshed = db.execute(
            "SELECT csrf_token FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not refreshed:
            return None
        csrf_token = refreshed["csrf_token"] or candidate

    mono_now = time.monotonic()
    with _session_touch_lock:
        last_touch = _session_touch.get(session_id, 0.0)
        touch_due = mono_now - last_touch >= _SESSION_TOUCH_INTERVAL
        if touch_due:
            _session_touch[session_id] = mono_now
        # 防止长期运行时该字典随历史会话无限增长
        if len(_session_touch) > 5000:
            for key in list(_session_touch)[: len(_session_touch) - 5000]:
                _session_touch.pop(key, None)

    if touch_due:
        db.execute(
            "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
            (now.isoformat(timespec="seconds"), session_id),
        )
        db.commit()

    return {
        "admin_id": row["admin_id"],
        "username": row["username"],
        "csrf_token": csrf_token,
    }


def destroy_session(session_id: str):
    if not session_id:
        return
    _delete_session_row(session_id)


def revoke_admin_sessions(admin_id: int):
    """撤销指定管理员的全部会话。"""
    db = get_db()
    db.execute("DELETE FROM sessions WHERE admin_id=?", (admin_id,))
    db.commit()


def rotate_session(admin_id: int, username: str) -> str:
    """撤销该管理员的全部旧会话，并为当前设备签发新会话。"""
    revoke_admin_sessions(admin_id)
    return create_session(admin_id, username)


def _cleanup_sessions():
    now = datetime.now(timezone.utc)
    idle_cutoff = now - timedelta(minutes=SESSION_IDLE_MINUTES)
    db = get_db()
    # 老版本会话的 last_seen_at 为空串：与 get_session 的取值规则保持一致，
    # 回退用 created_at 判断空闲超时，避免升级后旧会话被误清理。
    db.execute(
        "DELETE FROM sessions WHERE expires_at <= ? "
        "OR COALESCE(NULLIF(last_seen_at, ''), created_at) <= ?",
        (
            now.isoformat(timespec="seconds"),
            idle_cutoff.isoformat(timespec="seconds"),
        ),
    )
    db.commit()


def _maybe_cleanup_sessions(force: bool = False):
    """按 SESSION_CLEANUP_INTERVAL 节流清理过期/空闲会话。"""
    global _last_session_cleanup
    now = time.time()
    if not force and now - _last_session_cleanup < SESSION_CLEANUP_INTERVAL:
        return
    with _session_cleanup_lock:
        if not force and now - _last_session_cleanup < SESSION_CLEANUP_INTERVAL:
            return
        _cleanup_sessions()
        _last_session_cleanup = time.time()


# ========== 登录失败限流（数据库共享） ==========

def _rate_key(ip: str) -> str:
    return f"ip:{ip}"


def _rate_window_start(now: datetime) -> datetime:
    return now - timedelta(minutes=LOGIN_IP_WINDOW_MINUTES)


def is_login_ip_blocked(ip: str) -> bool:
    """只读判断该 IP 是否已在窗口内达到失败上限，不产生计数。"""
    if not ip:
        return False
    now = datetime.now(timezone.utc)
    cutoff = _rate_window_start(now).isoformat(timespec="seconds")
    row = get_db().execute(
        "SELECT attempts, window_start FROM login_rate_limits WHERE `key`=?",
        (_rate_key(ip),),
    ).fetchone()
    if not row:
        return False
    try:
        window_start = parse_dt(row["window_start"])
    except (ValueError, TypeError):
        return False
    if window_start < _rate_window_start(now):
        return False
    return int(row["attempts"]) >= LOGIN_IP_MAX_ATTEMPTS


def record_login_failure(ip: str) -> bool:
    """记录一次真实密码校验失败；返回该 IP 是否已达到上限。

    计数在 SQL 侧通过 UPSERT + CASE 完成：窗口内累加、窗口外重置为 1，
    避免多 worker “先 SELECT 再 UPDATE” 造成的失败次数丢失。
    """
    if not ip:
        return False
    now = datetime.now(timezone.utc)
    now_iso_value = now.isoformat(timespec="seconds")
    cutoff = _rate_window_start(now).isoformat(timespec="seconds")
    key = _rate_key(ip)
    db = get_db()
    # 顺手清理过期记录，避免表无限增长
    db.execute("DELETE FROM login_rate_limits WHERE window_start <= ?", (cutoff,))
    db.execute(
        "INSERT INTO login_rate_limits (`key`, attempts, window_start, updated_at) "
        "VALUES (?, 1, ?, ?) "
        + upsert_suffix(
            "ON DUPLICATE KEY UPDATE "
            "attempts = CASE WHEN login_rate_limits.window_start >= ? "
            "          THEN login_rate_limits.attempts + 1 ELSE 1 END, "
            "window_start = CASE WHEN login_rate_limits.window_start >= ? "
            "          THEN login_rate_limits.window_start "
            "          ELSE VALUES(window_start) END, "
            "updated_at = VALUES(updated_at)",
            "ON CONFLICT(`key`) DO UPDATE SET "
            "attempts = CASE WHEN login_rate_limits.window_start >= ? "
            "          THEN login_rate_limits.attempts + 1 ELSE 1 END, "
            "window_start = CASE WHEN login_rate_limits.window_start >= ? "
            "          THEN login_rate_limits.window_start "
            "          ELSE excluded.window_start END, "
            "updated_at = excluded.updated_at",
        ),
        (key, now_iso_value, now_iso_value, cutoff, cutoff),
    )
    db.commit()
    row = db.execute(
        "SELECT attempts FROM login_rate_limits WHERE `key`=?", (key,)
    ).fetchone()
    attempts = int(row["attempts"]) if row else 1
    return attempts >= LOGIN_IP_MAX_ATTEMPTS


def reset_login_ip_limit(ip: str) -> None:
    """登录成功后清除该 IP 的失败计数。"""
    if not ip:
        return
    db = get_db()
    db.execute("DELETE FROM login_rate_limits WHERE `key`=?", (_rate_key(ip),))
    db.commit()


def clear_login_rate_limits() -> None:
    """清空登录失败限流表（测试/运维用）。"""
    db = get_db()
    db.execute("DELETE FROM login_rate_limits")
    db.commit()


# ========== 登录 ==========

def login(username: str, password: str) -> tuple:
    """校验用户名与密码。

    返回 (success, session_id_or_error, admin_obj)。
    第二阶段不再根据全站失败次数锁定账户，避免多 IP 攻击者锁死管理员；
    来源 IP 的失败限流由登录路由在调用本函数前后执行。
    """
    admin = get_admin_by_username(username)
    if not admin:
        # 未知用户名也执行一次 bcrypt，避免响应时间暴露账号是否存在
        verify_password(password, _get_dummy_hash())
        return (False, "用户名或密码错误", None)

    if not verify_password(password, admin["password_hash"]):
        attempts = int(admin["login_attempts"] or 0) + 1
        # 仅保留失败次数用于统计/日志，不再写入全局 locked_until
        update_admin_login_attempts(admin["id"], attempts, None)
        return (False, "用户名或密码错误", None)

    # 登录成功，清零账户失败统计
    update_admin_login_attempts(admin["id"], 0, None)
    session_id = create_session(admin["id"], admin["username"])
    return (True, session_id, admin)


def get_current_admin(request):
    """从请求中获取当前登录的管理员，并把会话信息挂到 request.state。"""
    session_id = request.cookies.get("admin_session")
    if not session_id:
        return None
    session = get_session(session_id)
    if not session:
        return None
    admin = get_admin_by_id(session["admin_id"])
    if not admin:
        # 管理员被删除后，会话立即失效
        destroy_session(session_id)
        return None
    request.state.admin_session_id = session_id
    request.state.admin_csrf_token = session["csrf_token"]
    return admin
