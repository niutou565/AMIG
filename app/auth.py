"""后台认证与 Session 管理"""
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from app.models import get_admin_by_username, update_admin_login_attempts, get_admin_by_id
from app.config import SECRET_KEY, LOGIN_MAX_ATTEMPTS, LOGIN_LOCK_MINUTES

# 简单的内存 Session 存储 {session_id: {admin_id, username, expires_at}}
_sessions = {}


def hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_session(admin_id: int, username: str) -> str:
    session_id = secrets.token_hex(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    _sessions[session_id] = {
        "admin_id": admin_id,
        "username": username,
        "expires_at": expires
    }
    # 清理过期 session
    _cleanup_sessions()
    return session_id


def get_session(session_id: str):
    _cleanup_sessions()
    session = _sessions.get(session_id)
    if session and session["expires_at"] > datetime.now(timezone.utc):
        return session
    return None


def destroy_session(session_id: str):
    _sessions.pop(session_id, None)


def _cleanup_sessions():
    now = datetime.now(timezone.utc)
    expired = [k for k, v in _sessions.items() if v["expires_at"] <= now]
    for k in expired:
        del _sessions[k]


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