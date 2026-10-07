# SMS Gateway v0.4 MR200 read-fix test plan

## Why this build exists

The MR200 v6 web UI marks a message read by batching:

1. `GET LTE_SMS_RECVMSGBOX` (`totalNumber`, `amountPerPage`)
2. `SET LTE_SMS_RECVMSGENTRY#<page stack>` with `unread=0`

in one CGI action request. `tplinkrouterc6u==5.36.0` sends only the SET.
The MR200 also exposes page-relative `__stack` values (`1,0,0,0,0,0`, `2,...`) while its `index` field is a different stable message number (`49`, `48`, ...).

v0.4 therefore re-fetches page 1 in the same authorized session, resolves the target again, and for TPLinkMR* clients uses the same GET+SET batch as the MR200 web UI.

`auto_mark_read` now defaults to **off** until the manual read test below passes.

## Test order

1. Install v0.4 and restart Home Assistant.
2. Keep `Automatically mark received SMS as read` OFF.
3. Send `V04 READ TEST` to the MR200 and wait until it appears in the HA SMS Log.
4. Copy that incoming log entry's local `id`.
5. Run `tplink_router.mark_sms_read` for that `message_id`.
6. Verify in the MR200 UI that the message remains present but becomes read.
7. **Critical regression test:** send `V04 AFTER READ`. It must arrive while the integration stays loaded.
8. If step 7 passes, enable automatic mark-read and send `V04 AUTO READ`. Verify it is logged, becomes read, and another new SMS can still arrive afterward.
9. Only after read is stable, test `tplink_router.delete_sms`. Deleting from the router must not remove the HA history entry.

If step 7 fails, turn auto-read off again and unload the integration before further router-side SMS writes.
