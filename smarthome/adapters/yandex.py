"""Адаптер облака Яндекса: опрос состояний, команды, сценарии Яндекса.

Два способа входа:
  * QR-код (рекомендуется): сессия приложения «Дом с Алисой», API iot.quasar.yandex.ru;
  * OAuth-токен своего приложения (официальный API api.iot.yandex.net).
Если есть оба, используется вход по QR-коду.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..config import Settings
from ..core.hub import Action, Hub
from ..storage import Storage
from ..yandex import oauth
from ..yandex.client import YandexAPIError, YandexIoTClient
from ..yandex.models import Device
from ..yandex.quasar import QuasarClient, QuasarSession

TOKEN_KEY = "yandex_oauth"
QUASAR_KEY = "yandex_quasar"
QR_TIMEOUT = 300  # секунд на подтверждение QR-кода


class YandexAdapter:
    name = "yandex"

    def __init__(self, settings: Settings, storage: Storage, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.storage = storage
        self._transport = transport
        self._hub: Hub | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._client: YandexIoTClient | QuasarClient | None = None
        self._client_key = ""
        self._qr: QuasarSession | None = None
        self._qr_started = 0.0
        self.last_ok: float | None = None
        self.last_error: str | None = None

    # --- учётные данные -----------------------------------------------------

    def token_info(self) -> dict[str, Any]:
        if self.settings.yandex_token:
            return {"access_token": self.settings.yandex_token, "source": "env"}
        return self.storage.get_json(TOKEN_KEY) or {}

    def quasar_info(self) -> dict[str, Any]:
        return self.storage.get_json(QUASAR_KEY) or {}

    @property
    def method(self) -> str | None:
        if self.quasar_info().get("x_token"):
            return "qr"
        if self.token_info().get("access_token"):
            return "oauth"
        return None

    @property
    def logged_in(self) -> bool:
        return self.method is not None

    def _save_quasar(self, session: QuasarSession) -> None:
        self.storage.set_json(QUASAR_KEY, session.export())

    # --- вход по QR-коду ----------------------------------------------------

    async def qr_start(self) -> str:
        if self._qr:
            await self._qr.close()
        self._qr = QuasarSession(transport=self._transport)
        self._qr_started = time.time()
        return await self._qr.start_qr()

    @property
    def qr_link(self) -> str | None:
        return self._qr.qr_link if self._qr else None

    async def qr_check(self) -> dict[str, Any]:
        if not self._qr:
            return {"done": False, "expired": True}
        if time.time() - self._qr_started > QR_TIMEOUT:
            await self._qr.close()
            self._qr = None
            return {"done": False, "expired": True}
        if not await self._qr.check_qr():
            return {"done": False}
        session, self._qr = self._qr, None
        self._save_quasar(session)
        await session.close()
        self.last_error = None
        if self._hub:
            self._hub.log(self.name, "Выполнен вход в Яндекс по QR-коду")
        self.poll_soon(0)
        return {"done": True}

    # --- вход по OAuth ------------------------------------------------------

    def login_url(self, response_type: str = "code") -> str:
        if not self.settings.yandex_client_id:
            raise RuntimeError("Не задан YANDEX_CLIENT_ID")
        return oauth.authorize_url(self.settings.yandex_client_id, response_type)

    async def login_with_code(self, code: str) -> None:
        """Принимает код подтверждения или сразу готовый OAuth-токен."""
        code = code.strip()
        if oauth.looks_like_token(code):
            async with YandexIoTClient(code, transport=self._transport) as client:
                await client.user_info_raw()
            tok = {"access_token": code, "refresh_token": None, "expires_at": time.time() + 365 * 86400}
        else:
            tok = await oauth.exchange_code(
                code, self.settings.yandex_client_id, self.settings.yandex_client_secret
            )
        self.storage.set_json(TOKEN_KEY, tok)
        self.last_error = None
        self.poll_soon(0)

    async def check_client(self) -> tuple[bool, str]:
        if not (self.settings.yandex_client_id and self.settings.yandex_client_secret):
            return False, "На сервере не заданы YANDEX_CLIENT_ID / YANDEX_CLIENT_SECRET"
        return await oauth.check_client(
            self.settings.yandex_client_id, self.settings.yandex_client_secret, self._transport
        )

    def logout(self) -> None:
        self.storage.delete(TOKEN_KEY)
        self.storage.delete(QUASAR_KEY)
        self.last_error = None
        self.last_ok = None
        self._client_key = "logout"  # клиент пересоздастся при следующем входе
        if self._hub:
            self._hub.sync_source(self.name, [])

    async def _maybe_refresh(self) -> None:
        tok = self.storage.get_json(TOKEN_KEY)
        if not tok or not tok.get("refresh_token"):
            return
        if tok.get("expires_at", 0) - time.time() > 7 * 86400:
            return
        try:
            new = await oauth.refresh(
                tok["refresh_token"], self.settings.yandex_client_id, self.settings.yandex_client_secret
            )
            self.storage.set_json(TOKEN_KEY, new)
            if self._hub:
                self._hub.log(self.name, "OAuth-токен Яндекса обновлён")
        except Exception as e:
            if self._hub:
                self._hub.log(self.name, f"Не удалось обновить токен: {e}", "warning")

    async def _get_client(self) -> YandexIoTClient | QuasarClient | None:
        method = self.method
        if method == "qr":
            q = self.quasar_info()
            key = "qr:" + q["x_token"]
        elif method == "oauth":
            key = "oauth:" + self.token_info()["access_token"]
        else:
            return None
        if self._client is None or key != self._client_key:
            if self._client:
                await self._client.close()
            if method == "qr":
                self._client = QuasarClient(QuasarSession(
                    x_token=q["x_token"], cookies=q.get("cookies"),
                    transport=self._transport, on_update=self._save_quasar,
                ))
            else:
                self._client = YandexIoTClient(key.split(":", 1)[1], transport=self._transport)
            self._client_key = key
        return self._client

    # --- жизненный цикл -----------------------------------------------------

    async def start(self, hub: Hub) -> None:
        self._hub = hub
        self._task = asyncio.create_task(self._loop(), name="yandex-poll")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for c in (self._client, self._qr):
            if c:
                await c.close()

    def poll_soon(self, delay: float = 1.5) -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._wake.set)

    async def _loop(self) -> None:
        backoff = self.settings.yandex_poll_interval
        last_refresh_check = 0.0
        while True:
            try:
                if time.time() - last_refresh_check > 3600:
                    last_refresh_check = time.time()
                    await self._maybe_refresh()
                await self.poll()
                backoff = self.settings.yandex_poll_interval
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = str(e) or type(e).__name__
                if msg != self.last_error and self._hub:
                    self._hub.log(self.name, f"Ошибка опроса: {msg}", "error")
                self.last_error = msg
                backoff = min(backoff * 2, 300)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass

    async def poll(self) -> None:
        client = await self._get_client()
        if not client or not self._hub:
            return
        info = await client.user_info()
        rooms = {r.id: r.name for r in info.rooms}
        self._hub.sync_source(self.name, list(info.devices), rooms)
        self._hub.extra["yandex_scenarios"] = [s.model_dump() for s in info.scenarios]
        self._hub.extra["yandex_groups"] = [g.model_dump() for g in info.groups]
        self._hub.extra["households"] = [h.model_dump() for h in info.households]
        if self.last_error and self._hub:
            self._hub.log(self.name, "Связь с Яндексом восстановлена")
        if self.last_ok is None and self._hub:
            self._hub.log(self.name, f"Загружено устройств из Яндекса: {len(info.devices)}")
        self.last_ok = time.time()
        self.last_error = None

    def raw(self) -> dict[str, Any]:
        """Последний сырой ответ Яндекса (для отладки формата)."""
        return getattr(self._client, "last_raw", {}) or {}

    # --- команды ------------------------------------------------------------

    async def execute(self, device: Device, actions: list[Action]) -> None:
        client = await self._get_client()
        if not client:
            raise YandexAPIError("Не выполнен вход в аккаунт Яндекса")
        await client.act(device.id, *actions)
        self.poll_soon()

    async def run_scenario(self, scenario_id: str) -> None:
        client = await self._get_client()
        if not client:
            raise YandexAPIError("Не выполнен вход в аккаунт Яндекса")
        await client.run_scenario(scenario_id)
        self.poll_soon(2)

    def status(self) -> dict[str, Any]:
        tok = self.token_info()
        return {
            "logged_in": self.logged_in,
            "method": self.method,
            "token_source": tok.get("source", "oauth") if tok else None,
            "token_expires_at": tok.get("expires_at") if self.method == "oauth" else None,
            "can_login": bool(self.settings.yandex_client_id and self.settings.yandex_client_secret),
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "poll_interval": self.settings.yandex_poll_interval,
            "client_id": self.settings.yandex_client_id,
            "login_url": self.login_url("code") if self.settings.yandex_client_id else None,
            "token_url": self.login_url("token") if self.settings.yandex_client_id else None,
        }
