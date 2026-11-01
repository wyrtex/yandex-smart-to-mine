import json
import time

import httpx
from fastapi.testclient import TestClient

from smarthome.adapters.yandex import YandexAdapter
from smarthome.config import Settings
from smarthome.storage import Storage
from smarthome.web.app import create_app
from smarthome.yandex.quasar import convert_v3, encode_trigger

V3 = {
    "status": "ok",
    "updates_url": "wss://example",
    "households": [{
        "id": "h1", "name": "Мой дом",
        "all": [
            {"id": "lamp", "name": "Лампа", "type": "devices.types.light", "item_type": "device",
             "room_name": "Спальня", "state": "online",
             "parameters": {"device_info": {"manufacturer": "Yandex", "model": "YNDX-00501"}},
             "capabilities": [
                 {"type": "devices.capabilities.on_off", "retrievable": True, "reportable": True,
                  "parameters": {}, "state": {"instance": "on", "value": True}},
                 {"type": "devices.capabilities.range", "retrievable": True,
                  "parameters": {"instance": "brightness", "range": {"min": 1, "max": 100, "precision": 1}},
                  "state": {"instance": "brightness", "value": 40}}],
             "properties": []},
            {"id": "door", "name": "Дверь", "type": "devices.types.sensor.open", "item_type": "device",
             "room_name": "Прихожая", "state": "online", "capabilities": [],
             "properties": [{"type": "devices.properties.event", "retrievable": True,
                             "parameters": {"instance": "open", "events": [{"value": "opened"}, {"value": "closed"}]},
                             "state": {"instance": "open", "value": "closed"},
                             "last_updated": "2026-10-31T19:32:02Z"}]},
            {"id": "weird", "name": "Странное", "type": "devices.types.other", "item_type": "device",
             "capabilities": [{"type": "devices.capabilities.on_off", "retrievable": "не bool",
                               "parameters": {}, "state": {"instance": "on", "value": True}},
                              {"type": "devices.capabilities.toggle", "parameters": {"instance": "mute"}}],
             "properties": []},
            {"id": "st-1", "name": "Станция", "type": "devices.types.smart_speaker.yandex.station_2",
             "item_type": "device", "room_name": "Спальня", "state": "online",
             "quasar_info": {"device_id": "Q1", "platform": "yandexstation_2"},
             "capabilities": [], "properties": []},
            {"id": "grp", "name": "Группа", "item_type": "group", "type": "devices.types.light"},
        ],
    }],
}


def test_convert_v3():
    info = convert_v3(V3, [{"id": "s1", "name": "Утро"}])
    assert [d.id for d in info.devices] == ["lamp", "door", "weird", "st-1"]
    assert info.device("door").property("open").last_updated == 1793475122.0
    # битое умение пропущено, остальные на месте
    assert [c.short_type for c in info.device("weird").capabilities] == ["toggle"]
    lamp = info.device("lamp")
    assert info.room_name(lamp.room) == "Спальня"
    assert lamp.capability("range", "brightness").state.value == 40
    assert lamp.device_info["model"] == "YNDX-00501"
    assert info.device("door").property("open").state.value == "closed"
    assert info.scenarios[0].name == "Утро"


class FakeYandex:
    """Имитация passport + iot.quasar для полного цикла входа по QR."""

    def __init__(self):
        self.confirmed = False
        self.session_valid = True
        self.actions = []
        self.csrf_seen = []
        self.created = 0
        self.puts = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url, path = str(req.url), req.url.path
        if path == "/pwl-yandex":
            return httpx.Response(200, text='<script>window.__CSRF__ = "csrf1";</script>')
        if path.endswith("/auth/password/submit"):
            assert req.headers["x-csrf-token"] == "csrf1"
            return httpx.Response(200, json={"track_id": "T1", "csrf_token": "c"})
        if path.endswith("/auth/magic/code"):
            return httpx.Response(200, json={"link": "https://passport.yandex.ru/am/push/qrsecure?track_id=T1"})
        if path.endswith("/magic/code/status"):
            return httpx.Response(200, json={"state": "otp_auth_finished", "trackId": "T2"} if self.confirmed else {})
        if path.endswith("/sessions/get_session"):
            return httpx.Response(200, json={}, headers={"set-cookie": "Session_id=SID; Domain=.yandex.ru; Path=/"})
        if path.endswith("/oauth/token_by_sessionid"):
            assert "Session_id=SID" in req.headers["ya-client-cookie"]
            return httpx.Response(200, json={"access_token": "XTOKEN"})
        if path == "/1/bundle/auth/x_token/":
            assert req.headers["ya-consumer-authorization"] == "OAuth XTOKEN"
            return httpx.Response(200, json={"status": "ok", "passport_host": "https://passport.yandex.ru", "track_id": "R"})
        if path == "/auth/session/":
            self.session_valid = True
            return httpx.Response(302, headers={"location": "https://passport.yandex.ru/auth/finish?x=1",
                                                "set-cookie": "Session_id=SID2; Domain=.yandex.ru; Path=/"})
        if url.startswith("https://yandex.ru/iot"):
            return httpx.Response(200, text='{"csrfToken2":"CSRF2"}')
        if path == "/m/v3/user/devices":
            if not self.session_valid:
                return httpx.Response(401)
            return httpx.Response(200, json=V3)
        if path == "/m/user/scenarios":
            items = [{"id": "s1", "name": "Утро"}]
            if self.created:
                items.append({"id": "helper1", "name": "SH st-1", "triggers": [{"type": "voice", "value": encode_trigger("st-1")}]})
            return httpx.Response(200, json={"status": "ok", "scenarios": items})
        if path == "/m/v4/user/scenarios" and req.method == "POST":
            self.created += 1
            return httpx.Response(200, json={"status": "ok", "scenario_id": "helper1"})
        if path.startswith("/m/v4/user/scenarios/") and req.method == "PUT":
            self.puts.append(json.loads(req.content))
            return httpx.Response(200, json={"status": "ok"})
        if path.startswith("/m/user/devices/") and path.endswith("/actions"):
            self.csrf_seen.append(req.headers.get("x-csrf-token"))
            self.actions.append(json.loads(req.content))
            return httpx.Response(200, json={"status": "ok", "devices": []})
        if path.startswith("/m/user/scenarios/") and path.endswith("/actions"):
            self.actions.append(path)
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, text=url)


def _wait(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    raise AssertionError("не дождались")


def test_qr_login_full_cycle():
    fake = FakeYandex()
    settings = Settings(yandex_poll_interval=0.2)
    storage = Storage(":memory:")
    app = create_app(settings, storage, [YandexAdapter(settings, storage, httpx.MockTransport(fake))])
    with TestClient(app) as c:
        assert c.post("/api/yandex/qr/check").json()["expired"] is True
        link = c.post("/api/yandex/qr/start").json()["link"]
        assert "qrsecure" in link
        svg = c.get("/api/yandex/qr.svg")
        assert svg.status_code == 200 and b"<svg" in svg.content
        assert c.post("/api/yandex/qr/check").json() == {"done": False}

        fake.confirmed = True
        assert c.post("/api/yandex/qr/check").json() == {"done": True}
        saved = storage.get_json("yandex_quasar")
        assert saved["x_token"] == "XTOKEN" and any(ck["name"] == "Session_id" for ck in saved["cookies"])

        st = c.get("/api/status").json()["adapters"]["yandex"]
        assert st["logged_in"] is True and st["method"] == "qr"
        devs = _wait(lambda: c.get("/api/devices").json()["devices"])
        assert {d["id"] for d in devs} == {"lamp", "door", "weird", "st-1"}
        assert c.get("/api/devices/st-1").json()["is_speaker"] is True
        assert c.get("/api/devices/lamp").json()["is_speaker"] is False
        assert c.get("/api/devices").json()["households"][0]["name"] == "Мой дом"
        # служебный сценарий скрыт из списка сценариев Яндекса
        assert [x["name"] for x in c.get("/api/yandex/scenarios").json()] == ["Утро"]

        r = c.post("/api/devices/st-1/speak", json={"text": "Привет"})
        assert r.status_code == 200, r.text
        assert fake.created == 1  # служебный сценарий создан один раз
        put = fake.puts[-1]
        assert put["name"] == "SH st-1"
        assert put["triggers"][0]["trigger"]["value"] == encode_trigger("st-1")
        cap = put["steps"][0]["parameters"]["items"][0]["value"]["capabilities"][0]
        assert cap["state"] == {"instance": "tts", "value": {"text": "Привет"}}
        assert fake.actions[-1] == "/m/user/scenarios/helper1/actions"

        r = c.post("/api/devices/st-1/speak", json={"text": "включи радио", "mode": "command"})
        assert r.status_code == 200 and fake.created == 1
        cap = fake.puts[-1]["steps"][0]["parameters"]["items"][0]["value"]["capabilities"][0]
        assert cap == {"type": "devices.capabilities.quasar.server_action",
                       "state": {"instance": "text_action", "value": "включи радио"}}
        assert c.post("/api/devices/st-1/speak", json={"text": "я" * 101}).status_code == 400

        # действие speak в сценарии
        sc = c.post("/api/scenarios", json={"name": "Голос", "actions": [
            {"type": "speak", "device": "Станция", "text": "Дверь открыта"}]}).json()
        c.post(f"/api/scenarios/{sc['id']}/run")
        _wait(lambda: any(p["steps"][0]["parameters"]["items"][0]["value"]["capabilities"][0]["state"]
                          .get("value") == {"text": "Дверь открыта"} for p in fake.puts))
        assert c.get("/api/devices/lamp").json()["room_name"] == "Спальня"
        assert c.get("/api/yandex/scenarios").json()[0]["name"] == "Утро"

        r = c.post("/api/devices/lamp/action", json={"set": {"range.brightness": 70}})
        assert r.status_code == 200, r.text
        device_actions = [a for a in fake.actions if isinstance(a, dict)]
        assert device_actions[0]["actions"][0]["state"] == {"instance": "brightness", "value": 70}
        assert set(fake.csrf_seen) == {"CSRF2"}
        assert c.post("/api/yandex/scenarios/s1/run").status_code == 200

        # сессия истекла -> восстановление cookie по x-token
        fake.session_valid = False
        _wait(lambda: fake.session_valid)
        assert any(ck["value"] == "SID2" for ck in storage.get_json("yandex_quasar")["cookies"])

        assert c.post("/api/yandex/logout").status_code == 200
        assert storage.get_json("yandex_quasar") is None
        assert c.get("/api/status").json()["adapters"]["yandex"]["logged_in"] is False


def test_encode_trigger_uuid():
    t = encode_trigger("4f0c2c5e-1b2a-4d3e-9f00-abcdef012345")
    assert len(t) == 36 and set(t) <= set("оеаинтсрвлкмдпуяы")
