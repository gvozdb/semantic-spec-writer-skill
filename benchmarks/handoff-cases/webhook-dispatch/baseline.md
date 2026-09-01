# Signed idempotent webhook dispatch

Implement `handle_webhook(secret, body, signature, event, deliveries)` and the
supporting signature, event normalization, and deduplication helpers in the
existing webhooks package. Preserve every input.

`verify_signature(secret, body, signature)` accepts strings only. Compute the
lowercase hexadecimal HMAC-SHA256 of the UTF-8 body with the UTF-8 secret. The
only accepted signature shape is the exact prefix `sha256=` followed by 64
lowercase hexadecimal characters. Compare digests safely. Malformed values
return `false`; the helper must not raise. `handle_webhook` checks this first and
returns exactly `{"ok": false, "status": 401, "error":
"invalid_signature"}` on failure. (A1)

`normalize_event(event)` requires `event.id` and `event.type` to be strings
whose trimmed values are non-empty. It returns a deep copy of the event with
only those two fields replaced by their trimmed values, retaining arbitrary
nested fields. Invalid input raises `ValueError("invalid webhook event")`.
The handler converts that failure to exactly `{"ok": false, "status": 400,
"error": "invalid_event"}`. (A2)

Supported event types are `invoice.paid` and `invoice.failed`. After
normalization, any other type returns exactly `{"ok": false, "status": 422,
"error": "unsupported_event", "type": normalized_type}`. Unsupported events
are not recorded. (A2)

`find_delivery(deliveries, event_id)` returns a deep copy of the first matching
`event_id` or `None`. A duplicate normalized event ID is a successful replay:
return exactly `{"ok": true, "status": 200, "replayed": true, "delivery":
existing_record, "deliveries": unchanged_deep_copy}`. Do not compare payloads
or create another record. (A3)

For a new supported event, append without mutation the exact record
`{"event_id": normalized_id, "type": normalized_type}` and return exactly
`{"ok": true, "status": 200, "replayed": false, "delivery": record,
"deliveries": updated_deliveries}`. Add no timestamps, hashes, or generated
IDs. (A4)

Use the repository's existing narrow Python checks and add no dependencies.
