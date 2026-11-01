"""Ядро: реестр всех устройств (из любых источников), их состояния и события.

Модель устройства едина для всех источников и совпадает с моделью Яндекса
(capabilities + properties), поэтому любое устройство можно отдать обратно в Алису.

Ключ состояния — строка «тип.instance», например:
    on_off.on, range.brightness, color_setting.temperature_k, mode.input_source,
    toggle.mute, float.temperature, event.open, z2m.<поле> (сырые поля Zigbee2MQTT)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from ..storage import Storage
from ..yandex.models import Capability, Device, Property, State

log = logging.getLogger(__name__)

Action = dict[str, Any]


class Adapter(Protocol):
    name: str

    async def start(self, hub: "Hub") -> None: ...
    async def stop(self) -> None: ...
    async def execute(self, device: Device, actions: list[Action]) -> None: ...
    def status(self) -> dict[str, Any]: ...


def cap_key(cap: Capability) -> str:
    if cap.short_type == "color_setting":
        inst = cap.state.instance if cap.state and cap.state.instance else "color"
        return f"color_setting.{inst}"
    return f"{cap.short_type}.{cap.instance or '?'}"


def prop_key(prop: Property) -> str:
    return f"{prop.short_type}.{prop.instance or '?'}"


def device_states(dev: Device) -> dict[str, Any]:
    """Плоский словарь {ключ: значение} всех известных состояний устройства."""
    out: dict[str, Any] = {}
    for cap in dev.capabilities:
        if cap.state is not None:
            out[cap_key(cap)] = cap.state.value
    for prop in dev.properties:
        if prop.state is not None:
            out[prop_key(prop)] = prop.state.value
    extra = getattr(dev, "raw_state", None)
    if isinstance(extra, dict):
        out.update({f"z2m.{k}": v for k, v in extra.items()})
    return out


def action_from_key(key: str, value: Any) -> Action:
    """'range.brightness', 50 -> действие в формате Яндекса.

    Строковое значение с + или - для range означает относительное изменение: '+5', '-10'.
    """
    cap, _, instance = key.partition(".")
    if not instance:
        raise ValueError(f"Неверный ключ «{key}», ожидается тип.instance, например on_off.on")
    state: dict[str, Any] = {"instance": instance, "value": value}
    if cap == "z2m":
        return {"type": "z2m", "state": state}
    if cap == "range" and isinstance(value, str) and value[:1] in "+-":
        state.update(value=float(value), relative=True)
    elif cap == "range" and isinstance(value, str):
        state["value"] = float(value)
    elif cap == "color_setting" and instance == "rgb" and isinstance(value, str):
        state["value"] = int(value.lstrip("#"), 16)
    elif cap in ("on_off", "toggle") and isinstance(value, str):
        state["value"] = value.lower() in ("1", "true", "on", "да", "вкл")
    return {"type": f"devices.capabilities.{cap}", "state": state}


class Hub:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.devices: dict[str, Device] = {}
        self.rooms: dict[str, str] = {}
        self.adapters: dict[str, Adapter] = {}
        self.extra: dict[str, Any] = {}  # данные адаптеров, например сценарии Яндекса
        self._subscribers: set[asyncio.Queue] = set()

    # --- адаптеры -----------------------------------------------------------

    def add_adapter(self, adapter: Adapter) -> None:
        self.adapters[adapter.name] = adapter

    async def start(self) -> None:
        for a in self.adapters.values():
            await a.start(self)

    async def stop(self) -> None:
        for a in self.adapters.values():
            try:
                await a.stop()
            except Exception:  # pragma: no cover
                log.exception("Ошибка остановки адаптера %s", a.name)

    # --- события ------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def emit(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def log(self, source: str, message: str, level: str = "info") -> None:
        getattr(log, "warning" if level == "warning" else "error" if level == "error" else "info")(
            "[%s] %s", source, message
        )
        self.storage.log(source, message, level)
        self.emit({"type": "log", "source": source, "message": message, "level": level})

    # --- устройства ---------------------------------------------------------

    def find(self, id_or_name: str) -> Device | None:
        if id_or_name in self.devices:
            return self.devices[id_or_name]
        needle = id_or_name.casefold()
        for dev in self.devices.values():
            if dev.name.casefold() == needle or needle in (a.casefold() for a in dev.aliases):
                return dev
        return None

    def sync_source(self, source: str, devices: list[Device], rooms: dict[str, str] | None = None) -> None:
        """Полная синхронизация устройств одного источника (например, после опроса Яндекса)."""
        if rooms:
            self.rooms.update(rooms)
        seen = set()
        changed_list = False
        for dev in devices:
            dev.source = source
            seen.add(dev.id)
            if dev.id not in self.devices:
                changed_list = True
            self.upsert(dev, announce=False)
        for dev_id in [d.id for d in self.devices.values() if d.source == source and d.id not in seen]:
            del self.devices[dev_id]
            changed_list = True
        if changed_list:
            self.emit({"type": "devices"})

    def upsert(self, dev: Device, announce: bool = True) -> None:
        old = self.devices.get(dev.id)
        old_states = device_states(old) if old else {}
        self.devices[dev.id] = dev
        if old is None and announce:
            self.emit({"type": "devices"})
        self._diff(dev, old_states, device_states(dev))

    def update_states(self, device_id: str, states: dict[str, Any]) -> None:
        """Частичное обновление состояний по ключам (для оптимистичных обновлений и Zigbee)."""
        dev = self.devices.get(device_id)
        if not dev:
            return
        before = device_states(dev)
        now = time.time()
        for key, value in states.items():
            _apply_state(dev, key, value, now)
        self._diff(dev, before, device_states(dev))

    def _diff(self, dev: Device, old: dict[str, Any], new: dict[str, Any]) -> None:
        now = time.time()
        for key in new.keys() | old.keys():
            if key in new and (key not in old or old[key] != new[key]):
                value = new[key]
                if not key.startswith("z2m."):
                    self.storage.add_history(dev.id, key, value, now)
                self.emit({
                    "type": "state", "device_id": dev.id, "device": dev.name,
                    "key": key, "value": value, "old": old.get(key), "ts": now,
                })

    async def execute(self, id_or_name: str, actions: list[Action]) -> None:
        dev = self.find(id_or_name)
        if not dev:
            raise KeyError(f"Устройство «{id_or_name}» не найдено")
        adapter = self.adapters.get(dev.source)
        if not adapter:
            raise RuntimeError(f"Нет адаптера для источника {dev.source}")
        await adapter.execute(dev, actions)
        # оптимистичное обновление: интерфейс и сценарии видят изменение сразу
        optimistic: dict[str, Any] = {}
        current = device_states(dev)
        for act in actions:
            cap = act["type"].removeprefix("devices.capabilities.")
            st = act.get("state", {})
            key = f"{cap}.{st.get('instance')}"
            value = st.get("value")
            if st.get("relative"):
                if not isinstance(current.get(key), (int, float)):
                    continue
                value = current[key] + value
            optimistic[key] = value
        self.update_states(dev.id, optimistic)

    def to_dict(self, dev: Device) -> dict[str, Any]:
        data = dev.model_dump(mode="json")
        data["room_name"] = self.rooms.get(dev.room or "", None)
        data["states"] = device_states(dev)
        return data


def _apply_state(dev: Device, key: str, value: Any, ts: float) -> None:
    kind, _, instance = key.partition(".")
    if kind == "z2m":
        raw = getattr(dev, "raw_state", None)
        if not isinstance(raw, dict):
            raw = {}
            setattr(dev, "raw_state", raw)
        raw[instance] = value
        return
    if kind in ("float", "event"):
        for prop in dev.properties:
            if prop.short_type == kind and prop.instance == instance:
                prop.state = State(instance=instance, value=value)
                prop.last_updated = ts
        return
    for cap in dev.capabilities:
        if cap.short_type != kind:
            continue
        if kind == "color_setting" or cap.instance == instance:
            cap.state = State(instance=instance, value=value)
            cap.last_updated = ts
            return
