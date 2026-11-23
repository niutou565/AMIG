# -*- coding: utf-8 -*-
"""统一的服务端密码策略。

规则目标：
- 最低 12 个字符，避免过短的弱密码；
- 最大 72 个 UTF-8 字节，与 bcrypt 的输入限制一致，绝不静默截断；
- 拒绝全空格、单一重复字符、明显的重复片段；
- 拦截常见弱口令 / 泄露口令；
- 不强制特殊字符组合，避免用复杂度规则替代实际强度。
"""

MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_BYTES = 72

# 精简的常见弱口令集合（统一小写比较）；不引入外部字典文件或新依赖。
COMMON_WEAK_PASSWORDS = frozenset({
    "password", "password1", "password12", "password123", "password1234",
    "passw0rd", "p@ssword", "p@ssw0rd", "admin", "admin123", "admin1234",
    "administrator", "root", "root123", "qwerty", "qwerty123", "qwertyuiop",
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "12345678901", "123456789012", "111111111111", "000000000000",
    "abc123", "abc123456", "iloveyou", "letmein", "welcome", "welcome123",
    "monkey", "dragon", "sunshine", "princess", "football", "baseball",
    "master", "shadow", "superman", "trustno1", "changeme", "whatever",
    "amig", "amig123", "amig1234", "amigadmin", "test1234", "testtest",
})


def _is_repeated_pattern(password: str) -> bool:
    """检测整串由同一片段重复构成，例如 aaaaaaaa / abcabcabcabc / 12341234。"""
    length = len(password)
    if length < 4:
        return False
    for size in range(1, min(8, length // 2) + 1):
        if length % size == 0 and password == password[:size] * (length // size):
            return True
    return False


def validate_password(password: str) -> str:
    """校验密码是否满足策略；合法返回空字符串，否则返回给用户看的错误提示。"""
    if not isinstance(password, str) or not password:
        return "密码不能为空"
    if not password.strip():
        return "密码不能全部为空格"
    if len(password) < MIN_PASSWORD_CHARS:
        return f"密码长度不能少于 {MIN_PASSWORD_CHARS} 个字符"

    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return f"密码不能超过 {MAX_PASSWORD_BYTES} 个字节（bcrypt 输入限制）"

    normalized = password.strip().lower()
    if normalized in COMMON_WEAK_PASSWORDS:
        return "密码过于常见，请更换更强的密码"
    if len(set(password)) == 1:
        return "密码不能由同一个字符重复组成"
    if _is_repeated_pattern(password):
        return "密码重复模式过于简单，请更换更强的密码"
    return ""
