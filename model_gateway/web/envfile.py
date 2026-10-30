"""读写本机 .env。只给网页入口用，库本身不会自动加载这个文件。"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path) -> None:
    """把文件里的变量放进当前进程。已经存在的环境变量优先。"""
    if not path.is_file():
        return
    for key, value in parse_env(path.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """按变量名替换。值为空字符串时删掉这一行，其余行原样保留。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen: set[str] = set()
    rewritten: list[str] = []
    for line in lines:
        key = line_key(line)
        if key is None or key not in updates:
            rewritten.append(line)
            continue
        seen.add(key)
        value = updates[key]
        if value == "":
            continue
        prefix = "export " if line.strip().startswith("export ") else ""
        rewritten.append(f"{prefix}{key}={quote_env(value)}")
    for key, value in updates.items():
        if key in seen or value == "":
            continue
        rewritten.append(f"{key}={quote_env(value)}")
    text = "\n".join(rewritten)
    if text:
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key = line_key(line)
        if key is None:
            continue
        raw = line.strip()
        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()
        values[key] = unquote_env(raw.split("=", 1)[1].strip())
    return values


def line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    body = stripped[len("export ") :].strip() if stripped.startswith("export ") else stripped
    if "=" not in body:
        return None
    key = body.split("=", 1)[0].strip()
    return key or None


def quote_env(value: str) -> str:
    if value == "" or any(char in value for char in " \t#'\""):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def unquote_env(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value
