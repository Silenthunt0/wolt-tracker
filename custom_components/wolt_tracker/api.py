"""Thin client for the three Wolt endpoints we need, with refresh-token rotation.

Wolt rotates the refresh token on every refresh and keeps only the newest one
alive, so each rotation is handed to `on_tokens` immediately for persisting,
and this client must be the sole user of its token chain (never share the
token with a browser session).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import (
    AUTH_URL,
    ORDER_STATUS_IN_PROGRESS,
    ORDERS_URL,
    TOKEN_REFRESH_MARGIN,
    TRACKING_URL,
)

_LOGGER = logging.getLogger(__name__)

API_HEADERS = {
    "platform": "Web",
    "client-version": "1.16.150",
    "clientversionnumber": "1.16.150",
    "app-language": "en",
    "origin": "https://wolt.com",
    "referer": "https://wolt.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
}


class WoltAuthError(Exception):
    """The refresh token was rejected; a new login is needed."""


class WoltApiError(Exception):
    """Transient or unexpected API failure."""


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (we only read the user id)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


class WoltApi:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        access_token: str | None = None,
        expires_at: float = 0.0,
        on_tokens: Callable[[str, str, float], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.expires_at = expires_at
        self._on_tokens = on_tokens
        self._lock = asyncio.Lock()

    async def async_refresh(self) -> None:
        """Exchange the refresh token for a new access + refresh token."""
        if not self.refresh_token:
            raise WoltAuthError("no refresh token")
        data = {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
        try:
            async with self._session.post(
                AUTH_URL,
                data=data,
                headers={"origin": "https://wolt.com"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (400, 401, 403):
                    body = await resp.text()
                    raise WoltAuthError(f"refresh rejected ({resp.status}): {body[:200]}")
                if resp.status != 200:
                    raise WoltApiError(f"refresh failed with HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WoltApiError(f"refresh request failed: {err}") from err
        try:
            self.access_token = payload["access_token"]
            self.refresh_token = payload["refresh_token"]
            self.expires_at = time.time() + float(payload.get("expires_in", 1800))
        except (KeyError, TypeError) as err:
            raise WoltApiError(f"unexpected refresh payload: {payload!r}") from err
        _LOGGER.debug("Wolt access token refreshed, valid for %s s", payload.get("expires_in"))
        if self._on_tokens:
            await self._on_tokens(self.refresh_token, self.access_token, self.expires_at)

    async def _ensure_token(self) -> str:
        async with self._lock:
            if not self.access_token or time.time() > self.expires_at - TOKEN_REFRESH_MARGIN:
                await self.async_refresh()
            assert self.access_token
            return self.access_token

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        token = await self._ensure_token()
        for attempt in (0, 1):
            headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
            try:
                async with self._session.get(
                    url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        # Access token revoked early: force one refresh and retry.
                        async with self._lock:
                            await self.async_refresh()
                            token = self.access_token or ""
                        continue
                    if resp.status != 200:
                        raise WoltApiError(f"GET {url} -> HTTP {resp.status}")
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise WoltApiError(f"GET {url} failed: {err}") from err
        raise WoltApiError(f"GET {url} kept returning 401")

    async def async_active_orders(self) -> list[dict[str, Any]]:
        """In-progress orders from the orders page, newest first."""
        data = await self._get(ORDERS_URL, params={"limit": 5})
        orders = data.get("orders") if isinstance(data, dict) else None
        if not isinstance(orders, list):
            raise WoltApiError(f"orders payload has no list: {str(data)[:200]}")
        return [
            o
            for o in orders
            if isinstance(o, dict)
            and (o.get("telemetry") or {}).get("order_status_type") == ORDER_STATUS_IN_PROGRESS
            and o.get("purchase_id")
        ]

    async def async_tracking(self, purchase_id: str) -> dict[str, Any]:
        data = await self._get(TRACKING_URL.format(purchase_id=purchase_id))
        if not isinstance(data, dict) or "order_details" not in data:
            raise WoltApiError(f"tracking payload malformed: {str(data)[:200]}")
        return data
