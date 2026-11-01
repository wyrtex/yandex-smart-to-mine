# Свой умный дом, совместимый с Яндексом

Цель: все устройства из «Дома с Алисой» доступны в собственной системе со всеми
умениями и свойствами, со своими сценариями и интерфейсом. При этом Алиса
продолжает ими управлять.

## Архитектура

```
              Нидерланды (VPS)                                 Дом (Россия)
 ┌──────────────────────────────────────┐          ┌──────────────────────────────────┐
 │  smarthome (ядро)                    │          │  мини-ПК / Raspberry Pi          │
 │   ├─ реестр устройств и состояний    │  MQTT по │   ├─ Zigbee-стик + Zigbee2MQTT   │
 │   ├─ движок сценариев                │◄────────►│   │   (датчики, выключатели)     │
 │   ├─ веб-интерфейс / API             │  туннелю │   └─ локальные критичные сценарии│
 │   ├─ адаптер Yandex IoT API ─────────┼──► облако Яндекса ──► лампы, ТВ (Wi-Fi)   │
 │   └─ навык умного дома для Алисы ◄───┼─── Алиса видит Zigbee-устройства, которые │
 └──────────────────────────────────────┘     переехали с хаба Яндекса             │
                                                   └──────────────────────────────────┘
```

**Почему нужен домашний мини-компьютер.** Zigbee-стик должен физически стоять
дома, рядом с устройствами. Пробрасывать сам Zigbee-радиопротокол до VPS через
интернет ненадёжно: протокол чувствителен к задержкам и потерям пакетов. Поэтому
дома работает Zigbee2MQTT, а с ядром на VPS он общается по MQTT, которому
кратковременные обрывы не страшны. Подойдёт Raspberry Pi 4/5, Orange Pi или любой
старый ноутбук.

**Каналы связи с устройствами:**

| Устройства | Как подключены | Задержка |
|---|---|---|
| Датчики открытия, температуры, выключатели (Zigbee) | свой стик → Zigbee2MQTT → MQTT | мгновенно, локально |
| Лампы, телевизор, станции (Wi-Fi) | облачный Yandex IoT API | ~0.3–1 с, через облако |
| Обратно в Алису | свой навык умного дома (нужен публичный HTTPS, его даёт VPS) | — |

## План работ

1. **[сделано] Адаптер Yandex IoT API**: чтение всех устройств, умений, свойств,
   комнат, групп и сценариев; управление устройствами; CLI.
2. Ядро: единая модель устройства (в терминах Яндекса: capabilities/properties),
   хранилище состояний и истории (SQLite → при необходимости PostgreSQL), шина событий.
3. Опрос Yandex API и отслеживание изменений состояний для облачных устройств.
4. Движок сценариев: триггеры (событие, время, состояние), условия, действия.
   Сценарии описываются в YAML/Python.
5. Веб-интерфейс и REST/WebSocket API.
6. Домашний мост: Zigbee2MQTT + MQTT-брокер + защищённый туннель до VPS.
   Перенос Zigbee-устройств с хаба Яндекса на свой стик.
7. Навык умного дома Яндекса (Provider API), чтобы перенесённые устройства снова
   появились в Алисе.

## Быстрый старт (этап 1)

### 1. Получить OAuth-токен Яндекса

1. Откройте <https://oauth.yandex.ru/client/new> и создайте приложение:
   - платформа: «Веб-сервисы», Redirect URI: `https://oauth.yandex.ru/verification_code`;
   - доступы: «Умный дом Яндекса»: **iot:view** (просмотр) и **iot:control** (управление).
2. Скопируйте ClientID и откройте в браузере
   `https://oauth.yandex.ru/authorize?response_type=token&client_id=<ClientID>`.
3. Подтвердите доступ и скопируйте токен (он действует примерно год).
4. `cp .env.example .env` и впишите токен в `YANDEX_IOT_TOKEN`.

Токен даёт полное управление домом: не коммитьте `.env` (он в `.gitignore`).

### 2. Установить и запустить

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

python -m smarthome discover          # все устройства со всеми умениями и свойствами
                                      # + полный ответ API в data/yandex_user_info.json
python -m smarthome state "Лампа"     # состояние одного устройства (--json для сырого вида)
python -m smarthome on "Лампа"
python -m smarthome brightness "Лампа" 40
python -m smarthome color "Лампа" FF8800
python -m smarthome temp "Лампа" 2700
python -m smarthome scene "Лампа" night
python -m smarthome range "Телевизор" volume -5 --relative
python -m smarthome toggle "Телевизор" mute on
python -m smarthome mode "Телевизор" input_source one
python -m smarthome scenario "Я дома"
pytest
```

### Использование из кода

```python
from smarthome.yandex import YandexIoTClient, actions

async with YandexIoTClient(token) as ya:
    info = await ya.user_info()
    door = info.device("Датчик двери")
    if door.property("open").state.value == "opened":
        lamp = info.device("Лампа")
        await ya.act(lamp.id, actions.on_off(True), actions.color_temperature(3000))
```

## Структура

```
smarthome/
  yandex/client.py   — клиент Yandex IoT API (чтение, команды, группы, сценарии)
  yandex/models.py   — модели: дом, комната, устройство, умение, свойство (неизвестные поля сохраняются)
  yandex/actions.py  — конструкторы команд (on_off, range, color_setting, mode, toggle)
  inventory.py       — читаемый отчёт об устройствах
  cli.py             — командная строка
tests/               — тесты на реалистичном примере ответа API
```
