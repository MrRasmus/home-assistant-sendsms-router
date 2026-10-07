from __future__ import annotations
import hashlib
import asyncio
import logging
from datetime import timedelta, datetime
from logging import Logger
from collections.abc import Callable
from typing import Any, Type
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from tplinkrouterc6u import (
    VPN,
    TplinkRouterProvider,
    AbstractRouter,
    Firmware,
    Status,
    Connection,
    LTEStatus,
    SMS,
    ServingCell,
    VpnClientStatus,
    VPNStatus,
    PortStatus,
    IPv4Reservation,
)

try:
    from tplinkrouterc6u import MeshNode
except ImportError:  # pragma: no cover - older tplinkrouterc6u without mesh
    MeshNode = Any  # type: ignore[misc, assignment]
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from .const import (
    DOMAIN,
    DEFAULT_NAME,
    DEFAULT_SCAN_PAUSE,
    DEFAULT_OFFLINE_TIMEOUT,
)
from .utils import safe_call, is_retryable_error
from .sms_store import SmsLogEntry, SmsStore


def supports_led_control(router: AbstractRouter) -> bool:
    """Return True when the client exposes LED get/set methods (e.g. TL-SG108E)."""
    return hasattr(router, "led_status") and hasattr(router, "set_led")


def collect_mesh_nodes(router: AbstractRouter, logger: Logger) -> list[MeshNode] | None:
    """Return the EasyMesh node list, or None when this client can never provide one.

    None is the "stop asking" signal, matching how the other optional payloads in
    collect_status are gated. It covers both an older tplinkrouterc6u without the
    method and a client that implements the abstract default, so neither case logs
    a warning on every poll. A transient failure returns an empty list instead, so
    polling resumes on the next cycle.
    """
    getter = getattr(router, "get_mesh_nodes", None)
    if getter is None:
        return None
    try:
        return getter()
    except NotImplementedError:
        return None
    except Exception:
        logger.warning("TPLink Router failed to fetch mesh nodes", exc_info=True)
        return []


def collect_status(
        router: AbstractRouter,
        lte_status: LTEStatus | None,
        serving_cells: list[ServingCell] | None,
        vpn_server_status: VPNStatus | None,
        vpn_client_status: VpnClientStatus | None,
        port_status: list[PortStatus] | None,
        reservations: list[IPv4Reservation] | None,
        logger: Logger,
        mesh_nodes: list[MeshNode] | None = None,
        led_status: bool | None = None,
) -> tuple[Status, LTEStatus | None, list[ServingCell] | None, VPNStatus | None,
           VpnClientStatus | None, list[PortStatus] | None, list[SMS] | None,
           list[IPv4Reservation] | None, list[MeshNode] | None, bool | None]:
    """Gather all status data from the router; a failing SMS fetch must not break the update."""
    status = router.get_status()
    sms_list = None
    if lte_status is not None:
        lte_status = router.get_lte_status()
    if serving_cells is not None:
        serving_cells = router.get_lte_serving_cells()
    if vpn_server_status is not None:
        vpn_server_status = router.get_vpn_status()
    if vpn_client_status is not None:
        vpn_client_status = router.get_vpn_client_status()
    if port_status is not None:
        port_status = router.get_port_status()
    if mesh_nodes is not None:
        mesh_nodes = collect_mesh_nodes(router, logger)
    if hasattr(router, "get_sms") and lte_status is not None:
        sms_list = safe_call(router.get_sms, logger, "fetch SMS")
    if reservations is not None:
        reservations = safe_call(router.get_ipv4_reservations, logger, "fetch IPv4 reservations")
    if led_status is not None:
        led_status = safe_call(
            router.led_status, logger, "fetch LED status", level=logging.DEBUG
        )
    return (
        status,
        lte_status,
        serving_cells,
        vpn_server_status,
        vpn_client_status,
        port_status,
        sms_list,
        reservations,
        mesh_nodes,
        led_status,
    )


class TPLinkRouterCoordinator(DataUpdateCoordinator):
    def __init__(
            self,
            hass: HomeAssistant,
            router: AbstractRouter,
            update_interval: int,
            firmware: Firmware,
            status: Status,
            lte_status: LTEStatus | None,
            logger: Logger,
            unique_id: str,
            vpn_server_status: VPNStatus | None = None,
            vpn_client_status: VpnClientStatus | None = None,
            serving_cells: list[ServingCell] | None = None,
            port_status: list[PortStatus] | None = None,
            retries: int = 3,
            backoff_seconds: float = 1.0,
            scan_pause_minutes: int = DEFAULT_SCAN_PAUSE,
            offline_timeout_seconds: int = DEFAULT_OFFLINE_TIMEOUT,
            reservations: list[IPv4Reservation] | None = None,
            support_dhcp_reservations: bool = True,
            mesh_nodes: list[MeshNode] | None = None,
            led_status: bool | None = None,
            sms_store: SmsStore | None = None,
            sms_auto_mark_read: bool = True,
    ) -> None:
        self.router = router
        self.unique_id = unique_id
        self.status = status
        self.tracked = {}
        self.lte_status = lte_status
        self.serving_cells = serving_cells
        self.port_status = port_status
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.scan_pause_minutes = scan_pause_minutes
        self.offline_timeout_seconds = offline_timeout_seconds
        # list (incl. []) means "ask each poll"; None means the client cannot provide
        # a node list. Setup probes once so entities exist before the first interval.
        self.mesh_nodes: list[MeshNode] | None = mesh_nodes
        self.led_status: bool | None = led_status
        self.device_info = DeviceInfo(
            configuration_url=router.host,
            connections={(CONNECTION_NETWORK_MAC, self.status.lan_macaddr)},
            identifiers={(DOMAIN, self.status.lan_macaddr)},
            manufacturer="TPLink",
            model=firmware.model,
            name=DEFAULT_NAME,
            sw_version=firmware.firmware_version,
            hw_version=firmware.hardware_version,
        )

        self.vpn_server_status = vpn_server_status
        self.vpn_client_status = vpn_client_status
        self.reservations: list[IPv4Reservation] | None = reservations
        self.support_dhcp_reservations = support_dhcp_reservations
        self.sms_store = sms_store
        self.sms_auto_mark_read = sms_auto_mark_read

        self.scan_stopped_at: datetime | None = None
        self._last_update_time: datetime | None = None
        self._sms_hashes: set[str] = set()
        self.new_sms: list[SMS] = []
        self._lock = asyncio.Lock()

        super().__init__(
            hass,
            logger,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )

    @staticmethod
    async def get_client(hass: HomeAssistant, host: str, password: str, username: str, logger: Logger,
                         verify_ssl: bool) -> AbstractRouter:
        return await hass.async_add_executor_job(TplinkRouterProvider.get_client, host, password, username,
                                                 logger, verify_ssl)

    @staticmethod
    def get_client_by_class(client_class: str) -> Type[AbstractRouter]:
        return TplinkRouterProvider.get_clients()[client_class]

    @staticmethod
    def request(router: AbstractRouter, callback: Callable):
        router.authorize()
        try:
            return callback()
        finally:
            try:
                router.logout()
            except Exception:
                # Do not block updates if logout fails.
                pass

    async def _run_router_request(self, callback: Callable) -> Any:
        async with self._lock:
            return await self.hass.async_add_executor_job(
                TPLinkRouterCoordinator.request, self.router, callback
            )

    async def reboot(self) -> None:
        await self._run_router_request(self.router.reboot)

    async def set_wifi(self, wifi: Connection, enable: bool) -> None:
        def callback():
            self.router.set_wifi(wifi, enable)

        await self._run_router_request(callback)

    async def set_vpn_server(self, kind: VPN, enable: bool) -> None:
        def callback():
            self.router.set_vpn(kind, enable)

        await self._run_router_request(callback)

    async def set_vpn_client(self, enable: bool) -> None:
        def callback():
            self.router.set_vpn_client(enable)

        await self._run_router_request(callback)

    async def set_vpn_client_server(self, server_id, enable: bool) -> None:
        def callback():
            self.router.set_vpn_client_server(server_id, enable)

        await self._run_router_request(callback)

    async def set_vpn_client_device(self, mac: str, enable: bool) -> None:
        def callback():
            self.router.set_vpn_client_device(mac, enable)

        await self._run_router_request(callback)

    async def set_ipv4_dhcps(self, enable: bool) -> None:
        def callback():
            self.router.set_ipv4_dhcps(enable)

        await self._run_router_request(callback)

    async def set_ewan_connect(self, enable: bool) -> None:
        def callback():
            self.router.set_ewan_connect(enable)

        await self._run_router_request(callback)

    async def set_led(self, enable: bool) -> None:
        def callback():
            self.router.set_led(enable)

        await self._run_router_request(callback)

    async def add_ipv4_reservation(
        self, mac: str, ip: str, comment: str = "", enable: bool = True
    ) -> None:
        def callback():
            self.router.add_ipv4_reservation(mac, ip, comment, enable)

        await self._run_router_request(callback)
        await self.async_request_refresh()

    async def delete_ipv4_reservation(self, mac: str) -> None:
        def callback():
            self.router.delete_ipv4_reservation(mac)

        await self._run_router_request(callback)
        await self.async_request_refresh()

    async def send_sms(self, number: str, text: str) -> None:
        """Send an SMS and append successful sends to the persistent log."""
        def callback():
            self.router.send_sms(number, text)

        await self._run_router_request(callback)

        if self.sms_store is not None:
            try:
                await self.sms_store.async_add_message(
                    "out",
                    number,
                    text,
                )
            except Exception:
                self.logger.exception("TPLink Router failed to store sent SMS")

    async def clear_sms_log(self) -> None:
        """Clear the Home Assistant SMS history without touching the router inbox."""
        if self.sms_store is None:
            raise HomeAssistantError("SMS history is not available for this router")
        await self.sms_store.async_clear()

    async def mark_sms_read(self, message_id: str) -> None:
        """Mark a stored incoming SMS as read on the router."""
        await self._run_stored_sms_action(message_id, "set_sms_read", "mark as read")
        await self.async_request_refresh()

    async def delete_sms(self, message_id: str) -> None:
        """Delete a stored incoming SMS from the router inbox only."""
        await self._run_stored_sms_action(message_id, "delete_sms", "delete")
        await self.async_request_refresh()

    async def _run_stored_sms_action(
        self,
        message_id: str,
        router_method_name: str,
        action_name: str,
    ) -> None:
        """Locate a stored SMS in the current router inbox and run an action."""
        if self.sms_store is None:
            raise HomeAssistantError("SMS history is not available for this router")

        log_entry = self.sms_store.get_message(message_id)
        if log_entry is None:
            raise HomeAssistantError(f"SMS log entry {message_id} was not found")
        if log_entry["direction"] != "in":
            raise HomeAssistantError("Only received SMS messages exist in the router inbox")

        router_method = getattr(self.router, router_method_name, None)
        if router_method is None or not hasattr(self.router, "get_sms"):
            raise HomeAssistantError(f"This router does not support SMS {action_name}")

        def callback() -> bool:
            sms_list = self.router.get_sms()
            sms = self._find_router_sms(sms_list, log_entry)
            if sms is None:
                return False
            router_method(sms)
            return True

        if not await self._run_router_request(callback):
            raise HomeAssistantError(
                "The SMS is no longer present in the router's current inbox"
            )

    @staticmethod
    def _find_router_sms(sms_list: list[SMS], log_entry: SmsLogEntry) -> SMS | None:
        """Find the current router SMS represented by a persistent log entry."""
        router_hash = log_entry.get("router_hash")
        if router_hash:
            for sms in sms_list:
                if TPLinkRouterCoordinator._hash_item(sms) == router_hash:
                    return sms

        target_timestamp = SmsStore.normalize_timestamp(log_entry["timestamp"])
        for sms in sms_list:
            if sms.sender != log_entry["number"] or sms.content != log_entry["message"]:
                continue
            if SmsStore.normalize_timestamp(sms.received_at) == target_timestamp:
                return sms

        return None

    async def _async_process_new_sms(self) -> None:
        """Persist new SMS messages and optionally mark them read after saving."""
        if not self.new_sms or self.sms_store is None:
            return

        for sms in self.new_sms:
            try:
                await self.sms_store.async_add_message(
                    "in",
                    sms.sender,
                    sms.content,
                    sms.received_at,
                    router_hash=self._hash_item(sms),
                )
            except Exception:
                # Never mark a message read if we failed to persist it first.
                self.logger.exception("TPLink Router failed to store received SMS")
                continue

            if (
                not self.sms_auto_mark_read
                or not getattr(sms, "unread", False)
                or not hasattr(self.router, "set_sms_read")
            ):
                continue

            try:
                await self._run_router_request(
                    lambda sms=sms: self.router.set_sms_read(sms)
                )
            except Exception:
                # The SMS is already safe in our local store; a router-side read
                # failure should not fail the entire coordinator update.
                self.logger.warning(
                    "TPLink Router failed to mark received SMS as read",
                    exc_info=True,
                )

    async def _async_update_data(self):
        """Asynchronous update of all data."""
        retries = max(1, int(self.retries))
        last_error: Exception | None = None

        def update_once():
            return TPLinkRouterCoordinator.request(
                self.router,
                lambda: collect_status(
                    self.router,
                    self.lte_status,
                    self.serving_cells,
                    self.vpn_server_status,
                    self.vpn_client_status,
                    self.port_status,
                    self.reservations,
                    self.logger,
                    self.mesh_nodes,
                    self.led_status,
                ),
            )

        for attempt in range(retries):
            try:
                # Hold the router lock only for the authorize/request/logout cycle,
                # not for inter-attempt backoff, so switch/reboot/SMS can proceed.
                async with self._lock:
                    if self.scan_stopped_at is not None:
                        # scan_pause_minutes == 0 keeps fetching paused until turned
                        # back on manually (no automatic re-enable).
                        if (
                            self.scan_pause_minutes <= 0
                            or self.scan_stopped_at > (
                                datetime.now()
                                - timedelta(minutes=self.scan_pause_minutes)
                            )
                        ):
                            return
                        self.scan_stopped_at = None

                    (
                        self.status,
                        self.lte_status,
                        self.serving_cells,
                        self.vpn_server_status,
                        self.vpn_client_status,
                        self.port_status,
                        sms_list,
                        self.reservations,
                        self.mesh_nodes,
                        self.led_status,
                    ) = await self.hass.async_add_executor_job(update_once)

                if sms_list is not None:
                    self._process_sms_list(sms_list)
                    await self._async_process_new_sms()
                self._last_update_time = datetime.now()
                return
            except Exception as error:
                if not is_retryable_error(error):
                    raise
                last_error = error
                self.logger.warning(
                    "TPLink Router request attempt %s/%s failed: %s",
                    attempt + 1,
                    retries,
                    error,
                )
                if attempt < retries - 1:
                    await asyncio.sleep(self.backoff_seconds * (attempt + 1))

        if last_error is not None:
            raise last_error

    def _process_sms_list(self, sms_list: list[SMS]) -> None:
        current_hashes: set[str] = set()
        new_items: list[SMS] = []
        for sms in sms_list:
            h = TPLinkRouterCoordinator._hash_item(sms)
            current_hashes.add(h)
            if self._last_update_time is not None and h not in self._sms_hashes:
                new_items.append(sms)
        # Keep only hashes present in the current mailbox to avoid unbounded growth.
        self._sms_hashes = current_hashes
        self.new_sms = new_items

    @staticmethod
    def _hash_item(sms: SMS) -> str:
        key = f"{sms.sender}|{sms.content}|{sms.received_at.isoformat()}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()
