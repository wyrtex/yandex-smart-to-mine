"""Адаптер облака Яндекса: опрос состояний, команды, сценарии Яндекса, OAuth-токен."""

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

TOKEN_KEY = "yandex_oauth"


class YandexAdapter:
    name = "yandex"

    def __init__(self, settings: Settings, storage: Storage, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.storage = storage
        self._transport = transport
        self._hub: Hub | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._client: YandexIoTClient | None = None
        self._client_token = ""
        self.last_ok: float | None = None
        self.last_error: str | None = None

    # --- токен --------------------------------------------------------------

    def token_info(self) -> dict[str, Any]:
        if self.settings.yandex_token:
            return {"access_token": self.settings.yandex_token, "source": "env"}
        return self.storage.get_json(TOKEN_KEY) or {}

    @property
    def logged_in(self) -> bool:
        return bool(self.token_info().get("access_token"))

    def login_url(self) -> str:
        if not self.settings.yandex_client_id:
            raise RuntimeError("Не задан YANDEX_CLIENT_ID")
        return oauth.authorize_url(self.settings.yandex_client_id)

    async def login_with_code(self, code: str) -> None:
        tok = await oauth.exchange_code(
            code, self.settings.yandex_client_id, self.settings.yandex_client_secret
        )
        self.storage.set_json(TOKEN_KEY, tok)
        self.last_error = None
        self.poll_soon(0)

    def logout(self) -> None:
        self.storage.delete(TOKEN_KEY)
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
        except Exception as e:  # токен ещё действует — попробуем позже
            if self._hub:
                self._hub.log(self.name, f"Не удалось обновить токен: {e}", "warning")

    async def _get_client(self) -> YandexIoTClient | None:
        token = self.token_info().get("access_token", "")
        if not token:
            return None
        if self._client is None or token != self._client_token:
            if self._client:
                await self._client.close()
            self._client = YandexIoTClient(token, transport=self._transport)
            self._client_token = token
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
        if self._client:
            await self._client.close()

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
        self.last_ok = time.time()
        self.last_error = None

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
            "logged_in": bool(tok.get("access_token")),
            "token_source": tok.get("source", "oauth") if tok else None,
            "token_expires_at": tok.get("expires_at"),
            "can_login": bool(self.settings.yandex_client_id and self.settings.yandex_client_secret),
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "poll_interval": self.settings.yandex_poll_interval,
        }
