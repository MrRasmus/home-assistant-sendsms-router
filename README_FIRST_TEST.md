# TP-Link Router SMS Gateway - first test build

This test build adds:

- Persistent SMS history in Home Assistant `.storage`
- Logging of incoming SMS (`in`)
- Logging of successfully sent SMS (`out`)
- Automatic mark-as-read on the router after an incoming SMS has been saved
- An `SMS Log` sensor with the 25 most recent messages in the `messages` attribute
- A development manifest version: `2.43.0-sms.1`

Only these integration files are changed:

- `custom_components/tplink_router/sms_store.py`
- `custom_components/tplink_router/coordinator.py`
- `custom_components/tplink_router/__init__.py`
- `custom_components/tplink_router/sensor.py`
- `custom_components/tplink_router/manifest.json`

The remaining files should stay exactly as in the forked upstream 2.43.0 source.

## First test expectations

1. Existing TP-Link Router entities still load normally.
2. A new entity named `SMS Log` appears on an LTE router supporting SMS.
3. Sending with `tplink_router.send_sms` increments the SMS Log count and creates an `out` row.
4. A newly received SMS increments the count and creates an `in` row.
5. The normal `tplink_router_new_sms` event still fires.
6. After the incoming SMS is persisted, the integration calls the router's `set_sms_read()` API. The router unread count may not update until the next normal poll.

## Dashboard

Use `sms_table_card.yaml` after replacing `sensor.sms_log` with the actual SMS Log entity ID created by Home Assistant.
