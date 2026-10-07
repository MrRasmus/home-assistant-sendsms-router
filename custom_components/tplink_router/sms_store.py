"""Persistent SMS history storage for TP-Link Router."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Literal, TypedDict
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

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
        """Load SMS history from storage."""
        if self._loaded:
            return

        data = await self._store.async_load()
        if data is not None:
            self._messages = data.get("messages", [])[-self._max_messages :]

        self._loaded = True

    async def async_add_message(
        self,
        direction: Literal["in", "out"],
        number: str,
        message: str,
        timestamp: str | None = None,
    ) -> SmsLogEntry:
        """Add an SMS to history and persist it immediately."""
        await self.async_load()

        entry: SmsLogEntry = {
            "id": uuid4().hex,
            "direction": direction,
            "number": str(number),
            "message": str(message),
            "timestamp": timestamp or dt_util.utcnow().isoformat(),
        }
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

    async def async_clear(self) -> None:
        """Clear SMS history."""
        await self.async_load()
        self._messages.clear()
        await self._async_save()
        self._notify_listeners()

    async def _async_save(self) -> None:
        """Persist the current in-memory history."""
        await self._store.async_save({"messages": self._messages})

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
