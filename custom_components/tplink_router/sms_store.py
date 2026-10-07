"""Persistent SMS history storage for TP-Link Router."""

from __future__ import annotations

from copy import deepcopy
from typing import Literal, TypedDict
from uuid import uuid4

from homeassistant.core import HomeAssistant
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
        self._store: Store[dict] = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}.{entry_id}",
            private=True,
            atomic_writes=True,
        )

        self._max_messages = max_messages
        self._messages: list[SmsLogEntry] = []
        self._loaded = False

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
        """Add an SMS to history."""
        await self.async_load()

        entry: SmsLogEntry = {
            "id": uuid4().hex,
            "direction": direction,
            "number": number,
            "message": message,
            "timestamp": timestamp or dt_util.utcnow().isoformat(),
        }

        self._messages.append(entry)

        if len(self._messages) > self._max_messages:
            self._messages = self._messages[-self._max_messages :]

        await self._store.async_save(
            {
                "messages": self._messages,
            }
        )

        return entry

    async def async_get_messages(
        self,
        limit: int | None = None,
    ) -> list[SmsLogEntry]:
        """Return SMS history."""
        await self.async_load()

        messages = self._messages

        if limit is not None:
            messages = messages[-limit:]

        return deepcopy(messages)

    async def async_clear(self) -> None:
        """Clear SMS history."""
        await self.async_load()

        self._messages.clear()

        await self._store.async_save(
            {
                "messages": [],
            }
        )

    @property
    def count(self) -> int:
        """Return number of stored SMS messages."""
        return len(self._messages)