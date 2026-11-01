"""Настройки из переменных окружения и файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # OAuth-приложение Яндекса (oauth.yandex.ru): нужно, чтобы войти в аккаунт из интерфейса
    yandex_client_id: str = ""
    yandex_client_secret: str = ""
    # Готовый токен (необязательно, если вход выполнен через интерфейс)
    yandex_token: str = ""
    yandex_poll_interval: float = 10.0

    data_dir: Path = field(default_factory=lambda: Path("data"))
    host: str = "0.0.0.0"
    port: int = 8000
    # Пароль на веб-интерфейс и API. Пусто — без пароля (только для домашней сети!)
    password: str = ""

    # Zigbee2MQTT (пусто — адаптер выключен)
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    z2m_base_topic: str = "zigbee2mqtt"

    timezone: str = "Europe/Moscow"

    @classmethod
    def from_env(cls, dotenv: str | Path = ".env") -> "Settings":
        load_dotenv(dotenv)
        return cls(
            yandex_client_id=_env("YANDEX_CLIENT_ID"),
            yandex_client_secret=_env("YANDEX_CLIENT_SECRET"),
            yandex_token=_env("YANDEX_IOT_TOKEN"),
            yandex_poll_interval=float(_env("YANDEX_POLL_INTERVAL", "10")),
            data_dir=Path(_env("SMARTHOME_DATA_DIR", "data")),
            host=_env("SMARTHOME_HOST", "0.0.0.0"),
            port=int(_env("SMARTHOME_PORT", "8000")),
            password=_env("SMARTHOME_PASSWORD"),
            mqtt_host=_env("MQTT_HOST"),
            mqtt_port=int(_env("MQTT_PORT", "1883")),
            mqtt_user=_env("MQTT_USER"),
            mqtt_password=_env("MQTT_PASSWORD"),
            z2m_base_topic=_env("Z2M_BASE_TOPIC", "zigbee2mqtt"),
            timezone=_env("TZ", "Europe/Moscow"),
        )


def yandex_token() -> str:
    """Токен для CLI: из окружения или из сохранённого входа через интерфейс."""
    s = Settings.from_env()
    if s.yandex_token:
        return s.yandex_token
    from .storage import Storage

    tok = Storage(s.data_dir / "smarthome.db").get_json("yandex_oauth") or {}
    return tok.get("access_token", "")
