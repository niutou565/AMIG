"""`.env` 文件的读取与原子写入（安装向导与数据库切换共用，仅用标准库）。

设计要点：
- 保留原有注释与键顺序，只更新/追加指定的键；
- 写入前把原文件备份为 `.env.bak`（存在才备份）；
- 先写同目录临时文件，`os.replace` 原子替换，避免写一半导致配置损坏；
- 值包含空白、引号或 `#` 时自动加双引号并转义。
"""
import os
import shutil
import tempfile


def read_env_file(path: str) -> dict:
    """读取 .env 为字典（不展开变量，重复键以后者为准）。"""
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                quote = value[0]
                value = value[1:-1]
                if quote == '"':
                    value = value.replace('\\"', '"').replace("\\\\", "\\")
            result[key] = value
    return result


def _format_value(value: str) -> str:
    value = "" if value is None else str(value)
    if value == "" or any(ch in value for ch in " \t\"'#"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update_env_file(path: str, updates: dict) -> None:
    """原子更新 .env：存在则替换对应行，不存在则追加到文件末尾。

    调用方必须保证 updates 的值不含换行（防止写入额外配置行）。
    """
    for key, value in updates.items():
        if not key or any(ch in str(value) for ch in "\r\n"):
            raise ValueError(f"非法的 .env 配置项: {key!r}")

    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()

    remaining = dict(updates)
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={_format_value(remaining.pop(key))}")
                continue
        new_lines.append(line)
    for key, value in remaining.items():
        new_lines.append(f"{key}={_format_value(value)}")

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")

    fd, tmp_path = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(new_lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
