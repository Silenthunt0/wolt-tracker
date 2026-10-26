"""Thin client for the Wolt endpoints we need, with refresh-token rotation,
plus the support chat ("Converse") calls behind Wolt's support widget.

Wolt rotates the refresh token on every refresh and keeps only the newest one
alive, so each rotation is handed to `on_tokens` immediately for persisting,
and this client must be the sole user of its token chain (never share the
token with a browser session).

The support chat needs its own bearer token. It is obtained by exchanging the
current Wolt access token (one POST, no refresh chain involved), so a chat
problem can never invalidate the order-tracking login.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import (
    AUTH_URL,
    CONVERSE_APP_ID,
    CONVERSE_TOKEN_REFRESH_MARGIN,
    CONVERSE_TOKEN_URL,
    CONVERSE_WIDGET_URL,
    ORDER_STATUS_IN_PROGRESS,
    ORDERS_URL,
    REVIEW_TEMPLATE_URL,
    REVIEW_URL,
    SUBSCRIPTION_URL,
    SUBSCRIPTIONS_URL,
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

CONVERSE_HEADERS = {
    "x-converse-platform": "web",
    "x-converse-client": "wolt-consumer/1.16.152; conveyer-app/ha-wolt-tracker",
    "x-converse-language": "en-US",
    "origin": "https://wolt.com",
    "referer": "https://wolt.com/",
    "user-agent": API_HEADERS["user-agent"],
}

TIMEOUT = aiohttp.ClientTimeout(total=15)


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
        # Support chat
        self.contact_id: str | None = None  # my participant id in Converse
        self._converse_token: str | None = None
        self._converse_expires = 0.0
        self._converse_lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth

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
                timeout=TIMEOUT,
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
        return await self._request("GET", url, params=params)

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Authenticated call to Wolt's consumer APIs; one forced refresh on 401."""
        token = await self._ensure_token()
        for attempt in (0, 1):
            headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
            try:
                async with self._session.request(
                    method, url, headers=headers, params=params, json=json_body, timeout=TIMEOUT
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        # Access token revoked early: force one refresh and retry.
                        async with self._lock:
                            await self.async_refresh()
                            token = self.access_token or ""
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        raise WoltApiError(f"{method} {url} -> HTTP {resp.status}: {body[:200]}")
                    if resp.status == 204 or resp.content_length == 0:
                        return None
                    text = await resp.text()
                    if not text:
                        return None
                    try:
                        return json.loads(text)
                    except ValueError:
                        return text
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise WoltApiError(f"{method} {url} failed: {err}") from err
        raise WoltApiError(f"{method} {url} kept returning 401")

    # ---------------------------------------------------------------- orders

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

    # --------------------------------------------------------------- rating

    async def async_subscriptions(self) -> list[dict[str, Any]]:
        """Orders Wolt still considers "subscribed": in progress, or delivered
        and not yet rated / dismissed. Each has order_id, status, venue_name,
        subscribed and (for group orders) group.my_member.is_host."""
        data = await self._get(SUBSCRIPTIONS_URL)
        orders = data.get("order_details") if isinstance(data, dict) else None
        if not isinstance(orders, list):
            raise WoltApiError(f"subscriptions payload malformed: {str(data)[:200]}")
        return [o for o in orders if isinstance(o, dict) and o.get("order_id")]

    async def async_review_template(self, purchase_id: str) -> dict[str, Any]:
        """The rating form for a delivered order: {"food": {...}, "delivery": {...}}
        with title, message, variant_name, five ratings (index + 1 = value)
        and attribution_options (tags valid for given rating values)."""
        data = await self._get(REVIEW_TEMPLATE_URL.format(purchase_id=purchase_id))
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict) or not isinstance(results.get("food"), dict):
            raise WoltApiError(f"review template malformed: {str(data)[:200]}")
        return results

    async def async_review_submit(self, purchase_id: str, review: dict[str, Any]) -> None:
        """Post the rating, the same body shape the web app sends:
        {"food": {"rating", "variant_name", "attribution", "comment", "skipped": false},
         "delivery": {...}} or {"skipped": true, "variant_name", "attribution": []}."""
        await self._request("POST", REVIEW_URL.format(purchase_id=purchase_id), json_body=review)

    async def async_unsubscribe_order(self, purchase_id: str) -> None:
        """Close the rating prompt for an order (what the web app does after
        rating, or when the prompt is dismissed)."""
        await self._request("DELETE", SUBSCRIPTION_URL.format(purchase_id=purchase_id))

    # ---------------------------------------------------------- support chat

    async def _converse_exchange(self) -> str:
        """Trade the current Wolt access token for a Converse bearer token.

        Also (re)registers the contact, which is how we learn our own
        participant id; that id is what Wolt's read receipts refer to.
        """
        access = await self._ensure_token()
        headers = {**CONVERSE_HEADERS, "x-converse-request-id": str(uuid.uuid4())}
        try:
            async with self._session.put(
                f"{CONVERSE_WIDGET_URL}/contact",
                json={"idp": "wolt", "accessToken": access},
                headers=headers,
                timeout=TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    raise WoltApiError(f"converse contact registration -> HTTP {resp.status}")
                contact = await resp.json(content_type=None)
            async with self._session.post(
                CONVERSE_TOKEN_URL,
                json={
                    "grantType": "wolt_access_token",
                    "audience": "converse_widget",
                    "workspaceId": "wolt",
                    "appId": CONVERSE_APP_ID,
                    "accessToken": access,
                },
                headers=headers,
                timeout=TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    raise WoltApiError(f"converse token exchange -> HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WoltApiError(f"converse auth failed: {err}") from err
        contact_id = ((contact or {}).get("contact") or {}).get("id") if isinstance(contact, dict) else None
        if contact_id:
            self.contact_id = contact_id
        try:
            self._converse_token = payload["accessToken"]
            self._converse_expires = time.time() + float(payload.get("expiresIn", 1800))
        except (KeyError, TypeError) as err:
            raise WoltApiError(f"unexpected converse token payload: {str(payload)[:200]}") from err
        _LOGGER.debug("Converse token obtained, valid for %s s", payload.get("expiresIn"))
        return self._converse_token

    async def _ensure_converse(self, force: bool = False) -> str:
        async with self._converse_lock:
            if (
                force
                or not self._converse_token
                or time.time() > self._converse_expires - CONVERSE_TOKEN_REFRESH_MARGIN
            ):
                await self._converse_exchange()
            assert self._converse_token
            return self._converse_token

    async def _converse(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: Any = None,
        idempotent: bool = False,
    ) -> Any:
        token = await self._ensure_converse()
        url = f"{CONVERSE_WIDGET_URL}/{path}"
        for attempt in (0, 1):
            headers = {
                **CONVERSE_HEADERS,
                "Authorization": f"Bearer {token}",
                "x-converse-request-id": str(uuid.uuid4()),
            }
            if idempotent:
                headers["Idempotency-Key"] = str(uuid.uuid4())
            try:
                async with self._session.request(
                    method, url, params=params, json=json_body, headers=headers, timeout=TIMEOUT
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        token = await self._ensure_converse(force=True)
                        continue
                    if resp.status not in (200, 201, 204):
                        body = await resp.text()
                        raise WoltApiError(f"{method} {path} -> HTTP {resp.status}: {body[:200]}")
                    if resp.status == 204:
                        return None
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise WoltApiError(f"{method} {path} failed: {err}") from err
        raise WoltApiError(f"{method} {path} kept returning 401")

    async def async_support_conversations(self, status: str = "opened") -> dict[str, Any]:
        """Support conversations with the given status, plus their latest
        message (with read receipts) and the agents involved."""
        data = await self._converse(
            "GET", "conversations", params=[("status", status), ("types", "support")]
        )
        if not isinstance(data, dict) or not isinstance(data.get("conversations"), list):
            raise WoltApiError(f"conversations payload malformed: {str(data)[:200]}")
        return data

    async def async_support_messages(self, conversation_id: str) -> dict[str, Any]:
        data = await self._converse(
            "GET",
            f"conversations/{conversation_id}/messages",
            params=[("types", "regular"), ("types", "widget")],
        )
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise WoltApiError(f"messages payload malformed: {str(data)[:200]}")
        return data

    async def async_support_send(self, conversation_id: str, text: str) -> Any:
        """Post a message from the customer into a support conversation."""
        return await self._converse(
            "POST",
            f"conversations/{conversation_id}/messages",
            json_body={"message": {"body": text, "attachments": []}},
            idempotent=True,
        )

    async def async_support_mark_read(self, conversation_id: str, message_ids: list[str]) -> Any:
        if not message_ids:
            return None
        return await self._converse(
            "POST",
            f"conversations/{conversation_id}/messages/readBy",
            json_body={"messageIds": message_ids},
        )
