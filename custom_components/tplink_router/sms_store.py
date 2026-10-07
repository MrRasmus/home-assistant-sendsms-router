"""Persistent SMS history storage for TP-Link Router."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal, NotRequired, TypedDict
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "tplink_router.sms_log"
DEFAULT_MAX_MESSAGES = 500


class SmsLogEntry(TypedDict):
    """Represent one SMS log entry."""

    id: str
    direction: Literal["in", "out"]
    number: str
    message: str
    timestamp: str
    router_hash: NotRequired[str]


class SmsStore:
    """Store SMS history persistently in Home Assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        """Initialize the SMS store."""
        self._store: Store[dict[str, list[SmsLogEntry]]] = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        self._max_messages = max_messages
        self._messages: list[SmsLogEntry] = []
        self._loaded = False
        self._listeners: set[Callable[[], None]] = set()

    async def async_load(self) -> None:
        """Load SMS history from storage and normalize old timestamps to UTC."""
        if self._loaded:
            return

        data = await self._store.async_load()
        changed = False
        loaded_messages: list[SmsLogEntry] = []

        if data is not None:
            for item in data.get("messages", [])[-self._max_messages :]:
                entry = dict(item)
                timestamp = self.normalize_timestamp(entry.get("timestamp"))
                if timestamp != entry.get("timestamp"):
                    entry["timestamp"] = timestamp
                    changed = True
                loaded_messages.append(entry)

        self._messages = loaded_messages
        self._loaded = True

        if changed:
            await self._async_save()

    async def async_add_message(
        self,
        direction: Literal["in", "out"],
        number: str,
        message: str,
        timestamp: str | datetime | None = None,
        router_hash: str | None = None,
    ) -> SmsLogEntry:
        """Add an SMS to history and persist it immediately."""
        await self.async_load()

        entry: SmsLogEntry = {
            "id": uuid4().hex,
            "direction": direction,
            "number": str(number),
            "message": str(message),
            "timestamp": self.normalize_timestamp(timestamp),
        }
        if router_hash is not None:
            entry["router_hash"] = router_hash

        self._messages.append(entry)

        if len(self._messages) > self._max_messages:
            self._messages = self._messages[-self._max_messages :]

        await self._async_save()
        self._notify_listeners()
        return deepcopy(entry)

    async def async_get_messages(
        self,
        limit: int | None = None,
    ) -> list[SmsLogEntry]:
        """Return a copy of SMS history."""
        await self.async_load()
        return self.get_messages(limit)

    def get_messages(self, limit: int | None = None) -> list[SmsLogEntry]:
        """Return a copy of the already-loaded SMS history."""
        messages = self._messages if limit is None else self._messages[-limit:]
        return deepcopy(messages)

    def get_message(self, message_id: str) -> SmsLogEntry | None:
        """Return one stored SMS by its local log id."""
        for message in self._messages:
            if message["id"] == message_id:
                return deepcopy(message)
        return None

    async def async_clear(self) -> None:
        """Clear SMS history."""
        await self.async_load()
        self._messages.clear()
        await self._async_save()
        self._notify_listeners()

    async def _async_save(self) -> None:
        """Persist the current in-memory history."""
        await self._store.async_save({"messages": self._messages})

    @staticmethod
    def normalize_timestamp(value: str | datetime | None) -> str:
        """Return a timezone-aware UTC ISO timestamp.

        TP-Link MR routers may return receivedTime without timezone information.
        The tested MR200 firmware reports that value in UTC, so naive timestamps
        are interpreted as UTC before being persisted.
        """
        if value is None:
            timestamp = datetime.now(UTC)
        elif isinstance(value, datetime):
            timestamp = value
        else:
            try:
                timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return str(value)

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        return timestamp.astimezone(UTC).isoformat()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for SMS history changes."""
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Notify listeners that the SMS history changed."""
        for listener in tuple(self._listeners):
            listener()

    @property
    def count(self) -> int:
        """Return number of stored SMS messages."""
        return len(self._messages)
