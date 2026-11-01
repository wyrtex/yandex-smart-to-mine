import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from smarthome.adapters.yandex import YandexAdapter
from smarthome.adapters.zigbee2mqtt import (
    build_mappings, payload_from_actions, states_from_payload,
)
from smarthome.config import Settings
from smarthome.core.hub import Hub, action_from_key, device_states
from smarthome.core.scenarios import Scenario, ScenarioEngine, Trigger, state_trigger_fires, time_in_window
from smarthome.storage import Storage
from smarthome.web.app import create_app
from smarthome.yandex.models import UserInfo

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "user_info.json").read_text("utf-8"))


class FakeAdapter:
    name = "yandex"

    def __init__(self):
        self.calls = []

    async def start(self, hub): ...
    async def stop(self): ...
    def status(self): return {}

    async def execute(self, device, actions):
        self.calls.append((device.id, actions))


def make_hub():
    hub = Hub(Storage(":memory:"))
    fake = FakeAdapter()
    hub.add_adapter(fake)
    info = UserInfo.model_validate(FIXTURE)
    hub.sync_source("yandex", info.devices, {r.id: r.name for r in info.rooms})
    return hub, fake


# ------------------------------------------------------------------ hub

def test_device_states_and_keys():
    hub, _ = make_hub()
    st = device_states(hub.find("Лампа"))
    assert st == {"on_off.on": True, "color_setting.temperature_k": 4500, "range.brightness": 80}
    assert device_states(hub.find("Датчик двери"))["event.open"] == "closed"


def test_action_from_key():
    assert action_from_key("range.volume", "+5")["state"] == {"instance": "volume", "value": 5.0, "relative": True}
    assert action_from_key("color_setting.rgb", "#00ff00")["state"]["value"] == 0x00FF00
    assert action_from_key("on_off.on", "вкл")["state"]["value"] is True
    assert action_from_key("z2m.child_lock", "LOCK") == {"type": "z2m", "state": {"instance": "child_lock", "value": "LOCK"}}
    with pytest.raises(ValueError):
        action_from_key("bad", 1)


def test_sync_emits_changes_and_history():
    async def go():
        hub, _ = make_hub()
        q = hub.subscribe()
        info = json.loads(json.dumps(FIXTURE))
        info["devices"][1]["properties"][0]["state"]["value"] = "opened"
        hub.sync_source("yandex", UserInfo.model_validate(info).devices)
        ev = q.get_nowait()
        assert (ev["device_id"], ev["key"], ev["old"], ev["value"]) == ("door-1", "event.open", "closed", "opened")
        assert q.empty()
        assert hub.storage.history("door-1", "event.open")[-1]["value"] == "opened"
        # устройство пропало из источника
        info["devices"].pop()
        hub.sync_source("yandex", UserInfo.model_validate(info).devices)
        assert hub.find("sw-1") is None
    asyncio.run(go())


def test_execute_optimistic_update():
    async def go():
        hub, fake = make_hub()
        await hub.execute("Лампа", [action_from_key("range.brightness", "+10"), action_from_key("on_off.on", False)])
        assert fake.calls[0][0] == "lamp-1"
        st = device_states(hub.find("lamp-1"))
        assert st["range.brightness"] == 90 and st["on_off.on"] is False
        await hub.execute("Лампа", [action_from_key("color_setting.hsv", {"h": 10, "s": 50, "v": 100})])
        st = device_states(hub.find("lamp-1"))
        assert st["color_setting.hsv"] == {"h": 10, "s": 50, "v": 100}
        assert "color_setting.temperature_k" not in st
    asyncio.run(go())


# ------------------------------------------------------------------ сценарии

def test_trigger_logic():
    t = Trigger(type="state", key="event.open", to="opened")
    assert state_trigger_fires(t, "closed", "opened")
    assert not state_trigger_fires(t, "opened", "closed")
    t = Trigger(type="state", key="float.temperature", above=26)
    assert state_trigger_fires(t, 25.5, 26.5)
    assert not state_trigger_fires(t, 26.5, 27)  # уже выше порога
    assert Trigger.model_validate({"type": "state", "from": "a"}).from_ == "a"


def test_time_window():
    d = lambda h, m: datetime(2026, 1, 1, h, m)
    assert time_in_window(d(23, 0), "22:00", "06:00")
    assert time_in_window(d(5, 59), "22:00", "06:00")
    assert not time_in_window(d(12, 0), "22:00", "06:00")
    assert time_in_window(d(12, 0), "09:00", "18:00")


def test_scenario_runs_on_state_change():
    async def go():
        hub, fake = make_hub()
        engine = ScenarioEngine(hub)
        engine.save({
            "id": "door", "name": "Дверь",
            "triggers": [{"type": "state", "device": "Датчик двери", "key": "event.open", "to": "opened"}],
            "conditions": [{"type": "state", "device": "Выключатель", "key": "on_off.on", "value": False}],
            "actions": [
                {"type": "device", "device": "Выключатель", "set": {"on_off.on": True}},
                {"type": "delay", "seconds": 0.01},
                {"type": "device", "device": "Лампа", "set": {"range.brightness": 30}},
            ],
        })
        await engine.start()
        hub.update_states("door-1", {"event.open": "opened"})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(fake.calls) == 2:
                break
        assert [c[0] for c in fake.calls] == ["sw-1", "lamp-1"]
        # условие больше не выполняется (выключатель уже включён) — повторно не запускается
        hub.update_states("door-1", {"event.open": "closed"})
        hub.update_states("door-1", {"event.open": "opened"})
        await asyncio.sleep(0.05)
        assert len(fake.calls) == 2
        await engine.stop()
        # сценарий сохранён в базе
        assert ScenarioEngine(hub).scenarios["door"].name == "Дверь"
    asyncio.run(go())


def test_scenario_validation():
    with pytest.raises(Exception):
        Scenario.model_validate({"name": "x", "triggers": [{"type": "time", "at": "25:00"}]})
    with pytest.raises(Exception):
        Scenario.model_validate({"name": "x", "actions": [{"type": "unknown"}]})


# ------------------------------------------------------------------ zigbee2mqtt

Z2M_LIGHT = {
    "ieee_address": "0x01", "friendly_name": "kitchen/bulb", "type": "Router",
    "definition": {"model": "LED1", "vendor": "IKEA", "description": "bulb", "exposes": [
        {"type": "light", "features": [
            {"type": "binary", "name": "state", "property": "state", "access": 7, "value_on": "ON", "value_off": "OFF"},
            {"type": "numeric", "name": "brightness", "property": "brightness", "access": 7, "value_min": 0, "value_max": 254},
            {"type": "numeric", "name": "color_temp", "property": "color_temp", "access": 7, "value_min": 153, "value_max": 454},
            {"type": "composite", "name": "color_xy", "property": "color", "access": 7, "features": []},
        ]},
        {"type": "enum", "name": "power_on_behavior", "property": "power_on_behavior", "access": 7, "values": ["off", "on"]},
        {"type": "numeric", "name": "linkquality", "property": "linkquality", "access": 1},
    ]},
}
Z2M_DOOR = {
    "ieee_address": "0x02", "friendly_name": "door", "type": "EndDevice",
    "definition": {"model": "YNDX-00520", "vendor": "Yandex", "exposes": [
        {"type": "binary", "name": "contact", "property": "contact", "access": 1, "value_on": False, "value_off": True},
        {"type": "numeric", "name": "battery", "property": "battery", "access": 1, "unit": "%"},
    ]},
}
Z2M_2GANG = {
    "ieee_address": "0x03", "friendly_name": "switch2", "type": "Router",
    "definition": {"model": "SW2", "vendor": "X", "exposes": [
        {"type": "switch", "endpoint": "l1", "features": [
            {"type": "binary", "name": "state", "property": "state_l1", "endpoint": "l1", "access": 7, "value_on": "ON", "value_off": "OFF"}]},
        {"type": "switch", "endpoint": "l2", "features": [
            {"type": "binary", "name": "state", "property": "state_l2", "endpoint": "l2", "access": 7, "value_on": "ON", "value_off": "OFF"}]},
        {"type": "enum", "name": "action", "property": "action", "access": 1, "values": ["single_left", "double"]},
    ]},
}


def test_z2m_light_mapping():
    [m] = build_mappings(Z2M_LIGHT)
    d = m.device
    assert d.id == "z2m:0x01" and d.type == "devices.types.light" and d.source == "zigbee"
    cs = d.capability("color_setting")
    assert cs.parameters["temperature_k"] == {"min": 2203, "max": 6536} and cs.parameters["color_model"] == "hsv"
    st = states_from_payload(m, {"state": "ON", "brightness": 127, "color_temp": 370, "color_mode": "color_temp",
                                 "color": {"x": 0.5, "y": 0.4}, "linkquality": 80})
    assert st["on_off.on"] is True and st["range.brightness"] == 50
    assert st["color_setting.temperature_k"] == 2703 and "color_setting.hsv" not in st
    assert st["z2m.linkquality"] == 80
    pl = payload_from_actions(m, [action_from_key("on_off.on", False), action_from_key("range.brightness", 100),
                                  action_from_key("color_setting.temperature_k", 4000),
                                  action_from_key("z2m.power_on_behavior", "on")])
    assert pl == {"state": "OFF", "brightness": 254, "color_temp": 250, "power_on_behavior": "on"}
    assert [f["property"] for f in d.z2m_settable] == ["color_temp", "power_on_behavior"]


def test_z2m_sensor_and_multichannel():
    [door] = build_mappings(Z2M_DOOR)
    assert door.device.type == "devices.types.sensor.open"
    st = states_from_payload(door, {"contact": False, "battery": 90})
    assert st["event.open"] == "opened" and st["float.battery_level"] == 90
    with pytest.raises(ValueError):
        payload_from_actions(door, [action_from_key("on_off.on", True)])

    ms = {m.device.id: m for m in build_mappings(Z2M_2GANG)}
    assert set(ms) == {"z2m:0x03", "z2m:0x03:l1", "z2m:0x03:l2"}
    assert payload_from_actions(ms["z2m:0x03:l2"], [action_from_key("on_off.on", True)]) == {"state_l2": "ON"}
    assert states_from_payload(ms["z2m:0x03:l1"], {"state_l1": "OFF"})["on_off.on"] is False
    assert ms["z2m:0x03"].device.type == "devices.types.sensor.button"


def test_z2m_adapter_messages():
    from smarthome.adapters.zigbee2mqtt import Zigbee2MQTTAdapter

    hub = Hub(Storage(":memory:"))
    z = Zigbee2MQTTAdapter(Settings(mqtt_host="x"))
    hub.add_adapter(z)
    z._hub = hub
    z.handle_message("zigbee2mqtt/door", json.dumps({"contact": True}).encode())  # до списка устройств
    z.handle_message("zigbee2mqtt/bridge/devices", json.dumps([Z2M_DOOR, Z2M_LIGHT]).encode())
    assert device_states(hub.find("door"))["event.open"] == "closed"
    z.handle_message("zigbee2mqtt/kitchen/bulb", b'{"state":"ON"}')
    assert device_states(hub.find("kitchen/bulb"))["on_off.on"] is True
    z.handle_message("zigbee2mqtt/kitchen/bulb/availability", b'{"state":"offline"}')
    assert device_states(hub.find("kitchen/bulb"))["z2m.availability"] == "offline"


# ------------------------------------------------------------------ веб-API

def _yandex_transport(calls):
    def handler(req: httpx.Request):
        if req.url.path == "/v1.0/user/info":
            return httpx.Response(200, json=FIXTURE)
        if req.url.path == "/v1.0/devices/actions":
            calls.append(json.loads(req.content))
            return httpx.Response(200, json={"status": "ok", "devices": []})
        if req.url.path.startswith("/v1.0/scenarios/"):
            calls.append(req.url.path)
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={"status": "error"})
    return httpx.MockTransport(handler)


def make_client(password=""):
    calls = []
    settings = Settings(yandex_token="tok", password=password)
    storage = Storage(":memory:")
    app = create_app(settings, storage, [YandexAdapter(settings, storage, _yandex_transport(calls))])
    return TestClient(app), calls


def _wait_devices(c):
    for _ in range(100):
        data = c.get("/api/devices").json()
        if data["devices"]:
            return data
        time.sleep(0.02)
    raise AssertionError("устройства не загрузились")


def test_api_devices_and_actions():
    client, calls = make_client()
    with client as c:
        data = _wait_devices(c)
        assert len(data["devices"]) == 5 and data["rooms"]["room-1"] == "Гостиная"
        lamp = c.get("/api/devices/Лампа").json()
        assert lamp["room_name"] == "Гостиная" and lamp["states"]["range.brightness"] == 80
        r = c.post("/api/devices/lamp-1/action", json={"set": {"range.brightness": 25}})
        assert r.status_code == 200 and r.json()["states"]["range.brightness"] == 25
        assert calls[0]["devices"][0]["actions"][0]["state"] == {"instance": "brightness", "value": 25}
        assert c.post("/api/devices/nope/action", json={"set": {"on_off.on": True}}).status_code == 404
        assert c.post("/api/devices/lamp-1/action", json={}).status_code == 400

        hist = c.get("/api/devices/lamp-1/history?key=range.brightness").json()
        assert [h["value"] for h in hist][-1] == 25

        assert c.post("/api/yandex/scenarios/sc-1/run").status_code == 200
        assert c.get("/api/yandex/scenarios").json()[0]["name"] == "Я дома"

        sc = c.post("/api/scenarios", json={"name": "Тест", "actions": [{"type": "log", "message": "привет"}]}).json()
        assert c.post(f"/api/scenarios/{sc['id']}/run").status_code == 200
        assert c.post("/api/scenarios", json={"name": "x", "triggers": [{"type": "bad"}]}).status_code == 400
        assert c.get("/api/scenarios").json()[0]["name"] == "Тест"

        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "hello"
            c.post("/api/devices/lamp-1/action", json={"set": {"on_off.on": False}})
            ev = ws.receive_json()
            while ev["type"] != "state":
                ev = ws.receive_json()
            assert ev["key"] == "on_off.on" and ev["value"] is False

        assert c.get("/api/status").json()["adapters"]["yandex"]["logged_in"] is True
        page = c.get("/")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
        assert c.get("/static/app.js").headers["cache-control"] == "no-cache"


def test_api_password():
    client, _ = make_client(password="secret")
    with client as c:
        assert c.get("/api/ping").json()["auth_required"] is True
        assert c.get("/api/devices").status_code == 401
        assert c.get("/").status_code == 200  # интерфейс сам покажет экран входа
        assert c.get("/app.ipa").status_code in (200, 404)  # скачивание без пароля
        assert c.post("/api/login", json={"password": "nope"}).status_code == 401
        assert c.post("/api/login", json={"password": "secret"}).status_code == 200
        assert c.get("/api/devices").status_code == 200  # cookie сохранилась
    client2, _ = make_client(password="secret")
    with client2 as c:
        assert c.get("/api/status", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_ipa_download(tmp_path):
    (tmp_path / "SmartHome.ipa").write_bytes(b"PK-test")
    settings = Settings(password="x", dist_dir=tmp_path)
    storage = Storage(":memory:")
    with TestClient(create_app(settings, storage, [FakeAdapter()])) as c:
        r = c.get("/app.ipa")
        assert r.status_code == 200 and r.content == b"PK-test"
        assert "SmartHome.ipa" in r.headers["content-disposition"]
        assert "itms-services://" in c.get("/install").text


def test_login_with_pasted_token():
    calls = []
    settings = Settings(yandex_client_id="cid", yandex_client_secret="sec")
    storage = Storage(":memory:")
    app = create_app(settings, storage, [YandexAdapter(settings, storage, _yandex_transport(calls))])
    with TestClient(app) as c:
        assert "response_type=token" in c.get("/api/yandex/login-url?mode=token").json()["url"]
        assert "response_type=code" in c.get("/api/yandex/login-url").json()["url"]
        assert c.get("/api/status").json()["adapters"]["yandex"]["logged_in"] is False
        r = c.post("/api/yandex/code", json={"code": "  y0_AgAAAAAtestTokenValue1234567890  "})
        assert r.status_code == 200, r.text
        assert storage.get_json("yandex_oauth")["access_token"] == "y0_AgAAAAAtestTokenValue1234567890"
        assert c.get("/api/status").json()["adapters"]["yandex"]["logged_in"] is True
        _wait_devices(c)
