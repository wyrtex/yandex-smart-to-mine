"""Асинхронный клиент официального Yandex Smart Home IoT API.

Документация: https://yandex.ru/dev/dialogs/smart-home/doc/concepts/platform-user-info.html
Нужен OAuth-токен с правами iot:view (чтение) и iot:control (управление).
"""

from __future__ import annotations

from typing import Any

import httpx

from .actions import Action
from .models import ActionResult, Device, UserInfo

BASE_URL = "https://api.iot.yandex.net"


class YandexAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class YandexIoTClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not token:
            raise ValueError("Не задан OAuth-токен Яндекса (YANDEX_IOT_TOKEN)")
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    async def __aenter__(self) -> "YandexIoTClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # --- низкоуровневые запросы -------------------------------------------

    async def _request(self, method: str, path: str, json: Any = None) -> dict[str, Any]:
        resp = await self._http.request(method, path, json=json)
        try:
            data = resp.json()
        except ValueError:
            data = None

        if resp.status_code == 401:
            raise YandexAPIError(
                "Токен недействителен или не имеет прав iot:view / iot:control",
                status_code=401,
                payload=data,
            )
        if resp.status_code >= 400 or not isinstance(data, dict) or data.get("status") != "ok":
            message = (data or {}).get("message") if isinstance(data, dict) else None
            raise YandexAPIError(
                f"Ошибка API Яндекса ({resp.status_code}): {message or resp.text[:300]}",
                status_code=resp.status_code,
                payload=data,
            )
        return data

    # --- чтение -------------------------------------------------------------

    async def user_info_raw(self) -> dict[str, Any]:
        """Полный сырой ответ /v1.0/user/info: дома, комнаты, группы, устройства, сценарии."""
        return await self._request("GET", "/v1.0/user/info")

    async def user_info(self) -> UserInfo:
        return UserInfo.model_validate(await self.user_info_raw())

    async def device(self, device_id: str) -> Device:
        """Актуальное состояние одного устройства."""
        return Device.model_validate(await self._request("GET", f"/v1.0/devices/{device_id}"))

    # --- управление ---------------------------------------------------------

    async def act(self, device_id: str, *actions: Action) -> ActionResult:
        """Отправить одному устройству одно или несколько действий."""
        return await self.act_many({device_id: list(actions)})

    async def act_many(self, commands: dict[str, list[Action]]) -> ActionResult:
        """Отправить действия нескольким устройствам одним запросом."""
        body = {"devices": [{"id": dev_id, "actions": acts} for dev_id, acts in commands.items()]}
        result = ActionResult.model_validate(
            await self._request("POST", "/v1.0/devices/actions", json=body)
        )
        _raise_on_failed_actions(result)
        return result

    async def act_group(self, group_id: str, *actions: Action) -> dict[str, Any]:
        return await self._request(
            "POST", f"/v1.0/groups/{group_id}/actions", json={"actions": list(actions)}
        )

    async def run_scenario(self, scenario_id: str) -> dict[str, Any]:
        """Запустить сценарий, созданный в приложении «Дом с Алисой»."""
        return await self._request("POST", f"/v1.0/scenarios/{scenario_id}/actions")


def _raise_on_failed_actions(result: ActionResult) -> None:
    errors = []
    for dev in result.devices:
        for cap in dev.get("capabilities", []):
            ar = (cap.get("state") or {}).get("action_result") or {}
            if ar.get("status") == "ERROR":
                errors.append(
                    f"{dev.get('id')}: {cap.get('type')} -> "
                    f"{ar.get('error_code')} {ar.get('error_message', '')}".strip()
                )
    if errors:
        raise YandexAPIError("Устройство не выполнило команду: " + "; ".join(errors), payload=result)
