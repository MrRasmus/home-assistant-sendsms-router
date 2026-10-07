"""Tests for the TP-Link Router SMS gateway extensions."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.tplink_router.coordinator import TPLinkRouterCoordinator
from custom_components.tplink_router.sms_store import SmsStore


class FakeStorage:
    """Minimal async storage stand-in."""

    def __init__(self, data=None):
        self.data = deepcopy(data)
        self.saved = None

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        self.saved = deepcopy(data)
        self.data = deepcopy(data)


def _sms(*, sms_id=1, sender="+4512345678", content="PING", unread=True):
    return SimpleNamespace(
        id=sms_id,
        sender=sender,
        content=content,
        received_at=datetime(2026, 10, 7, 20, 37, 26),
        unread=unread,
    )


def _coordinator(*, store=None, auto_mark_read=True):
    coord = TPLinkRouterCoordinator.__new__(TPLinkRouterCoordinator)
    coord.sms_store = store
    coord.sms_auto_mark_read = auto_mark_read
    coord.router = Mock()
    coord.logger = logging.getLogger("test_sms_gateway")
    coord.new_sms = []

    async def run_request(callback):
        return callback()

    coord._run_router_request = run_request
    coord.async_request_refresh = AsyncMock()
    return coord


def test_store_normalizes_old_naive_timestamp_to_utc():
    storage = FakeStorage(
        {
            "messages": [
                {
                    "id": "old",
                    "direction": "in",
                    "number": "+4512345678",
                    "message": "old",
                    "timestamp": "2026-10-07T20:37:26",
                }
            ]
        }
    )

    with patch("custom_components.tplink_router.sms_store.Store", return_value=storage):
        store = SmsStore(Mock(), "entry-1")
        asyncio.run(store.async_load())

    assert store.get_messages()[0]["timestamp"] == "2026-10-07T20:37:26+00:00"
    assert storage.saved is not None


def test_store_adds_utc_timestamp_and_router_hash():
    storage = FakeStorage()

    with patch("custom_components.tplink_router.sms_store.Store", return_value=storage):
        store = SmsStore(Mock(), "entry-1")
        entry = asyncio.run(
            store.async_add_message(
                "in",
                "+4512345678",
                "PING",
                datetime(2026, 10, 7, 20, 37, 26),
                router_hash="abc123",
            )
        )

    assert entry["timestamp"] == "2026-10-07T20:37:26+00:00"
    assert entry["router_hash"] == "abc123"
    assert storage.saved["messages"][0]["id"] == entry["id"]


def test_received_sms_is_stored_then_marked_read():
    store = Mock()
    store.async_add_message = AsyncMock()
    coord = _coordinator(store=store, auto_mark_read=True)
    sms = _sms()
    coord.new_sms = [sms]
    coord.router.set_sms_read = Mock()

    asyncio.run(coord._async_process_new_sms())

    store.async_add_message.assert_awaited_once_with(
        "in",
        sms.sender,
        sms.content,
        sms.received_at,
        router_hash=coord._hash_item(sms),
    )
    coord.router.set_sms_read.assert_called_once_with(sms)


def test_auto_mark_read_can_be_disabled():
    store = Mock()
    store.async_add_message = AsyncMock()
    coord = _coordinator(store=store, auto_mark_read=False)
    sms = _sms()
    coord.new_sms = [sms]
    coord.router.set_sms_read = Mock()

    asyncio.run(coord._async_process_new_sms())

    store.async_add_message.assert_awaited_once()
    coord.router.set_sms_read.assert_not_called()


def test_sent_sms_is_logged_only_after_router_send_succeeds():
    store = Mock()
    store.async_add_message = AsyncMock()
    coord = _coordinator(store=store)
    coord.router.send_sms = Mock()

    asyncio.run(coord.send_sms("+4512345678", "hello"))

    coord.router.send_sms.assert_called_once_with("+4512345678", "hello")
    store.async_add_message.assert_awaited_once_with("out", "+4512345678", "hello")


def test_delete_sms_finds_current_router_message_by_hash():
    sms = _sms()
    message_hash = TPLinkRouterCoordinator._hash_item(sms)
    store = Mock()
    store.get_message.return_value = {
        "id": "local-1",
        "direction": "in",
        "number": sms.sender,
        "message": sms.content,
        "timestamp": "2026-10-07T20:37:26+00:00",
        "router_hash": message_hash,
    }
    coord = _coordinator(store=store)
    coord.router.get_sms = Mock(return_value=[sms])
    coord.router.delete_sms = Mock()

    asyncio.run(coord.delete_sms("local-1"))

    coord.router.delete_sms.assert_called_once_with(sms)
    coord.async_request_refresh.assert_awaited_once()


def test_router_action_rejects_outgoing_log_entry():
    store = Mock()
    store.get_message.return_value = {
        "id": "local-out",
        "direction": "out",
        "number": "+4512345678",
        "message": "hello",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    coord = _coordinator(store=store)
    coord.router.delete_sms = Mock()
    coord.router.get_sms = Mock(return_value=[])

    with pytest.raises(HomeAssistantError, match="Only received SMS"):
        asyncio.run(coord.delete_sms("local-out"))


def test_clear_sms_log_does_not_touch_router():
    store = Mock()
    store.async_clear = AsyncMock()
    coord = _coordinator(store=store)

    asyncio.run(coord.clear_sms_log())

    store.async_clear.assert_awaited_once()
    coord.router.assert_not_called()
