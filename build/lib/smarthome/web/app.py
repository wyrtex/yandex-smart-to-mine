"""HTTP API + WebSocket + веб-интерфейс (его же показывает iOS-приложение)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..adapters.yandex import YandexAdapter
from ..config import Settings
from ..core.hub import Hub, action_from_key
from ..core.scenarios import ScenarioEngine
from ..storage import Storage

STATIC = Path(__file__).parent / "static"
COOKIE = "sh_session"
PUBLIC_PATHS = {"/api/login", "/api/ping"}


def _session_token(password: str) -> str:
    return hmac.new(password.encode(), b"smarthome-session", hashlib.sha256).hexdigest()


def _authorized(settings: Settings, cookies: dict[str, str], headers: Any, query: Any) -> bool:
    if not settings.password:
        return True
    expected = _session_token(settings.password)
    if hmac.compare_digest(cookies.get(COOKIE, ""), expected):
        return True
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], settings.password):
        return True
    return hmac.compare_digest(query.get("token", ""), settings.password)


class NoCacheStatic(StaticFiles):
    """Интерфейс всегда проверяется на обновление — приложение не нужно переустанавливать."""

    async def get_response(self, path, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(
    settings: Settings | None = None,
    storage: Storage | None = None,
    adapters: list[Any] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    storage = storage or Storage(settings.data_dir / "smarthome.db")
    hub = Hub(storage)

    if adapters is None:
        adapters = [YandexAdapter(settings, storage)]
        if settings.mqtt_host:
            from ..adapters.zigbee2mqtt import Zigbee2MQTTAdapter

            adapters.append(Zigbee2MQTTAdapter(settings))
    for a in adapters:
        hub.add_adapter(a)
    engine = ScenarioEngine(hub, settings.timezone)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        await engine.start()
        hub.log("core", f"Сервер запущен, версия {__version__}")
        prune = asyncio.create_task(_prune_loop(storage))
        try:
            yield
        finally:
            prune.cancel()
            await engine.stop()
            await hub.stop()

    app = FastAPI(title="Умный дом", version=__version__, lifespan=lifespan)
    app.state.hub = hub
    app.state.engine = engine
    app.state.settings = settings

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC_PATHS:
            if not _authorized(settings, request.cookies, request.headers, request.query_params):
                return JSONResponse({"detail": "Нужен вход"}, status_code=401)
        return await call_next(request)

    def yandex() -> YandexAdapter:
        a = hub.adapters.get("yandex")
        if not isinstance(a, YandexAdapter):
            raise HTTPException(400, "Адаптер Яндекса не подключён")
        return a

    # --- служебное ----------------------------------------------------------

    @app.get("/api/ping")
    async def ping():
        return {"ok": True, "version": __version__, "auth_required": bool(settings.password)}

    @app.post("/api/login")
    async def login(body: dict = Body(...)):
        if not settings.password:
            return {"ok": True}
        if not hmac.compare_digest(str(body.get("password", "")), settings.password):
            raise HTTPException(401, "Неверный пароль")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, _session_token(settings.password), max_age=10 * 365 * 86400,
                        httponly=True, samesite="lax")
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/status")
    async def status():
        return {
            "version": __version__,
            "time": time.time(),
            "timezone": settings.timezone,
            "devices": len(hub.devices),
            "adapters": {name: a.status() for name, a in hub.adapters.items()},
        }

    @app.get("/api/log")
    async def get_log(limit: int = 200):
        return storage.recent_log(limit)

    # --- устройства ---------------------------------------------------------

    @app.get("/api/devices")
    async def devices():
        return {
            "rooms": hub.rooms,
            "devices": [hub.to_dict(d) for d in hub.devices.values()],
        }

    def _device(device_id: str):
        dev = hub.find(device_id)
        if not dev:
            raise HTTPException(404, "Устройство не найдено")
        return dev

    @app.get("/api/devices/{device_id}")
    async def device(device_id: str):
        return hub.to_dict(_device(device_id))

    @app.post("/api/devices/{device_id}/action")
    async def device_action(device_id: str, body: dict = Body(...)):
        """{"set": {"on_off.on": true, "range.brightness": 50}} или {"actions": [...формат Яндекса]}"""
        dev = _device(device_id)
        try:
            actions = list(body.get("actions") or [])
            actions += [action_from_key(k, v) for k, v in (body.get("set") or {}).items()]
            if not actions:
                raise ValueError("Нет действий")
            await hub.execute(dev.id, actions)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e))
        return hub.to_dict(dev)

    @app.get("/api/devices/{device_id}/history")
    async def device_history(device_id: str, key: str | None = None, hours: float = 24):
        dev = _device(device_id)
        return storage.history(dev.id, key, since=time.time() - hours * 3600)

    # --- сценарии -----------------------------------------------------------

    def _sc_dict(sc) -> dict[str, Any]:
        d = sc.model_dump(by_alias=True, exclude_none=True)
        d["running"] = engine.is_running(sc.id)
        d["last_run"] = engine.last_run.get(sc.id)
        return d

    @app.get("/api/scenarios")
    async def scenarios():
        return [_sc_dict(s) for s in engine.scenarios.values()]

    @app.post("/api/scenarios")
    async def save_scenario(body: dict = Body(...)):
        try:
            sc = engine.save(body)
        except Exception as e:
            raise HTTPException(400, str(e))
        hub.log("scenarios", f"Сохранён сценарий «{sc.name}»")
        return _sc_dict(sc)

    @app.delete("/api/scenarios/{scenario_id}")
    async def delete_scenario(scenario_id: str):
        engine.delete(scenario_id)
        return {"ok": True}

    @app.post("/api/scenarios/{scenario_id}/run")
    async def run_scenario(scenario_id: str):
        sc = engine.scenarios.get(scenario_id)
        if not sc:
            raise HTTPException(404, "Сценарий не найден")
        engine.fire(sc, reason="запуск вручную", force=True)
        return {"ok": True}

    @app.post("/api/scenarios/{scenario_id}/stop")
    async def stop_scenario(scenario_id: str):
        engine.cancel(scenario_id)
        return {"ok": True}

    # --- Яндекс -------------------------------------------------------------

    @app.get("/api/yandex/scenarios")
    async def yandex_scenarios():
        return hub.extra.get("yandex_scenarios", [])

    @app.post("/api/yandex/scenarios/{scenario_id}/run")
    async def yandex_run(scenario_id: str):
        try:
            await yandex().run_scenario(scenario_id)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/api/yandex/login-url")
    async def yandex_login_url():
        try:
            return {"url": yandex().login_url()}
        except RuntimeError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/yandex/code")
    async def yandex_code(body: dict = Body(...)):
        try:
            await yandex().login_with_code(str(body.get("code", "")))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Яндекс не принял код: {e}")
        hub.log("yandex", "Выполнен вход в аккаунт Яндекса")
        return {"ok": True}

    @app.post("/api/yandex/logout")
    async def yandex_logout():
        yandex().logout()
        hub.log("yandex", "Выполнен выход из аккаунта Яндекса")
        return {"ok": True}

    # --- Zigbee -------------------------------------------------------------

    @app.post("/api/zigbee/permit-join")
    async def permit_join(seconds: int = 254):
        z = hub.adapters.get("zigbee")
        if not z:
            raise HTTPException(400, "Zigbee2MQTT не настроен (MQTT_HOST)")
        try:
            await z.permit_join(seconds)  # type: ignore[attr-defined]
        except Exception as e:
            raise HTTPException(400, str(e))
        hub.log("zigbee", f"Сопряжение новых устройств открыто на {seconds} с")
        return {"ok": True}

    # --- события в реальном времени ----------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        if not _authorized(settings, websocket.cookies, websocket.headers, websocket.query_params):
            await websocket.close(code=4401)
            return
        q = hub.subscribe()
        try:
            await websocket.send_json({"type": "hello", "version": __version__})
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    ev = {"type": "ping"}
                await websocket.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unsubscribe(q)

    # --- интерфейс ----------------------------------------------------------

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", NoCacheStatic(directory=STATIC), name="static")
    return app


async def _prune_loop(storage: Storage) -> None:
    while True:
        storage.prune_history(90)
        await asyncio.sleep(86400)
