"""Человекочитаемый отчёт обо всех устройствах, их умениях и свойствах."""

from __future__ import annotations

from datetime import datetime, timezone

from .yandex.models import Capability, Device, Property, UserInfo


UNITS = {
    "percent": "%",
    "temperature.celsius": "°C",
    "temperature.kelvin": "K",
    "pressure.mmhg": "мм рт. ст.",
    "ppm": "ppm",
    "watt": "Вт",
    "volt": "В",
    "ampere": "А",
    "kilowatt_hour": "кВт·ч",
    "illumination.lux": "лк",
}


def _unit(unit: str | None) -> str:
    if not unit:
        return ""
    unit = unit.removeprefix("unit.")
    return UNITS.get(unit, unit)


def _ts(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime(" (%d.%m %H:%M)")


def _cap_params(cap: Capability) -> str:
    p = cap.parameters
    t = cap.short_type
    if t == "range":
        r = p.get("range", {})
        unit = _unit(p.get("unit"))
        return f"{r.get('min')}..{r.get('max')} шаг {r.get('precision')} {unit}".strip()
    if t == "color_setting":
        parts = []
        if p.get("color_model"):
            parts.append(f"цвет={p['color_model']}")
        if tk := p.get("temperature_k"):
            parts.append(f"температура={tk.get('min')}..{tk.get('max')}K")
        if scenes := (p.get("color_scene") or {}).get("scenes"):
            parts.append("сцены=" + ",".join(s.get("id", "?") for s in scenes))
        return " ".join(parts)
    if t == "mode":
        return "режимы=" + ",".join(m.get("value", "?") for m in p.get("modes", []))
    if t == "on_off" and p.get("split"):
        return "раздельные вкл/выкл"
    return ""


def _cap_line(cap: Capability) -> str:
    value = cap.state.value if cap.state else "—"
    params = _cap_params(cap)
    flags = ("R" if cap.retrievable else "-") + ("P" if cap.reportable else "-")
    line = f"    [умение]   {cap.short_type}.{cap.instance or '?'} = {value}{_ts(cap.last_updated)}"
    line += f"  [{flags}]"
    return line + (f"  — {params}" if params else "")


def _prop_line(prop: Property) -> str:
    value = prop.state.value if prop.state else "—"
    unit = f" {_unit(prop.unit)}" if prop.unit else ""
    extra = ""
    if prop.short_type == "event" and (events := prop.parameters.get("events")):
        extra = "  — события=" + ",".join(e.get("value", "?") for e in events)
    flags = ("R" if prop.retrievable else "-") + ("P" if prop.reportable else "-")
    return (
        f"    [свойство] {prop.short_type}.{prop.instance or '?'} = {value}{unit}"
        f"{_ts(prop.last_updated)}  [{flags}]{extra}"
    )


def format_device(dev: Device, info: UserInfo | None = None) -> str:
    room = info.room_name(dev.room) if info else dev.room
    lines = [f"  • {dev.name}  ({dev.short_type})  id={dev.id}"]
    meta = []
    if room:
        meta.append(f"комната: {room}")
    if dev.aliases:
        meta.append("синонимы: " + ", ".join(dev.aliases))
    if di := getattr(dev, "device_info", None):
        if isinstance(di, dict):
            meta.append(" ".join(str(v) for k, v in di.items() if k in ("manufacturer", "model") and v))
    if meta:
        lines.append("    " + "; ".join(m for m in meta if m))
    lines += [_cap_line(c) for c in dev.capabilities]
    lines += [_prop_line(p) for p in dev.properties]
    return "\n".join(lines)


def format_inventory(info: UserInfo) -> str:
    out = []
    for hh in info.households:
        out.append(f"Дом: {hh.name}  (id={hh.id})")
    out.append(
        f"Комнат: {len(info.rooms)}, устройств: {len(info.devices)}, "
        f"групп: {len(info.groups)}, сценариев: {len(info.scenarios)}"
    )
    out.append("Флаги: R — можно запросить состояние, P — устройство само сообщает об изменениях\n")

    by_room: dict[str | None, list[Device]] = {}
    for dev in info.devices:
        by_room.setdefault(dev.room, []).append(dev)
    for room_id, devices in by_room.items():
        out.append(f"== {info.room_name(room_id) or 'Без комнаты'} ==")
        out += [format_device(d, info) for d in devices]
        out.append("")

    if info.groups:
        out.append("== Группы ==")
        out += [f"  • {g.name}  id={g.id}  устройств: {len(g.devices)}" for g in info.groups]
        out.append("")
    if info.scenarios:
        out.append("== Сценарии Яндекса ==")
        out += [
            f"  • {s.name}  id={s.id}" + ("" if s.is_active else "  (выключен)")
            for s in info.scenarios
        ]
    return "\n".join(out).rstrip() + "\n"
