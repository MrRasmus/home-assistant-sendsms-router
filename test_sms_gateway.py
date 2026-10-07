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




class TPLinkMR200Client:
    """Tiny MR-style router used to verify the web-UI read action sequence."""

    class ActItem:
        GET = 1
        SET = 2

        def __init__(self, type, oid, stack="0,0,0,0,0,0", pstack="0,0,0,0,0,0", attrs=None):
            self.type = type
            self.oid = oid
            self.stack = stack
            self.pstack = pstack
            self.attrs = [] if attrs is None else attrs

    def __init__(self, sms_list=None):
        self.sms_list = list(sms_list or [])
        self.req_act_calls = []
        self.public_set_sms_read_calls = []

    def get_sms(self):
        return list(self.sms_list)

    def set_sms_read(self, sms):
        self.public_set_sms_read_calls.append(sms)

    def req_act(self, acts):
        self.req_act_calls.append(acts)
        return "", {}


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


def test_received_sms_is_stored_then_marked_read_with_mr_webui_sequence():
    sms = _sms()
    message_hash = TPLinkRouterCoordinator._hash_item(sms)
    stored_entry = {
        "id": "local-1",
        "direction": "in",
        "number": sms.sender,
        "message": sms.content,
        "timestamp": "2026-10-07T20:37:26+00:00",
        "router_hash": message_hash,
    }
    store = Mock()
    store.async_add_message = AsyncMock(return_value=stored_entry)
    coord = _coordinator(store=store, auto_mark_read=True)
    coord.router = TPLinkMR200Client([sms])
    coord.new_sms = [sms]

    asyncio.run(coord._async_process_new_sms())

    store.async_add_message.assert_awaited_once_with(
        "in",
        sms.sender,
        sms.content,
        sms.received_at,
        router_hash=message_hash,
    )
    assert len(coord.router.req_act_calls) == 1
    acts = coord.router.req_act_calls[0]
    assert len(acts) == 2
    assert acts[0].type == acts[0].GET
    assert acts[0].oid == "LTE_SMS_RECVMSGBOX"
    assert acts[0].attrs == ["totalNumber", "amountPerPage"]
    assert acts[1].type == acts[1].SET
    assert acts[1].oid == "LTE_SMS_RECVMSGENTRY"
    assert acts[1].stack == "1,0,0,0,0,0"
    assert acts[1].attrs == ["unread=0"]
    assert coord.router.public_set_sms_read_calls == []


def test_mr_webui_mark_read_uses_get_and_set_in_one_req_act():
    sms = _sms(sms_id=3)
    coord = _coordinator(store=Mock())
    coord.router = TPLinkMR200Client([sms])

    coord._set_sms_read_router(sms)

    acts = coord.router.req_act_calls[0]
    assert [act.oid for act in acts] == [
        "LTE_SMS_RECVMSGBOX",
        "LTE_SMS_RECVMSGENTRY",
    ]
    assert acts[1].stack == "3,0,0,0,0,0"
    assert acts[1].attrs == ["unread=0"]


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
