"""Вход в Яндекс по QR-коду и работа с API приложения «Дом с Алисой» (iot.quasar.yandex.ru).

Этот способ не требует своего OAuth-приложения: пользователь подтверждает вход,
отсканировав QR-код (или открыв ссылку) в приложении Яндекса, а сервер получает
сессию и x-token. Тот же способ использует интеграция YandexStation для Home Assistant.

API неофициальный: формат устройств совпадает с официальным (capabilities/properties),
но Яндекс может его поменять.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import inspect
from typing import Any, Callable

import httpx

from .client import YandexAPIError
from .models import Capability, Device, Household, Property, Room, Scenario, UserInfo

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PASSPORT = "https://passport.yandex.ru"
MOBILEPROXY = "https://mobileproxy.passport.yandex.net"
IOT = "https://iot.quasar.yandex.ru"
# Публичное приложение Яндекса для обмена сессии на x-token (как в YandexStation)
X_CLIENT_ID = "c0ebe342af7d48fbbbfcf2d2eedb8f9e"
X_CLIENT_SECRET = "ad0a908f0aa341a182a37ecd75bc319e"


class QuasarSession:
    """HTTP-сессия Яндекса с cookie, x-token и CSRF-токеном."""

    def __init__(
        self,
        x_token: str | None = None,
        cookies: list[dict[str, str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        on_update: Callable[["QuasarSession"], Any] | None = None,
    ):
        self.http = httpx.AsyncClient(
            headers={"User-Agent": UA}, timeout=20, follow_redirects=True, transport=transport
        )
        for c in cookies or []:
            self.http.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        self.x_token = x_token
        self.csrf_token: str | None = None
        self.on_update = on_update
        self._auth_headers: dict[str, str] | None = None
        self._auth_json: dict[str, Any] | None = None
        self.qr_link: str | None = None
        self._last_ts = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    def export(self) -> dict[str, Any]:
        """Данные для сохранения между перезапусками."""
        return {
            "x_token": self.x_token,
            "cookies": [
                {"name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path}
                for c in self.http.cookies.jar
            ],
        }

    def _yandex_cookie_header(self) -> str:
        return "; ".join(
            f"{c.name}={c.value}" for c in self.http.cookies.jar if c.domain.endswith("yandex.ru")
        )

    # --- вход по QR -----------------------------------------------------------

    async def start_qr(self) -> str:
        """Начать вход: возвращает ссылку, которую нужно открыть/отсканировать в приложении Яндекса."""
        r = await self.http.get(f"{PASSPORT}/pwl-yandex")
        m = re.search(r'__CSRF__ = "([^"]+)', r.text)
        if r.status_code != 200 or not m:
            raise YandexAPIError(f"Яндекс не отдал страницу входа ({r.status_code})")
        self._auth_headers = {"X-CSRF-Token": m[1]}

        r = await self.http.post(
            f"{PASSPORT}/pwl-yandex/api/passport/auth/password/submit",
            json={"retpath": f"{PASSPORT}/"},
            headers=self._auth_headers,
        )
        data = _json(r)
        if r.status_code != 200 or "track_id" not in data:
            raise YandexAPIError(f"Не удалось начать вход: {r.status_code} {r.text[:200]}")
        self._auth_json = data

        r = await self.http.post(
            f"{PASSPORT}/pwl-yandex/api/passport/auth/magic/code",
            data={"location_id": "0", "magic_track_id": data["track_id"], "track_id": ""},
            headers=self._auth_headers,
        )
        link = _json(r).get("link")
        if r.status_code != 200 or not link:
            raise YandexAPIError(f"Яндекс не выдал QR-код: {r.status_code} {r.text[:200]}")
        self.qr_link = link
        return link

    async def check_qr(self) -> bool:
        """True, когда пользователь подтвердил вход; после этого получен x-token."""
        if not self._auth_json or not self._auth_headers:
            raise YandexAPIError("Сначала нужно получить QR-код")
        r = await self.http.post(
            f"{PASSPORT}/pwl-yandex/api/passport/auth/magic/code/status",
            json=self._auth_json,
            headers=self._auth_headers,
        )
        data = _json(r)
        if data.get("state") != "otp_auth_finished":
            return False
        r = await self.http.post(
            f"{PASSPORT}/pwl-yandex/api/passport/sessions/get_session",
            data={"track_id": data.get("trackId", "")},
            headers=self._auth_headers,
        )
        if r.status_code != 200:
            raise YandexAPIError(f"Не удалось получить сессию: {r.status_code}")
        await self._token_by_session()
        return True

    async def _token_by_session(self) -> None:
        r = await self.http.post(
            f"{MOBILEPROXY}/1/bundle/oauth/token_by_sessionid",
            data={"client_id": X_CLIENT_ID, "client_secret": X_CLIENT_SECRET},
            headers={"Ya-Client-Host": "passport.yandex.ru", "Ya-Client-Cookie": self._yandex_cookie_header()},
        )
        token = _json(r).get("access_token")
        if not token:
            raise YandexAPIError(f"Не удалось получить x-token: {r.text[:200]}")
        self.x_token = token
        await self._changed()

    async def refresh_cookies(self) -> bool:
        """Восстановить cookie по x-token (если сессия истекла)."""
        if not self.x_token:
            return False
        r = await self.http.post(
            f"{MOBILEPROXY}/1/bundle/auth/x_token/",
            data={"type": "x-token", "retpath": "https://www.yandex.ru"},
            headers={"Ya-Consumer-Authorization": f"OAuth {self.x_token}"},
        )
        data = _json(r)
        if data.get("status") != "ok":
            log.error("Обновление cookie по x-token не удалось: %s", data)
            return False
        r = await self.http.get(
            f"{data['passport_host']}/auth/session/",
            params={"track_id": data["track_id"]},
            follow_redirects=False,
        )
        ok = "/auth/finish" in r.headers.get("location", "")
        if ok:
            self.csrf_token = None
            await self._changed()
        return ok

    async def _changed(self) -> None:
        if self.on_update:
            res = self.on_update(self)
            if inspect.isawaitable(res):
                await res

    # --- запросы к API ------------------------------------------------------

    async def request(self, method: str, url: str, retry: int = 2, **kwargs: Any) -> dict[str, Any]:
        # не чаще 5 запросов в секунду
        delay = self._last_ts + 0.2 - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_ts = time.time()

        headers = dict(kwargs.pop("headers", {}) or {})
        if method.upper() != "GET":
            if self.csrf_token is None:
                r = await self.http.get("https://yandex.ru/iot")
                m = re.search(r'"csrfToken2":"(.+?)"', r.text)
                if not m:
                    if retry and await self.refresh_cookies():
                        return await self.request(method, url, retry - 1, headers=headers, **kwargs)
                    raise YandexAPIError("Не удалось получить CSRF-токен Яндекса — войдите заново")
                self.csrf_token = m[1]
            headers["x-csrf-token"] = self.csrf_token

        r = await self.http.request(method, url, headers=headers, **kwargs)
        if r.status_code == 200:
            data = _json(r)
            if data.get("status") not in (None, "ok"):
                raise YandexAPIError(f"Ошибка Яндекса: {data.get('message') or data}", payload=data)
            return data
        if retry and r.status_code == 401:
            if await self.refresh_cookies():
                return await self.request(method, url, retry - 1, headers=headers, **kwargs)
        elif retry and r.status_code == 403:
            self.csrf_token = None
            return await self.request(method, url, retry - 1, headers=headers, **kwargs)
        if r.status_code == 401:
            raise YandexAPIError("Сессия Яндекса истекла — войдите заново по QR-коду", status_code=401)
        raise YandexAPIError(f"{url}: ответ {r.status_code} {r.text[:200]}", status_code=r.status_code)


MASK_EN = "0123456789abcdef-"
MASK_RU = "оеаинтсрвлкмдпуяы"
HELPER_PREFIX = "SH "  # служебные сценарии для озвучки, в интерфейсе скрыты
TTS_MAX = 100


def encode_trigger(uid: str) -> str:
    """UID -> фраза из русских букв (Яндекс принимает только такие голосовые триггеры)."""
    return "".join(MASK_RU[MASK_EN.index(ch)] for ch in uid.lower() if ch in MASK_EN)


def helper_scenario(device_id: str, text: str, mode: str) -> dict[str, Any]:
    """Служебный сценарий: голосовой триггер-пустышка + действие на колонке."""
    if mode == "tts":
        capability = {"type": "devices.capabilities.quasar",
                      "state": {"instance": "tts", "value": {"text": text}}}
    else:
        capability = {"type": "devices.capabilities.quasar.server_action",
                      "state": {"instance": "text_action", "value": text}}
    return {
        "name": HELPER_PREFIX + device_id,
        "icon": "home",
        "triggers": [{"trigger": {"type": "scenario.trigger.voice", "value": encode_trigger(device_id)}}],
        "steps": [{
            "type": "scenarios.steps.actions.v2",
            "parameters": {"items": [{
                "id": device_id,
                "type": "step.action.item.device",
                "value": {"id": device_id, "item_type": "device", "capabilities": [capability]},
            }]},
        }],
    }


class QuasarClient:
    """Тот же интерфейс, что у YandexIoTClient: user_info / act / run_scenario."""

    def __init__(self, session: QuasarSession):
        self.s = session
        self.last_raw: dict[str, Any] = {}
        self._scenarios: list[dict[str, Any]] = []
        self._helpers: dict[str, str] = {}  # device_id -> id служебного сценария

    async def close(self) -> None:
        await self.s.close()

    async def user_info(self) -> UserInfo:
        data = await self.s.request("GET", f"{IOT}/m/v3/user/devices")
        self.last_raw = data
        try:
            scen = await self.s.request("GET", f"{IOT}/m/user/scenarios")
            self._scenarios = scen.get("scenarios", []) or []
        except Exception as e:  # сценарии не критичны
            log.warning("Не удалось получить сценарии: %s", e)
        visible = [x for x in self._scenarios if not str(x.get("name", "")).startswith(HELPER_PREFIX)]
        return convert_v3(data, visible)

    async def _helper_id(self, device_id: str) -> str:
        if device_id in self._helpers:
            return self._helpers[device_id]
        if not self._scenarios:
            scen = await self.s.request("GET", f"{IOT}/m/user/scenarios")
            self._scenarios = scen.get("scenarios", []) or []
        trigger = encode_trigger(device_id)
        for sc in self._scenarios:
            trig = (sc.get("triggers") or [{}])[0]
            value = trig.get("value") if isinstance(trig, dict) else None
            if sc.get("name") == HELPER_PREFIX + device_id or value == trigger:
                self._helpers[device_id] = sc["id"]
                return sc["id"]
        created = await self.s.request(
            "POST", f"{IOT}/m/v4/user/scenarios", json=helper_scenario(device_id, "готово", "tts")
        )
        sid = created.get("scenario_id")
        if not sid:
            raise YandexAPIError(f"Не удалось создать служебный сценарий: {created}")
        self._helpers[device_id] = sid
        return sid

    async def speak(self, device_id: str, text: str, mode: str = "tts") -> None:
        """mode="tts" — колонка произносит текст; mode="command" — выполняет его как голосовую команду."""
        text = text.strip()
        if not text:
            raise ValueError("Пустой текст")
        if len(text) > TTS_MAX:
            raise ValueError(f"Текст длиннее {TTS_MAX} символов ({len(text)}) — так Яндекс не принимает")
        sid = await self._helper_id(device_id)
        try:
            await self.s.request("PUT", f"{IOT}/m/v4/user/scenarios/{sid}", json=helper_scenario(device_id, text, mode))
        except YandexAPIError as e:
            if e.status_code != 404:
                raise
            # сценарий удалили в приложении Яндекса — создаём заново
            self._helpers.pop(device_id, None)
            self._scenarios = []
            sid = await self._helper_id(device_id)
            await self.s.request("PUT", f"{IOT}/m/v4/user/scenarios/{sid}", json=helper_scenario(device_id, text, mode))
        await self.s.request("POST", f"{IOT}/m/user/scenarios/{sid}/actions")

    async def act(self, device_id: str, *actions: dict[str, Any]) -> dict[str, Any]:
        data = await self.s.request(
            "POST", f"{IOT}/m/user/devices/{device_id}/actions", json={"actions": list(actions)}
        )
        errors = []
        for dev in data.get("devices", []) or []:
            for cap in dev.get("capabilities", []) or []:
                ar = (cap.get("state") or {}).get("action_result") or {}
                if ar.get("status") == "ERROR":
                    errors.append(f"{cap.get('type')}: {ar.get('error_code')}")
        if errors:
            raise YandexAPIError("Устройство не выполнило команду: " + "; ".join(errors), payload=data)
        return data

    async def run_scenario(self, scenario_id: str) -> dict[str, Any]:
        return await self.s.request("POST", f"{IOT}/m/user/scenarios/{scenario_id}/actions")


def convert_v3(data: dict[str, Any], scenarios: list[dict[str, Any]] | None = None) -> UserInfo:
    """Ответ /m/v3/user/devices -> модель UserInfo (как у официального API)."""
    households, rooms, devices, groups = [], {}, [], []
    for house in data.get("households", []) or []:
        hid = house.get("id") or house.get("name", "home")
        households.append(Household(id=hid, name=house.get("name", "Дом")))
        shared = "sharing_info" in house
        for item in house.get("all", []) or []:
            if item.get("item_type", "device") != "device" or "type" not in item:
                continue
            room_name = item.get("room_name")
            room_id = None
            if room_name:
                room_id = f"{hid}:{room_name}"
                rooms.setdefault(room_id, Room(id=room_id, name=room_name, household_id=hid))
                rooms[room_id].devices.append(item["id"])
            try:
                dev = Device(
                    id=item["id"],
                    name=item.get("name", item["id"]),
                    type=item["type"],
                    household_id=hid,
                    room=room_id,
                    skill_id=item.get("skill_id"),
                    capabilities=_safe_list(Capability, item.get("capabilities"), item),
                    properties=_safe_list(Property, item.get("properties"), item),
                )
            except Exception as e:  # одно странное устройство не должно ломать весь список
                log.warning("Устройство %s пропущено: %s", item.get("name"), e)
                continue
            dev.online = item.get("state", "online")  # type: ignore[attr-defined]
            dev.shared = shared  # type: ignore[attr-defined]
            if di := (item.get("parameters") or {}).get("device_info"):
                dev.device_info = di  # type: ignore[attr-defined]
            if q := item.get("quasar_info"):
                dev.quasar_info = q  # type: ignore[attr-defined]
            devices.append(dev)
    return UserInfo(
        status="ok",
        households=households,
        rooms=list(rooms.values()),
        devices=devices,
        groups=groups,
        scenarios=[
            Scenario(id=s["id"], name=s.get("name", s["id"]), is_active=s.get("is_active", True))
            for s in scenarios or []
            if s.get("id")
        ],
    )


def _safe_list(model: Any, items: Any, device: dict[str, Any]) -> list[Any]:
    """Разбираем умения/свойства по одному: неразобранное пропускаем, остальное оставляем."""
    out = []
    for raw in items or []:
        if not isinstance(raw, dict) or not raw.get("type"):
            continue
        try:
            out.append(model.model_validate(raw))
        except Exception as e:
            log.warning("%s: пропущено %s (%s)", device.get("name"), raw.get("type"), e)
    return out


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
