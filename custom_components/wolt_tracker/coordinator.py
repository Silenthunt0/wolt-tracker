"""Adaptive poller that derives the "courier close" signal and watches the
support chat for messages from Wolt's agents."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WoltApi, WoltApiError, WoltAuthError
from .const import (
    CONF_ACTIVE_INTERVAL,
    CONF_CLOSE_DISTANCE,
    CONF_CLOSE_ETA,
    CONF_COURIER_INTERVAL,
    CONF_IDLE_INTERVAL,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_CLOSE_DISTANCE,
    DEFAULT_CLOSE_ETA,
    DEFAULT_COURIER_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    EVENT_RATING_PENDING,
    EVENT_SUPPORT_MESSAGE,
    RATING_API_OFFSET,
    RATING_PSEUDO_TAGS,
    RATING_SECTIONS,
    STATUS_DELIVERED,
    STATUS_READY,
    SUPPORT_STATUS_OPENED,
    SUPPORT_STATUS_RESOLVED,
    SUPPORT_TRANSCRIPT_LIMIT,
    TERMINAL_STATUSES,
)

_LOGGER = logging.getLogger(__name__)


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _mongo_date(value: Any) -> datetime | None:
    """Wolt serialises datetimes as {"$date": <epoch ms>}."""
    if isinstance(value, dict) and isinstance(value.get("$date"), (int, float)):
        return datetime.fromtimestamp(value["$date"] / 1000, tz=timezone.utc)
    return None


def _iso_date(value: Any) -> datetime | None:
    """Converse serialises datetimes as ISO 8601 with a Z suffix."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class WoltState:
    """What the entities read."""

    purchase_id: str | None = None
    status: str | None = None  # received/acknowledged/production/ready/delivered/rejected
    venue_name: str | None = None
    order_number: str | None = None
    item_count: int | None = None
    eta: datetime | None = None
    eta_min: datetime | None = None
    eta_max: datetime | None = None
    dropoff_lat: float | None = None
    dropoff_lon: float | None = None
    courier_lat: float | None = None
    courier_lon: float | None = None
    courier_heading: float | None = None
    courier_vehicle: str | None = None
    courier_delivering_mine: bool | None = None
    courier_distance_m: float | None = None
    courier_close: bool = False
    last_update: datetime | None = None

    # Support chat (the conversation we follow: the one about the active
    # order if there is one, else the most recently updated open one).
    support_conversation_id: str | None = None
    support_status: str | None = None  # opened / resolved / None
    support_purchase_id: str | None = None
    support_agent: str | None = None
    support_unread: bool = False
    support_unread_ids: list[str] = field(default_factory=list)
    support_message: str | None = None  # latest message in the conversation
    support_message_id: str | None = None
    support_message_from: str | None = None  # "agent" | "me"
    support_message_at: datetime | None = None
    support_transcript: list[dict[str, Any]] = field(default_factory=list)

    # Post-delivery rating: the newest delivered order Wolt still wants rated.
    rating_pending: bool = False
    rating_purchase_id: str | None = None
    rating_venue: str | None = None
    rating_ordered_at: datetime | None = None
    rating_template: dict[str, Any] | None = None  # {"food": {...}, "delivery": {...}}

    def copy_rating_from(self, other: WoltState) -> None:
        for name in (
            "rating_pending",
            "rating_purchase_id",
            "rating_venue",
            "rating_ordered_at",
            "rating_template",
        ):
            setattr(self, name, getattr(other, name))

    def copy_support_from(self, other: WoltState) -> None:
        for name in (
            "support_conversation_id",
            "support_status",
            "support_purchase_id",
            "support_agent",
            "support_unread",
            "support_unread_ids",
            "support_message",
            "support_message_id",
            "support_message_from",
            "support_message_at",
            "support_transcript",
        ):
            setattr(self, name, getattr(other, name))


class WoltCoordinator(DataUpdateCoordinator[WoltState]):
    """Slow when idle, fast while a courier is on the way."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: WoltApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_IDLE_INTERVAL),
        )
        self.api = api
        self.entry = entry
        self.apply_options(entry.options)
        self._purchase_id: str | None = None
        self._close_latched_for: str | None = None
        self._rating_templates: dict[str, dict[str, Any]] = {}  # purchase_id -> normalised template
        self._rating_templates_raw: dict[str, dict[str, Any]] = {}  # as Wolt sent it, for diagnostics

    def apply_options(self, options: dict[str, Any]) -> None:
        self.close_distance = float(options.get(CONF_CLOSE_DISTANCE, DEFAULT_CLOSE_DISTANCE))
        self.close_eta = timedelta(minutes=float(options.get(CONF_CLOSE_ETA, DEFAULT_CLOSE_ETA)))
        self._idle = timedelta(seconds=int(options.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)))
        self._active = timedelta(seconds=int(options.get(CONF_ACTIVE_INTERVAL, DEFAULT_ACTIVE_INTERVAL)))
        self._courier = timedelta(
            seconds=int(options.get(CONF_COURIER_INTERVAL, DEFAULT_COURIER_INTERVAL))
        )

    async def _async_update_data(self) -> WoltState:
        prev = self.data or WoltState()
        state = WoltState(last_update=datetime.now(tz=timezone.utc))
        try:
            await self._poll_order(state, prev)
        except WoltAuthError as err:
            # Marks entities unavailable and starts HA's re-authentication flow.
            raise ConfigEntryAuthFailed(str(err)) from err
        except WoltApiError as err:
            raise UpdateFailed(str(err)) from err
        # The chat is secondary: a failure there keeps the last chat state and
        # never takes the courier tracking down with it.
        try:
            await self._poll_support(state, prev)
        except WoltAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except WoltApiError as err:
            _LOGGER.warning("Wolt support chat poll failed: %s", err)
            state.copy_support_from(prev)
        # Same for the rating prompt.
        try:
            await self._poll_rating(state, prev)
        except WoltAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except WoltApiError as err:
            _LOGGER.warning("Wolt rating poll failed: %s", err)
            state.copy_rating_from(prev)
        return state

    # ---------------------------------------------------------------- order

    async def _poll_order(self, state: WoltState, prev: WoltState) -> None:
        # 1. Which order (if any) are we following?
        need_discovery = self._purchase_id is None or (
            prev.status in TERMINAL_STATUSES and prev.purchase_id == self._purchase_id
        )
        if need_discovery:
            active = await self.api.async_active_orders()
            self._purchase_id = active[0]["purchase_id"] if active else None
            if not active:
                self._close_latched_for = None
                self.update_interval = self._idle
                return

        assert self._purchase_id
        state.purchase_id = self._purchase_id

        # 2. Live tracking for that order.
        data = await self.api.async_tracking(self._purchase_id)
        od = data.get("order_details") or {}
        state.status = od.get("status")
        state.venue_name = od.get("venue_name")
        state.order_number = od.get("order_number")
        items = od.get("items")
        state.item_count = len(items) if isinstance(items, list) else None
        state.eta = _mongo_date(od.get("delivery_eta"))
        state.eta_min = _mongo_date(od.get("delivery_eta_min"))
        state.eta_max = _mongo_date(od.get("delivery_eta_max"))
        coords = ((od.get("delivery_location") or {}).get("coordinates") or {}).get("coordinates")
        if isinstance(coords, list) and len(coords) == 2:
            state.dropoff_lon, state.dropoff_lat = float(coords[0]), float(coords[1])

        drivers = [d for d in (data.get("drivers") or []) if isinstance(d, dict)]
        mine = [d for d in drivers if d.get("delivering_your_order")] or drivers
        if mine:
            d = mine[0]
            loc = d.get("location")
            if isinstance(loc, list) and len(loc) == 2:
                state.courier_lon, state.courier_lat = float(loc[0]), float(loc[1])
            state.courier_heading = d.get("heading")
            state.courier_vehicle = d.get("vehicle")
            state.courier_delivering_mine = bool(d.get("delivering_your_order"))
            if eta := _mongo_date(d.get("delivery_eta")):
                state.eta = eta
            if None not in (state.courier_lat, state.dropoff_lat):
                state.courier_distance_m = round(
                    haversine_m(state.courier_lon, state.courier_lat, state.dropoff_lon, state.dropoff_lat)
                )

        # 3. Derive "courier close" (latched per order until it ends).
        if state.status in TERMINAL_STATUSES:
            close = False
            self._close_latched_for = None
        else:
            close = self._close_latched_for == state.purchase_id
            if (
                not close
                and state.status == STATUS_READY
                and state.courier_delivering_mine
                and state.courier_distance_m is not None
            ):
                by_distance = state.courier_distance_m <= self.close_distance
                # A past ETA means "late", not "close"; only a near-future ETA counts.
                by_eta = (
                    state.eta is not None
                    and timedelta(0) <= (state.eta - state.last_update) <= self.close_eta
                )
                if by_distance or by_eta:
                    close = True
                    self._close_latched_for = state.purchase_id
                    _LOGGER.info(
                        "Wolt courier close: %s m, eta %s (order %s)",
                        state.courier_distance_m,
                        state.eta,
                        state.purchase_id,
                    )
        state.courier_close = close

        # 4. Poll rate for the next cycle.
        if state.status in TERMINAL_STATUSES:
            self.update_interval = self._idle
        elif state.status == STATUS_READY and state.courier_lat is not None:
            self.update_interval = self._courier
        else:
            self.update_interval = self._active

    # --------------------------------------------------------------- rating

    async def _poll_rating(self, state: WoltState, prev: WoltState) -> None:
        """Delivered orders stay "subscribed" until rated or dismissed; that
        is exactly the set the Wolt app shows the two-step rating for."""
        orders = await self.api.async_subscriptions()
        waiting = [
            o
            for o in orders
            if o.get("status") == STATUS_DELIVERED
            and o.get("subscribed", True)
            # Group orders are rated by the host only.
            and ((o.get("group") or {}).get("my_member") or {}).get("is_host") is not False
        ]
        if not waiting:
            # Rated (in the app or through the service) or dismissed: forget it.
            self._rating_templates.clear()
            return
        order = max(waiting, key=lambda o: ((_mongo_date(o.get("payment_time")) or datetime.min.replace(tzinfo=timezone.utc)), str(o["order_id"])))
        pid = str(order["order_id"])
        state.rating_pending = True
        state.rating_purchase_id = pid
        state.rating_venue = order.get("venue_name")
        state.rating_ordered_at = _mongo_date(order.get("payment_time"))

        template = self._rating_templates.get(pid)
        if template is None:
            try:
                raw = await self.api.async_review_template(pid)
                self._rating_templates_raw[pid] = raw
                template = self._normalise_template(raw)
                self._rating_templates[pid] = template
            except WoltApiError as err:
                # Still announce; the rate service re-fetches the template itself.
                _LOGGER.warning("Could not load the Wolt rating form for %s: %s", pid, err)
        state.rating_template = template

        if pid != prev.rating_purchase_id:
            _LOGGER.info("Wolt order %s (%s) is waiting for a rating", pid, state.rating_venue)
            self.hass.bus.async_fire(
                EVENT_RATING_PENDING,
                {
                    "purchase_id": pid,
                    "venue": state.rating_venue,
                    "ordered_at": state.rating_ordered_at.isoformat() if state.rating_ordered_at else None,
                    "template": template,
                },
            )

    async def async_rating_template(self, purchase_id: str) -> dict[str, Any]:
        """Normalised rating form for an order (cached per order)."""
        template = self._rating_templates.get(purchase_id)
        if template is None:
            raw = await self.api.async_review_template(purchase_id)
            self._rating_templates_raw[purchase_id] = raw
            template = self._normalise_template(raw)
            self._rating_templates[purchase_id] = template
        return template

    def rating_template_raw(self, purchase_id: str) -> dict[str, Any] | None:
        return self._rating_templates_raw.get(purchase_id)

    def forget_rating(self, purchase_id: str) -> None:
        self._rating_templates.pop(purchase_id, None)
        self._rating_templates_raw.pop(purchase_id, None)

    @staticmethod
    def _normalise_template(results: dict[str, Any]) -> dict[str, Any]:
        """Flatten Wolt's template into what a person (or an LLM) needs to
        pick a value: per section the five labels (value = position, 1..5)
        and which tags may accompany which value. Wolt numbers the smileys
        0..4 in attribution_options and in the review body; everything here
        is shifted to 1..5 and shifted back when sending."""
        out: dict[str, Any] = {}
        for section in RATING_SECTIONS:
            raw = results.get(section)
            if not isinstance(raw, dict):
                continue
            labels = [
                (r.get("label") or r.get("title") or "")
                for r in raw.get("ratings") or []
                if isinstance(r, dict)
            ]
            tags: dict[int, list[dict[str, str]]] = {}
            for opt in raw.get("attribution_options") or []:
                if not isinstance(opt, dict):
                    continue
                for value in opt.get("ratings") or []:
                    if isinstance(value, int):
                        tags.setdefault(value + RATING_API_OFFSET, []).extend(
                            {"key": t.get("key"), "name": t.get("name")}
                            for t in opt.get("tags") or []
                            if isinstance(t, dict) and t.get("key") and t["key"] not in RATING_PSEUDO_TAGS
                        )
            out[section] = {
                "title": raw.get("title"),
                "message": raw.get("message"),
                "variant_name": raw.get("variant_name"),
                "ratings": {i + 1: label for i, label in enumerate(labels)},
                "tags": tags,
            }
        return out

    # -------------------------------------------------------------- support

    def _read_by_me(self, message: dict[str, Any]) -> bool:
        me = self.api.contact_id
        for r in message.get("readBy") or []:
            if isinstance(r, dict) and r.get("type") == "contact" and (me is None or r.get("id") == me):
                return True
        return False

    @staticmethod
    def _sender(message: dict[str, Any]) -> str:
        return "me" if (message.get("from") or {}).get("type") == "contact" else "agent"

    @staticmethod
    def _body(message: dict[str, Any]) -> str:
        if message.get("deletedAt"):
            return "(message deleted)"
        return str(message.get("body") or "")

    async def _poll_support(self, state: WoltState, prev: WoltState) -> None:
        data = await self.api.async_support_conversations(SUPPORT_STATUS_OPENED)
        convs = [c for c in data["conversations"] if isinstance(c, dict) and c.get("id")]
        history = False
        if not convs and prev.support_conversation_id:
            # Nothing open. Keep the last conversation readable but mark it
            # closed; the "unread" signal only ever refers to an open chat.
            state.copy_support_from(prev)
            state.support_status = SUPPORT_STATUS_RESOLVED
            state.support_unread = False
            state.support_unread_ids = []
            return
        if not convs:
            # Nothing open and nothing remembered (first poll after start):
            # show the most recent closed chat so the sensor is readable.
            # History is never "unread" and never fires the event.
            data = await self.api.async_support_conversations(SUPPORT_STATUS_RESOLVED)
            convs = [c for c in data["conversations"] if isinstance(c, dict) and c.get("id")]
            history = True
            if not convs:
                return

        latest = {
            m.get("conversationId"): m
            for m in data.get("latestMessages") or []
            if isinstance(m, dict)
        }
        users = {u.get("id"): u for u in data.get("users") or [] if isinstance(u, dict)}

        # Prefer the chat about the order we follow, else the freshest one.
        conv = None
        if state.purchase_id:
            conv = next(
                (c for c in convs if (c.get("metadata") or {}).get("purchaseId") == state.purchase_id),
                None,
            )
        if conv is None:
            conv = max(convs, key=lambda c: str(c.get("updatedAt") or ""))
        cid = conv["id"]
        state.support_conversation_id = cid
        state.support_status = conv.get("status")
        state.support_purchase_id = (conv.get("metadata") or {}).get("purchaseId")
        assignee = next(
            (a for a in conv.get("assignees") or [] if isinstance(a, dict) and a.get("type") == "user"),
            None,
        )
        if assignee:
            state.support_agent = ((users.get(assignee.get("id")) or {}).get("name") or {}).get("firstName")

        last = latest.get(cid)
        if last:
            state.support_message_id = last.get("id")
            state.support_message = self._body(last)
            state.support_message_from = self._sender(last)
            state.support_message_at = _iso_date(last.get("createdAt"))
            state.support_unread = (
                state.support_message_from == "agent"
                and not last.get("deletedAt")
                and not self._read_by_me(last)
            )

        # Transcript: only re-fetch when something changed; it is the source
        # of the unread message ids the mark_read service needs.
        changed = (
            cid != prev.support_conversation_id
            or state.support_message_id != prev.support_message_id
            or not prev.support_transcript
        )
        if changed:
            mdata = await self.api.async_support_messages(cid)
            musers = {u.get("id"): u for u in mdata.get("users") or [] if isinstance(u, dict)}
            msgs = [m for m in mdata["messages"] if isinstance(m, dict) and m.get("id")]
            msgs.sort(key=lambda m: str(m.get("createdAt") or ""))
            transcript = []
            for m in msgs[-SUPPORT_TRANSCRIPT_LIMIT:]:
                sender = self._sender(m)
                name = None
                if sender == "agent":
                    name = ((musers.get((m.get("from") or {}).get("id")) or {}).get("name") or {}).get("firstName")
                transcript.append(
                    {
                        "id": m.get("id"),
                        "from": sender,
                        "name": name,
                        "body": self._body(m),
                        "at": m.get("createdAt"),
                        "read": self._read_by_me(m) if sender == "agent" else True,
                    }
                )
            state.support_transcript = transcript
            state.support_unread_ids = [
                m["id"]
                for m in msgs
                if self._sender(m) == "agent" and not m.get("deletedAt") and not self._read_by_me(m)
            ]
            if not state.support_agent and transcript:
                state.support_agent = next((t["name"] for t in reversed(transcript) if t["name"]), None)
        else:
            state.support_transcript = prev.support_transcript
            state.support_unread_ids = prev.support_unread_ids if state.support_unread else []
        if history:
            state.support_unread = False
            state.support_unread_ids = []

        # A new agent message that nobody has read yet: tell the automations once.
        if (
            state.support_unread
            and state.support_message_id
            and state.support_message_id != prev.support_message_id
        ):
            venue = state.venue_name if state.support_purchase_id == state.purchase_id else None
            _LOGGER.info("Wolt support message from %s (order %s)", state.support_agent, state.support_purchase_id)
            self.hass.bus.async_fire(
                EVENT_SUPPORT_MESSAGE,
                {
                    "conversation_id": cid,
                    "purchase_id": state.support_purchase_id,
                    "venue": venue,
                    "agent": state.support_agent,
                    "message": state.support_message,
                    "message_id": state.support_message_id,
                    "received_at": state.support_message_at.isoformat() if state.support_message_at else None,
                    "unread_count": len(state.support_unread_ids),
                },
            )
