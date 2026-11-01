import asyncio
import json
from pathlib import Path

import httpx
import pytest

from smarthome.inventory import format_inventory
from smarthome.yandex import YandexAPIError, YandexIoTClient, actions
from smarthome.yandex.models import UserInfo

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "user_info.json").read_text("utf-8"))


def make_client(handler):
    return YandexIoTClient("tok", transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_parse_user_info_keeps_everything():
    info = UserInfo.model_validate(FIXTURE)
    assert len(info.devices) == 5
    lamp = info.device("лампа")
    assert lamp.capability("range", "brightness").state.value == 80
    assert lamp.capability("color_setting").parameters["temperature_k"]["max"] == 6500
    door = info.device("door-1")
    assert door.property("open").state.value == "closed"
    assert door.property("battery_level").unit == "percent"
    # неизвестные поля не теряются
    assert info.device("sw-1").model_dump()["some_future_field"] == {"kept": True}


def test_inventory_report():
    text = format_inventory(UserInfo.model_validate(FIXTURE))
    assert "== Гостиная ==" in text
    assert "range.brightness = 80" in text
    assert "event.open = closed" in text
    assert "события=opened,closed" in text
    assert "mode.input_source = —" in text
    assert "режимы=one,two" in text
    assert "Я дома" in text


def test_user_info_sends_token():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["path"] = req.url.path
        return httpx.Response(200, json=FIXTURE)

    async def go():
        async with make_client(handler) as c:
            return await c.user_info()

    info = run(go())
    assert seen == {"auth": "Bearer tok", "path": "/v1.0/user/info"}
    assert info.households[0].name == "Мой дом"


def test_action_body_and_success():
    captured = {}

    def handler(req: httpx.Request):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "status": "ok", "request_id": "r",
            "devices": [{"id": "lamp-1", "capabilities": [{
                "type": "devices.capabilities.on_off",
                "state": {"instance": "on", "action_result": {"status": "DONE"}}}]}],
        })

    async def go():
        async with make_client(handler) as c:
            await c.act("lamp-1", actions.on_off(True), actions.color_rgb("#FF8800"))

    run(go())
    assert captured["body"] == {"devices": [{"id": "lamp-1", "actions": [
        {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}},
        {"type": "devices.capabilities.color_setting", "state": {"instance": "rgb", "value": 0xFF8800}},
    ]}]}


def test_action_device_error_raises():
    def handler(req):
        return httpx.Response(200, json={
            "status": "ok", "request_id": "r",
            "devices": [{"id": "tv-1", "capabilities": [{
                "type": "devices.capabilities.on_off",
                "state": {"instance": "on", "action_result": {
                    "status": "ERROR", "error_code": "DEVICE_UNREACHABLE"}}}]}],
        })

    async def go():
        async with make_client(handler) as c:
            await c.act("tv-1", actions.on_off(True))

    with pytest.raises(YandexAPIError, match="DEVICE_UNREACHABLE"):
        run(go())


def test_unauthorized():
    async def go():
        async with make_client(lambda r: httpx.Response(401, json={"status": "error"})) as c:
            await c.user_info()

    with pytest.raises(YandexAPIError, match="Токен"):
        run(go())


def test_relative_range():
    assert actions.range_("volume", -10, relative=True) == {
        "type": "devices.capabilities.range",
        "state": {"instance": "volume", "value": -10, "relative": True},
    }
