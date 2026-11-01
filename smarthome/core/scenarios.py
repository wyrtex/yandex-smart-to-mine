"""Движок сценариев.

Сценарий — JSON:
{
  "id": "hall_light",
  "name": "Свет в прихожей при открытии двери",
  "enabled": true,
  "mode": "restart",                    # single | restart | parallel
  "triggers": [
    {"type": "state", "device": "Датчик двери", "key": "event.open", "to": "opened"},
    {"type": "state", "device": "Климат", "key": "float.temperature", "above": 26},
    {"type": "time", "at": "07:30", "days": [1, 2, 3, 4, 5]},
    {"type": "interval", "seconds": 600}
  ],
  "conditions": [
    {"type": "time", "after": "18:00", "before": "06:00"},
    {"type": "state", "device": "Лампа", "key": "on_off.on", "op": "eq", "value": false}
  ],
  "actions": [
    {"type": "device", "device": "Лампа", "set": {"on_off.on": true, "range.brightness": 60}},
    {"type": "delay", "seconds": 300},
    {"type": "device", "device": "Лампа", "set": {"on_off.on": false}},
    {"type": "yandex_scenario", "scenario": "Я дома"},
    {"type": "scenario", "scenario": "другой_сценарий"},
    {"type": "log", "message": "Дверь открыта"}
  ]
}
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .hub import Hub, action_from_key, device_states

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Trigger(_M):
    type: Literal["state", "time", "interval"]
    device: str | None = None
    key: str | None = None
    to: Any = None
    from_: Any = Field(None, alias="from")
    above: float | None = None
    below: float | None = None
    at: str | None = None
    days: list[int] | None = None  # 1 = понедельник ... 7 = воскресенье
    seconds: float | None = None

    @field_validator("at")
    @classmethod
    def _check_at(cls, v: str | None) -> str | None:
        if v is not None and not _HHMM.match(v):
            raise ValueError("время в формате ЧЧ:ММ")
        return v


class Condition(_M):
    type: Literal["state", "time", "not", "or"]
    device: str | None = None
    key: str | None = None
    op: Literal["eq", "ne", "gt", "lt", "ge", "le", "in"] = "eq"
    value: Any = None
    after: str | None = None
    before: str | None = None
    days: list[int] | None = None
    conditions: list["Condition"] = Field(default_factory=list)


Condition.model_rebuild()


class ScenarioAction(_M):
    type: Literal["device", "delay", "yandex_scenario", "scenario", "log", "speak", "command"]
    device: str | None = None
    set: dict[str, Any] | None = None
    actions: list[dict[str, Any]] | None = None  # сырые действия в формате Яндекса
    seconds: float | None = None
    scenario: str | None = None
    message: str | None = None
    text: str | None = None  # для speak / command


class Scenario(_M):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    enabled: bool = True
    mode: Literal["single", "restart", "parallel"] = "single"
    triggers: list[Trigger] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    actions: list[ScenarioAction] = Field(default_factory=list)


def _matches_device(hub: Hub, ref: str | None, device_id: str) -> bool:
    if ref is None:
        return True
    dev = hub.find(ref)
    return dev is not None and dev.id == device_id


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def state_trigger_fires(t: Trigger, old: Any, new: Any) -> bool:
    if t.to is not None and new != t.to:
        return False
    if t.from_ is not None and old != t.from_:
        return False
    if t.above is not None or t.below is not None:
        n, o = _num(new), _num(old)
        if n is None:
            return False

        def inside(x: float | None) -> bool:
            return x is not None and (t.above is None or x > t.above) and (t.below is None or x < t.below)

        # срабатываем при входе в диапазон, а не на каждое изменение внутри него
        return inside(n) and not inside(o)
    return old != new


def _compare(op: str, a: Any, b: Any) -> bool:
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if op == "in":
        return a in (b or [])
    na, nb = _num(a), _num(b)
    if na is None or nb is None:
        return False
    return {"gt": na > nb, "lt": na < nb, "ge": na >= nb, "le": na <= nb}[op]


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def time_in_window(now: datetime, after: str | None, before: str | None) -> bool:
    cur = now.hour * 60 + now.minute
    a = _minutes(after) if after else 0
    b = _minutes(before) if before else 24 * 60
    if a <= b:
        return a <= cur < b
    return cur >= a or cur < b  # окно через полночь, например 22:00–06:00


class ScenarioEngine:
    def __init__(self, hub: Hub, timezone: str = "Europe/Moscow"):
        self.hub = hub
        self.tz = ZoneInfo(timezone)
        self.scenarios: dict[str, Scenario] = {}
        self._running: dict[str, set[asyncio.Task]] = {}
        self._tasks: list[asyncio.Task] = []
        self._last_interval: dict[tuple[str, int], float] = {}
        self.last_run: dict[str, dict[str, Any]] = {}
        self.load()

    # --- хранение -----------------------------------------------------------

    def load(self) -> None:
        self.scenarios = {}
        for data in self.hub.storage.list_scenarios():
            try:
                sc = Scenario.model_validate(data)
                self.scenarios[sc.id] = sc
            except Exception as e:
                self.hub.log("scenarios", f"Сценарий {data.get('id')} не загружен: {e}", "error")

    def save(self, data: dict[str, Any]) -> Scenario:
        sc = Scenario.model_validate(data)
        self.hub.storage.save_scenario(sc.model_dump(by_alias=True, exclude_none=True))
        self.scenarios[sc.id] = sc
        return sc

    def delete(self, scenario_id: str) -> None:
        self.cancel(scenario_id)
        self.hub.storage.delete_scenario(scenario_id)
        self.scenarios.pop(scenario_id, None)

    # --- жизненный цикл -----------------------------------------------------

    async def start(self) -> None:
        q = self.hub.subscribe()  # подписка сразу, чтобы не пропустить ранние события
        self._tasks = [
            asyncio.create_task(self._event_loop(q), name="scenarios-events"),
            asyncio.create_task(self._clock_loop(), name="scenarios-clock"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for sid in list(self._running):
            self.cancel(sid)

    def cancel(self, scenario_id: str) -> None:
        for t in self._running.pop(scenario_id, set()):
            t.cancel()

    def is_running(self, scenario_id: str) -> bool:
        return bool(self._running.get(scenario_id))

    async def _event_loop(self, q: asyncio.Queue) -> None:
        try:
            while True:
                ev = await q.get()
                if ev.get("type") == "state":
                    self.on_state(ev)
        finally:
            self.hub.unsubscribe(q)

    def on_state(self, ev: dict[str, Any]) -> None:
        for sc in list(self.scenarios.values()):
            if not sc.enabled:
                continue
            for t in sc.triggers:
                if (
                    t.type == "state"
                    and (t.key is None or t.key == ev["key"])
                    and _matches_device(self.hub, t.device, ev["device_id"])
                    and state_trigger_fires(t, ev.get("old"), ev.get("value"))
                ):
                    self.fire(sc, reason=f"{ev.get('device')}: {ev['key']} = {ev.get('value')}")
                    break

    async def _clock_loop(self) -> None:
        last_minute = None
        while True:
            now = datetime.now(self.tz)
            minute = now.strftime("%Y-%m-%d %H:%M")
            if minute != last_minute:
                last_minute = minute
                self.on_minute(now)
            self.on_tick(time.time())
            await asyncio.sleep(1)

    def on_minute(self, now: datetime) -> None:
        hhmm = now.strftime("%H:%M")
        for sc in list(self.scenarios.values()):
            if not sc.enabled:
                continue
            for t in sc.triggers:
                if t.type == "time" and t.at and _minutes(t.at) == _minutes(hhmm) and (
                    not t.days or now.isoweekday() in t.days
                ):
                    self.fire(sc, reason=f"время {t.at}")
                    break

    def on_tick(self, ts: float) -> None:
        for sc in list(self.scenarios.values()):
            if not sc.enabled:
                continue
            for i, t in enumerate(sc.triggers):
                if t.type == "interval" and t.seconds:
                    key = (sc.id, i)
                    last = self._last_interval.setdefault(key, ts)
                    if ts - last >= t.seconds:
                        self._last_interval[key] = ts
                        self.fire(sc, reason=f"каждые {int(t.seconds)} с")

    # --- выполнение ---------------------------------------------------------

    def check_conditions(self, conditions: list[Condition], now: datetime | None = None) -> bool:
        now = now or datetime.now(self.tz)
        return all(self._check(c, now) for c in conditions)

    def _check(self, c: Condition, now: datetime) -> bool:
        if c.type == "time":
            return time_in_window(now, c.after, c.before) and (not c.days or now.isoweekday() in c.days)
        if c.type == "not":
            return not self.check_conditions(c.conditions, now)
        if c.type == "or":
            return any(self._check(x, now) for x in c.conditions)
        dev = self.hub.find(c.device or "")
        if not dev:
            return False
        return _compare(c.op, device_states(dev).get(c.key or ""), c.value)

    def fire(self, sc: Scenario, reason: str = "", force: bool = False) -> bool:
        if not force and not self.check_conditions(sc.conditions):
            return False
        if self.is_running(sc.id):
            if sc.mode == "single":
                return False
            if sc.mode == "restart":
                self.cancel(sc.id)
        task = asyncio.create_task(self._run(sc, reason), name=f"scenario-{sc.id}")
        self._running.setdefault(sc.id, set()).add(task)
        task.add_done_callback(lambda t, sid=sc.id: self._running.get(sid, set()).discard(t))
        return True

    async def _run(self, sc: Scenario, reason: str, depth: int = 0) -> None:
        self.hub.log("scenarios", f"▶ {sc.name}" + (f" ({reason})" if reason else ""))
        self.last_run[sc.id] = {"ts": time.time(), "reason": reason, "ok": None}
        self.hub.emit({"type": "scenario", "id": sc.id, "status": "started"})
        ok = True
        try:
            for act in sc.actions:
                try:
                    await self._do(act, depth)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    ok = False
                    self.hub.log("scenarios", f"{sc.name}: ошибка действия {act.type}: {e}", "error")
        except asyncio.CancelledError:
            self.hub.log("scenarios", f"■ {sc.name}: прерван")
            ok = False
            raise
        finally:
            self.last_run[sc.id]["ok"] = ok
            self.hub.emit({"type": "scenario", "id": sc.id, "status": "finished", "ok": ok})

    async def _do(self, act: ScenarioAction, depth: int) -> None:
        if act.type == "device":
            actions = list(act.actions or [])
            actions += [action_from_key(k, v) for k, v in (act.set or {}).items()]
            await self.hub.execute(act.device or "", actions)
        elif act.type == "delay":
            await asyncio.sleep(act.seconds or 0)
        elif act.type in ("speak", "command"):
            await self.hub.speak(act.device or "", act.text or "", "tts" if act.type == "speak" else "command")
        elif act.type == "log":
            self.hub.log("scenarios", act.message or "")
        elif act.type == "scenario":
            target = self.scenarios.get(act.scenario or "") or next(
                (s for s in self.scenarios.values() if s.name == act.scenario), None
            )
            if not target:
                raise KeyError(f"сценарий «{act.scenario}» не найден")
            if depth > 5:
                raise RecursionError("слишком глубокая вложенность сценариев")
            await self._run(target, "из другого сценария", depth + 1)
        elif act.type == "yandex_scenario":
            ya = self.hub.adapters.get("yandex")
            scenarios = self.hub.extra.get("yandex_scenarios", [])
            sc = next((s for s in scenarios if act.scenario in (s["id"], s["name"])), None)
            if not ya or not sc:
                raise KeyError(f"сценарий Яндекса «{act.scenario}» не найден")
            await ya.run_scenario(sc["id"])  # type: ignore[attr-defined]
