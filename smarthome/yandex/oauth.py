"""Вход в аккаунт Яндекса через OAuth (код подтверждения) и обновление токена."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

OAUTH_URL = "https://oauth.yandex.ru"


class OAuthError(RuntimeError):
    pass


def authorize_url(client_id: str, response_type: str = "code") -> str:
    """Ссылка для входа. После подтверждения Яндекс покажет на странице verification_code
    код (response_type=code) или сразу готовый токен (response_type=token)."""
    return f"{OAUTH_URL}/authorize?" + urlencode(
        {"response_type": response_type, "client_id": client_id, "force_confirm": "yes"}
    )


def looks_like_token(value: str) -> bool:
    """Код подтверждения — 7 цифр, токен — длинная строка (обычно начинается с y0_)."""
    v = value.strip()
    return len(v) > 20 and not v.isdigit()


async def _token_request(data: dict[str, str], transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=OAUTH_URL, timeout=15, transport=transport) as http:
        resp = await http.post("/token", data=data)
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or "access_token" not in body:
        raise OAuthError(body.get("error_description") or body.get("error") or resp.text[:200])
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_at": time.time() + float(body.get("expires_in", 365 * 86400)),
    }


async def exchange_code(
    code: str, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    return await _token_request(
        {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": client_id,
            "client_secret": client_secret,
        },
        transport,
    )


async def refresh(
    refresh_token: str, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    return await _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        transport,
    )


async def check_client(
    client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[bool, str]:
    """Проверка пары ClientID/секрет: обмениваем заведомо неверный код.

    invalid_grant — Яндекс узнал приложение и секрет (ключи верны),
    invalid_client — неверный ClientID или секрет.
    """
    async with httpx.AsyncClient(base_url=OAUTH_URL, timeout=15, transport=transport) as http:
        resp = await http.post("/token", data={
            "grant_type": "authorization_code", "code": "0000000",
            "client_id": client_id, "client_secret": client_secret,
        })
    try:
        err = resp.json().get("error", "")
    except ValueError:
        err = ""
    if err == "invalid_grant":
        return True, "Яндекс принял ClientID и секрет"
    if err == "invalid_client":
        return False, "Яндекс не принял ClientID или секрет — проверьте .env на сервере"
    return False, f"Неожиданный ответ Яндекса: {resp.status_code} {resp.text[:200]}"
