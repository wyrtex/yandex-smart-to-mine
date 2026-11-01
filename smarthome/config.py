"""Настройки из переменных окружения и файла .env."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Минимальный загрузчик .env: KEY=VALUE, без перезаписи уже заданных переменных."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def yandex_token() -> str:
    load_dotenv()
    return os.environ.get("YANDEX_IOT_TOKEN", "")
