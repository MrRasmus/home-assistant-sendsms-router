# SMS Gateway v0.3 test plan

## Added in v0.3

- `Automatically mark received SMS as read` integration option, default ON.
- `tplink_router.mark_sms_read` service.
- `tplink_router.delete_sms` service. This removes the message from the router inbox but keeps the Home Assistant log entry.
- `tplink_router.clear_sms_log` service. This clears only Home Assistant's persistent SMS history.
- Incoming and outgoing log timestamps are normalized to timezone-aware UTC ISO timestamps.
- New incoming log entries keep a stable `router_hash`, so a later mark/delete service can locate the same message even if the router's inbox index changes.
- SMS Log exposes `last_id` and `auto_mark_read` attributes.

## First test after restart

1. Confirm the integration loads and `sensor.network_tp_link_router_sms_log` is available.
2. Open TP-Link Router -> Configure and confirm `Automatically mark received SMS as read after it is stored` exists and is ON.
3. Send one SMS from Home Assistant and confirm it appears as `direction: out` in SMS Log.
4. Send one SMS to the MR200 and confirm it appears as `direction: in`, then verify Unread SMS returns to 0.
5. Confirm new timestamps end in `+00:00`.
6. Copy the `id` of a received SMS from the `messages` attribute and test `tplink_router.mark_sms_read` with that `message_id`.
7. Send another received SMS, copy its local `id`, call `tplink_router.delete_sms`, and verify it disappears from the MR200 inbox while remaining in the Home Assistant SMS log.
8. Call `tplink_router.clear_sms_log` and verify SMS Log becomes 0 without deleting messages from the MR200.
9. Restart Home Assistant and confirm any remaining log entries persist.

## Important

`delete_sms` works only for received SMS messages still present on the router's current SMS inbox page. The local Home Assistant log uses its own stable UUID; the integration resolves that UUID back to the matching current router SMS before calling the TP-Link API.
