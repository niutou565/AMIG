"""后台认证与 Session 管理"""
import hashlib
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from app.models import get_admin_by_username, update_admin_login_attempts, get_admin_by_id, now_iso
from app.database import get_db
from app.config import LOGIN_MAX_ATTEMPTS, LOGIN_LOCK_MINUTES

# 会话有效期（小时）
SESSION_TTL_HOURS = 24

# 过期 Session 清理间隔（秒），避免每次 get_session 都执行 DELETE 写操作
SESSION_CLEANUP_INTERVAL = 600
_last_session_cleanup = 0.0
_session_cleanup_lock = threading.Lock()

# 登录 IP 级限流：同一 IP 在窗口内尝试次数受限，低于账户锁定阈值，
# 防止攻击者反复错误密码将管理员账户锁定（可用性 DoS）。
_LOGIN_IP_MAX_ATTEMPTS = 3
_LOGIN_IP_WINDOW_SECONDS = 900  # 15 分钟
_LOGIN_IP_CLEANUP_INTERVAL = 300  # 每 5 分钟清理一次过期 IP 记录
_login_ip_lock = threading.Lock()
_login_ip_attempts = {}  # ip -> [count, window_start]
_last_login_ip_cleanup = 0.0


def _cleanup_login_ip_attempts(now=None):
    """清理已过窗口期的 IP 限流记录，防止字典无限增长。调用方需持有锁。"""
    global _last_login_ip_cleanup
    if now is None:
        now = time.time()
    expired = [
        ip for ip, entry in _login_ip_attempts.items()
        if now - entry[1] >= _LOGIN_IP_WINDOW_SECONDS
    ]
    for ip in expired:
        _login_ip_attempts.pop(ip, None)
    _last_login_ip_cleanup = now


def check_login_ip_limit(ip: str) -> bool:
    """返回是否允许该 IP 继续尝试登录。多 worker 下各自独立计数，作为缓解已足够。"""
    if not ip:
        return True
    now = time.time()
    with _login_ip_lock:
        if now - _last_login_ip_cleanup >= _LOGIN_IP_CLEANUP_INTERVAL:
            _cleanup_login_ip_attempts(now)
        entry = _login_ip_attempts.get(ip)
        if entry and now - entry[1] < _LOGIN_IP_WINDOW_SECONDS:
            if entry[0] >= _LOGIN_IP_MAX_ATTEMPTS:
                return False
            entry[0] += 1
        else:
            _login_ip_attempts[ip] = [1, now]
        return True


def hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_session(admin_id: int, username: str) -> str:
    """创建会话并持久化到数据库（多 worker 进程间共享）。"""
    session_id = secrets.token_hex(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=SESSION_TTL_HOURS)
    db = get_db()
    db.execute(
        "INSERT INTO sessions (session_id, admin_id, username, created_at, expires_at) VALUES (?,?,?,?,?)",
        (session_id, admin_id, username, now_iso(), expires.isoformat(timespec="seconds"))
    )
    db.commit()
    return session_id


def get_session(session_id: str):
    """从数据库读取会话，过期或不存在返回 None。

    过期清理按固定间隔节流执行，避免每次请求都产生一次 DELETE 写操作。
    """
    _maybe_cleanup_sessions()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = get_db().execute(
        "SELECT admin_id, username FROM sessions WHERE session_id=? AND expires_at > ?",
        (session_id, now)
    ).fetchone()
    if row:
        return {"admin_id": row["admin_id"], "username": row["username"]}
    return None


def destroy_session(session_id: str):
    db = get_db()
    db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    db.commit()


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
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = get_db()
    db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    db.commit()


def _maybe_cleanup_sessions(force: bool = False):
    """按 SESSION_CLEANUP_INTERVAL 节流清理过期会话。"""
    global _last_session_cleanup
    now = time.time()
    if not force and now - _last_session_cleanup < SESSION_CLEANUP_INTERVAL:
        return
    with _session_cleanup_lock:
        if not force and now - _last_session_cleanup < SESSION_CLEANUP_INTERVAL:
            return
        _cleanup_sessions()
        _last_session_cleanup = time.time()


def login(username: str, password: str) -> tuple:
    """
    返回 (success, session_id_or_error, admin_obj)
    """
    admin = get_admin_by_username(username)
    if not admin:
        return (False, "用户名或密码错误", None)

    # 检查锁定
    if admin["locked_until"]:
        locked_until = datetime.fromisoformat(admin["locked_until"])
        if locked_until > datetime.now(timezone.utc):
            remaining = int((locked_until - datetime.now(timezone.utc)).total_seconds() / 60) + 1
            return (False, f"账户已锁定，请 {remaining} 分钟后重试", None)

    if not verify_password(password, admin["password_hash"]):
        attempts = admin["login_attempts"] + 1
        locked_until = None
        if attempts >= LOGIN_MAX_ATTEMPTS:
            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=LOGIN_LOCK_MINUTES)).isoformat()
        update_admin_login_attempts(admin["id"], attempts, locked_until)
        if locked_until:
            return (False, f"登录失败次数过多，已锁定 {LOGIN_LOCK_MINUTES} 分钟", None)
        return (False, "用户名或密码错误", None)

    # 登录成功，重置计数
    update_admin_login_attempts(admin["id"], 0, None)
    session_id = create_session(admin["id"], admin["username"])
    return (True, session_id, admin)


def get_current_admin(request):
    """从请求中获取当前登录的管理员"""
    session_id = request.cookies.get("admin_session")
    if not session_id:
        return None
    session = get_session(session_id)
    if not session:
        return None
    admin = get_admin_by_id(session["admin_id"])
    return admin