"""Конструкторы команд для POST /v1.0/devices/actions.

Каждая функция возвращает одно действие в формате API:
    {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}
"""

from __future__ import annotations

from typing import Any

Action = dict[str, Any]

CAP = "devices.capabilities."


def _action(cap: str, instance: str, value: Any, **extra: Any) -> Action:
    state: dict[str, Any] = {"instance": instance, "value": value}
    state.update(extra)
    return {"type": CAP + cap, "state": state}


def on_off(on: bool) -> Action:
    return _action("on_off", "on", bool(on))


def brightness(percent: int, relative: bool = False) -> Action:
    return range_("brightness", percent, relative)


def range_(instance: str, value: float, relative: bool = False) -> Action:
    """Диапазон: brightness, temperature, volume, channel, humidity, open."""
    extra = {"relative": True} if relative else {}
    return _action("range", instance, value, **extra)


def color_hsv(h: int, s: int, v: int) -> Action:
    return _action("color_setting", "hsv", {"h": h, "s": s, "v": v})


def color_rgb(rgb: int | str) -> Action:
    """rgb как число 0xRRGGBB или строка 'RRGGBB' / '#RRGGBB'."""
    if isinstance(rgb, str):
        rgb = int(rgb.lstrip("#"), 16)
    return _action("color_setting", "rgb", rgb)


def color_temperature(kelvin: int) -> Action:
    return _action("color_setting", "temperature_k", kelvin)


def color_scene(scene: str) -> Action:
    """Сцена освещения: например 'night', 'reading', 'party', 'alarm'."""
    return _action("color_setting", "scene", scene)


def mode(instance: str, value: str) -> Action:
    """Режим: например mode('program', 'auto') или mode('input_source', 'one')."""
    return _action("mode", instance, value)


def toggle(instance: str, value: bool) -> Action:
    """Переключатель: mute, pause, backlight, oscillation, ionization..."""
    return _action("toggle", instance, bool(value))
