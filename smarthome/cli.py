"""Командная строка: python -m smarthome <команда>."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import yandex_token
from .inventory import format_device, format_inventory
from .yandex import YandexAPIError, YandexIoTClient, actions
from .yandex.models import UserInfo


async def _resolve(client: YandexIoTClient, name: str) -> tuple[UserInfo, str]:
    info = await client.user_info()
    dev = info.device(name)
    if not dev:
        names = ", ".join(d.name for d in info.devices)
        raise SystemExit(f"Устройство «{name}» не найдено. Есть: {names}")
    return info, dev.id


async def cmd_discover(client: YandexIoTClient, args: argparse.Namespace) -> None:
    raw = await client.user_info_raw()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(format_inventory(UserInfo.model_validate(raw)))
    print(f"Полный ответ API сохранён в {out}")


async def cmd_state(client: YandexIoTClient, args: argparse.Namespace) -> None:
    info, dev_id = await _resolve(client, args.device)
    dev = await client.device(dev_id)
    if args.json:
        print(dev.model_dump_json(indent=2))
    else:
        print(format_device(dev, info))


async def cmd_action(client: YandexIoTClient, args: argparse.Namespace) -> None:
    _, dev_id = await _resolve(client, args.device)
    c = args.cmd
    if c == "on":
        act = actions.on_off(True)
    elif c == "off":
        act = actions.on_off(False)
    elif c == "brightness":
        act = actions.brightness(args.value)
    elif c == "color":
        act = actions.color_rgb(args.value)
    elif c == "temp":
        act = actions.color_temperature(args.value)
    elif c == "scene":
        act = actions.color_scene(args.value)
    elif c == "range":
        act = actions.range_(args.instance, args.value, relative=args.relative)
    elif c == "mode":
        act = actions.mode(args.instance, args.value)
    elif c == "toggle":
        act = actions.toggle(args.instance, args.value in ("1", "true", "on", "да"))
    else:  # pragma: no cover
        raise SystemExit(f"Неизвестная команда {c}")
    await client.act(dev_id, act)
    print("OK")


async def cmd_scenario(client: YandexIoTClient, args: argparse.Namespace) -> None:
    info = await client.user_info()
    sc = next((s for s in info.scenarios if args.name in (s.id, s.name)), None)
    if not sc:
        raise SystemExit("Сценарий не найден. Есть: " + ", ".join(s.name for s in info.scenarios))
    await client.run_scenario(sc.id)
    print("OK")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smarthome", description="Управление умным домом")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="выгрузить все устройства, умения и свойства")
    d.add_argument("--out", default="data/yandex_user_info.json")
    d.set_defaults(func=cmd_discover)

    s = sub.add_parser("state", help="текущее состояние устройства")
    s.add_argument("device", help="имя или id устройства")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_state)

    for name in ("on", "off"):
        a = sub.add_parser(name, help=f"{'включить' if name == 'on' else 'выключить'} устройство")
        a.add_argument("device")
        a.set_defaults(func=cmd_action)

    for name, typ, hlp in (
        ("brightness", int, "яркость в процентах"),
        ("color", str, "цвет RRGGBB"),
        ("temp", int, "цветовая температура в K"),
        ("scene", str, "сцена освещения"),
    ):
        a = sub.add_parser(name, help=hlp)
        a.add_argument("device")
        a.add_argument("value", type=typ)
        a.set_defaults(func=cmd_action)

    r = sub.add_parser("range", help="любой диапазон: volume, channel, temperature...")
    r.add_argument("device")
    r.add_argument("instance")
    r.add_argument("value", type=float)
    r.add_argument("--relative", action="store_true", help="изменить относительно текущего")
    r.set_defaults(func=cmd_action)

    for name in ("mode", "toggle"):
        a = sub.add_parser(name, help=f"установить {name}")
        a.add_argument("device")
        a.add_argument("instance")
        a.add_argument("value")
        a.set_defaults(func=cmd_action)

    sc = sub.add_parser("scenario", help="запустить сценарий Яндекса")
    sc.add_argument("name")
    sc.set_defaults(func=cmd_scenario)
    return p


async def _run(args: argparse.Namespace) -> None:
    async with YandexIoTClient(yandex_token()) as client:
        await args.func(client, args)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (YandexAPIError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
