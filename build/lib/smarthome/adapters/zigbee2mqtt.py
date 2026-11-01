"""Адаптер Zigbee2MQTT: устройства на собственном Zigbee-стике, полностью локально.

Каждое устройство Zigbee2MQTT превращается в устройство в модели Яндекса
(capabilities/properties), чтобы оно одинаково работало в интерфейсе, в сценариях
и потом в навыке Алисы. Кроме того, все сырые поля доступны как z2m.<поле>,
а записываемые поля можно менять действием z2m.<поле>.
"""

from __future__ import annotations

import asyncio
import colorsys
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings
from ..core.hub import Action, Hub
from ..yandex.models import Capability, Device, Property

log = logging.getLogger(__name__)

SOURCE = "zigbee"

# z2m-поле -> (instance Яндекса, единица, преобразование)
NUMERIC = {
    "temperature": ("temperature", "unit.temperature.celsius", None),
    "local_temperature": ("temperature", "unit.temperature.celsius", None),
    "humidity": ("humidity", "unit.percent", None),
    "battery": ("battery_level", "unit.percent", None),
    "pressure": ("pressure", "unit.pressure.mmhg", lambda v: round(v * 0.750062, 1)),
    "illuminance_lux": ("illumination", "unit.illumination.lux", None),
    "illuminance": ("illumination", "unit.illumination.lux", None),
    "co2": ("co2_level", "unit.ppm", None),
    "power": ("power", "unit.watt", None),
    "current": ("amperage", "unit.ampere", None),
    "energy": ("electricity_meter", "unit.kilowatt_hour", None),
    "pm25": ("pm2.5_density", "unit.density.mcg_m3", None),
    "voc": ("tvoc", "unit.density.mcg_m3", None),
}

# z2m-поле -> (instance события, функция значения, допустимые события)
EVENTS: dict[str, tuple[str, Callable[[Any], Any], list[str]]] = {
    "contact": ("open", lambda v: "closed" if v else "opened", ["opened", "closed"]),
    "occupancy": ("motion", lambda v: "detected" if v else "not_detected", ["detected", "not_detected"]),
    "presence": ("motion", lambda v: "detected" if v else "not_detected", ["detected", "not_detected"]),
    "water_leak": ("water_leak", lambda v: "leak" if v else "dry", ["dry", "leak"]),
    "smoke": ("smoke", lambda v: "detected" if v else "not_detected", ["detected", "not_detected"]),
    "gas": ("gas", lambda v: "detected" if v else "not_detected", ["detected", "not_detected"]),
    "vibration": ("vibration", lambda v: "vibration" if v else None, ["vibration", "tilt", "fall"]),
}

BUTTON_ACTIONS = {
    "single": "click", "click": "click", "on": "click", "off": "click", "toggle": "click",
    "double": "double_click", "double_click": "double_click",
    "long": "long_press", "hold": "long_press", "long_press": "long_press",
}


@dataclass
class Binding:
    key: str
    to_yandex: Callable[[Any, dict[str, Any]], Any]  # (значение поля, всё состояние) -> значение
    prop: str  # поле z2m, из которого читаем
    to_z2m: Callable[[Any], dict[str, Any]] | None = None  # значение Яндекса -> payload для /set


@dataclass
class Mapping:
    device: Device
    friendly_name: str
    endpoint: str | None
    bindings: list[Binding] = field(default_factory=list)


def _flatten(exposes: list[dict]) -> list[tuple[dict, str | None, str | None]]:
    """Список (feature, endpoint, родительский тип) из exposes."""
    out = []
    for e in exposes:
        if e.get("features"):
            for f in e["features"]:
                out.append((f, f.get("endpoint") or e.get("endpoint"), e.get("type")))
        else:
            out.append((e, e.get("endpoint"), None))
    return out


def _xy_to_hs(x: float, y: float) -> tuple[int, int]:
    if not y:
        return 0, 0
    Y = 1.0
    X, Z = (Y / y) * x, (Y / y) * (1 - x - y)
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    m = max(r, g, b, 1e-9)
    r, g, b = (max(c / m, 0) for c in (r, g, b))
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 360), round(s * 100)


def build_mappings(z2m_device: dict[str, Any]) -> list[Mapping]:
    """Описание устройства из zigbee2mqtt/bridge/devices -> устройства в модели Яндекса."""
    definition = z2m_device.get("definition") or {}
    if z2m_device.get("type") == "Coordinator" or not definition:
        return []
    ieee = z2m_device["ieee_address"]
    friendly = z2m_device["friendly_name"]
    features = _flatten(definition.get("exposes", []))
    endpoints = sorted({ep for f, ep, parent in features if ep and parent in ("light", "switch")})

    mappings: dict[str | None, Mapping] = {}

    def get(ep: str | None) -> Mapping:
        if ep not in endpoints:
            ep = None
        if ep not in mappings:
            dev_id = f"z2m:{ieee}" + (f":{ep}" if ep else "")
            name = friendly + (f" {ep}" if ep else "")
            dev = Device(
                id=dev_id, name=name, type="devices.types.other", source=SOURCE,
                external_id=ieee,
            )
            dev.device_info = {  # type: ignore[attr-defined]
                "manufacturer": definition.get("vendor"),
                "model": definition.get("model"),
                "description": definition.get("description"),
            }
            dev.z2m_settable = []  # type: ignore[attr-defined]
            mappings[ep] = Mapping(dev, friendly, ep)
        return mappings[ep]

    types_seen: dict[str | None, set[str]] = {}

    for f, ep, parent in features:
        name, prop = f.get("name"), f.get("property") or f.get("name")
        access = f.get("access", 1)
        m = get(ep)
        kinds = types_seen.setdefault(m.endpoint, set())
        dev = m.device

        if parent in ("light", "switch") and name == "state":
            on_v, off_v = f.get("value_on", "ON"), f.get("value_off", "OFF")
            kinds.add(parent)
            dev.capabilities.append(Capability(
                type="devices.capabilities.on_off", retrievable=True, reportable=True, parameters={}
            ))
            m.bindings.append(Binding(
                "on_off.on", lambda v, s, on_v=on_v: v == on_v, prop,
                lambda v, prop=prop, on_v=on_v, off_v=off_v: {prop: on_v if v else off_v},
            ))
        elif parent == "light" and name == "brightness":
            mx = f.get("value_max", 254) or 254
            dev.capabilities.append(Capability(
                type="devices.capabilities.range", retrievable=True, reportable=True,
                parameters={"instance": "brightness", "unit": "unit.percent", "random_access": True,
                            "range": {"min": 1, "max": 100, "precision": 1}},
            ))
            m.bindings.append(Binding(
                "range.brightness", lambda v, s, mx=mx: max(1, round(v / mx * 100)) if v else 1, prop,
                lambda v, prop=prop, mx=mx: {prop: round(float(v) * mx / 100)},
            ))
        elif parent == "light" and name in ("color_temp", "color_xy", "color_hs"):
            cap = next((c for c in dev.capabilities if c.short_type == "color_setting"), None)
            if cap is None:
                cap = Capability(type="devices.capabilities.color_setting", retrievable=True,
                                 reportable=True, parameters={})
                dev.capabilities.append(cap)
            if name == "color_temp":
                lo, hi = f.get("value_min", 150) or 150, f.get("value_max", 500) or 500
                cap.parameters["temperature_k"] = {"min": round(1e6 / hi), "max": round(1e6 / lo)}
                m.bindings.append(Binding(
                    "color_setting.temperature_k", lambda v, s: round(1e6 / v) if v else None, prop,
                    lambda v, prop=prop: {prop: round(1e6 / float(v))},
                ))
            elif "color_model" not in cap.parameters:
                cap.parameters["color_model"] = "hsv"

                def to_hsv(v: Any, s: dict) -> Any:
                    if not isinstance(v, dict):
                        return None
                    if "hue" in v:
                        h, sat = v.get("hue", 0), v.get("saturation", 100)
                    elif "x" in v:
                        h, sat = _xy_to_hs(v["x"], v["y"])
                    else:
                        return None
                    return {"h": round(h), "s": round(sat), "v": 100}

                m.bindings.append(Binding(
                    "color_setting.hsv", to_hsv, "color",
                    lambda v: {"color": {"hue": v["h"], "saturation": v["s"]}},
                ))
        elif name in NUMERIC and f.get("type") == "numeric":
            instance, unit, conv = NUMERIC[name]
            if name == "temperature" or name == "humidity":
                kinds.add("climate")
            dev.properties.append(Property(
                type="devices.properties.float", retrievable=True, reportable=True,
                parameters={"instance": instance, "unit": unit},
            ))
            m.bindings.append(Binding(
                f"float.{instance}", lambda v, s, conv=conv: conv(v) if conv and v is not None else v, prop,
            ))
        elif name in EVENTS and f.get("type") == "binary":
            instance, fn, events = EVENTS[name]
            kinds.add(name)
            dev.properties.append(Property(
                type="devices.properties.event", retrievable=True, reportable=True,
                parameters={"instance": instance, "events": [{"value": e} for e in events]},
            ))
            m.bindings.append(Binding(f"event.{instance}", lambda v, s, fn=fn: fn(v), prop))
        elif name == "action" and f.get("type") in ("enum", "text"):
            kinds.add("button")
            dev.properties.append(Property(
                type="devices.properties.event", retrievable=False, reportable=True,
                parameters={"instance": "button",
                            "events": [{"value": e} for e in ("click", "double_click", "long_press")]},
            ))
            m.bindings.append(Binding(
                "event.button", lambda v, s: BUTTON_ACTIONS.get(str(v), v) if v else None, prop,
            ))
        # всё, что можно записывать, доступно в интерфейсе как z2m.<поле>
        if access & 2 and f.get("type") in ("binary", "numeric", "enum") and not (
            parent in ("light", "switch") and name in ("state", "brightness")
        ):
            desc = {k: f[k] for k in ("property", "type", "unit", "value_min", "value_max",
                                       "value_step", "value_on", "value_off", "values",
                                       "description", "label") if k in f}
            desc["property"] = prop
            dev.z2m_settable.append(desc)  # type: ignore[attr-defined]

    for ep, m in mappings.items():
        kinds = types_seen.get(ep, set())
        m.device.type = (
            "devices.types.light" if "light" in kinds
            else "devices.types.switch" if "switch" in kinds
            else "devices.types.sensor.open" if "contact" in kinds
            else "devices.types.sensor.motion" if {"occupancy", "presence"} & kinds
            else "devices.types.sensor.water_leak" if "water_leak" in kinds
            else "devices.types.sensor.smoke" if "smoke" in kinds
            else "devices.types.sensor.button" if "button" in kinds
            else "devices.types.sensor.climate" if "climate" in kinds
            else "devices.types.sensor" if m.device.properties
            else "devices.types.other"
        )
    return list(mappings.values())


def states_from_payload(mapping: Mapping, payload: dict[str, Any]) -> dict[str, Any]:
    """Сообщение состояния z2m -> ключи состояний нашего устройства."""
    out: dict[str, Any] = {}
    for b in mapping.bindings:
        if b.prop not in payload:
            continue
        try:
            value = b.to_yandex(payload[b.prop], payload)
        except Exception:
            continue
        if value is not None:
            out[b.key] = value
    # для ламп с цветом и температурой: какой режим сейчас активен
    mode = payload.get("color_mode")
    if mode == "color_temp":
        out.pop("color_setting.hsv", None)
    elif mode in ("xy", "hs"):
        out.pop("color_setting.temperature_k", None)
    for k, v in payload.items():
        out[f"z2m.{k}"] = v
    return out


def payload_from_actions(mapping: Mapping, actions: list[Action]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    by_key = {b.key: b for b in mapping.bindings}
    for act in actions:
        st = act.get("state", {})
        if act.get("type") == "z2m":
            payload[st["instance"]] = st.get("value")
            continue
        cap = act["type"].removeprefix("devices.capabilities.")
        key = f"{cap}.{st.get('instance')}"
        b = by_key.get(key)
        if not b or not b.to_z2m:
            raise ValueError(f"{mapping.device.name}: действие {key} не поддерживается")
        if st.get("relative") and key == "range.brightness":
            payload["brightness_step"] = round(float(st["value"]) * 2.54)
            continue
        payload.update(b.to_z2m(st.get("value")))
    return payload


class Zigbee2MQTTAdapter:
    name = SOURCE

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = settings.z2m_base_topic.rstrip("/")
        self._hub: Hub | None = None
        self._task: asyncio.Task | None = None
        self._client = None
        self.connected = False
        self.last_error: str | None = None
        self.mappings: dict[str, Mapping] = {}  # device_id -> Mapping
        self._by_friendly: dict[str, list[Mapping]] = {}
        self._last_payload: dict[str, dict[str, Any]] = {}
        self.bridge_state: str | None = None

    async def start(self, hub: Hub) -> None:
        self._hub = hub
        self._task = asyncio.create_task(self._loop(), name="z2m")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        import aiomqtt

        delay = 2
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.settings.mqtt_host, port=self.settings.mqtt_port,
                    username=self.settings.mqtt_user or None, password=self.settings.mqtt_password or None,
                    identifier="smarthome-core",
                ) as client:
                    self._client = client
                    self.connected = True
                    self.last_error = None
                    if self._hub:
                        self._hub.log(self.name, f"Подключено к MQTT {self.settings.mqtt_host}")
                    await client.subscribe(f"{self.base}/#")
                    delay = 2
                    async for msg in client.messages:
                        self.handle_message(str(msg.topic), msg.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                self._client = None
                msg = str(e) or type(e).__name__
                if msg != self.last_error and self._hub:
                    self._hub.log(self.name, f"MQTT недоступен: {msg}", "error")
                self.last_error = msg
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def handle_message(self, topic: str, raw: bytes | str | Any) -> None:
        if not topic.startswith(self.base + "/") or not self._hub:
            return
        rest = topic[len(self.base) + 1:]
        try:
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            payload = json.loads(text) if text else None
        except UnicodeDecodeError:
            return
        except ValueError:
            payload = text

        if rest == "bridge/devices" and isinstance(payload, list):
            self._load_devices(payload)
            return
        if rest == "bridge/state":
            self.bridge_state = payload.get("state") if isinstance(payload, dict) else payload
            return
        if rest.startswith("bridge/"):
            return
        if rest.endswith("/availability"):
            name = rest.removesuffix("/availability")
            state = payload.get("state") if isinstance(payload, dict) else payload
            for m in self._by_friendly.get(name, []):
                self._hub.update_states(m.device.id, {"z2m.availability": state})
            return
        if isinstance(payload, dict) and not rest.endswith(("/set", "/get")):
            # запоминаем даже для ещё неизвестных устройств: retained-состояния
            # могут прийти раньше списка устройств bridge/devices
            self._last_payload[rest] = {**self._last_payload.get(rest, {}), **payload}
            for m in self._by_friendly.get(rest, []):
                self._hub.update_states(m.device.id, states_from_payload(m, payload))

    def _load_devices(self, z2m_devices: list[dict[str, Any]]) -> None:
        assert self._hub
        self.mappings.clear()
        self._by_friendly.clear()
        devices = []
        for zd in z2m_devices:
            for m in build_mappings(zd):
                self.mappings[m.device.id] = m
                self._by_friendly.setdefault(m.friendly_name, []).append(m)
                devices.append(m.device)
        self._hub.sync_source(self.name, devices)
        for name, payload in self._last_payload.items():
            for m in self._by_friendly.get(name, []):
                self._hub.update_states(m.device.id, states_from_payload(m, payload))

    async def execute(self, device: Device, actions: list[Action]) -> None:
        m = self.mappings.get(device.id)
        if not m:
            raise KeyError(device.id)
        if not self._client:
            raise RuntimeError("Нет связи с MQTT-брокером")
        payload = payload_from_actions(m, actions)
        await self._client.publish(f"{self.base}/{m.friendly_name}/set", json.dumps(payload))

    async def permit_join(self, seconds: int = 254) -> None:
        if not self._client:
            raise RuntimeError("Нет связи с MQTT-брокером")
        await self._client.publish(
            f"{self.base}/bridge/request/permit_join", json.dumps({"time": seconds})
        )

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "host": self.settings.mqtt_host,
            "bridge_state": self.bridge_state,
            "last_error": self.last_error,
            "devices": len(self.mappings),
        }
