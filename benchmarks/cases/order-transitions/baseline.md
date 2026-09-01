# Order lifecycle transitions

Implement `apply_order_event(order, event)` in `solution.py`. Both arguments
are JSON objects. `order` has a `status` field and may contain arbitrary other
fields. `event` has a `type` field and may contain event-specific data. The
function returns either a successful transition or a deterministic error
object, and it must never mutate either input object.

For a successful transition, return an object of the form
`{"ok": true, "order": updated_order}`. The updated order is a deep copy of
the input order with its status changed. A `pay` event from `pending` to
`paid` also stores a trimmed, non-empty string `payment_id` from the event. A
`ship` event from `packed` to `shipped` similarly stores a trimmed, non-empty
string `tracking_number`. All unrelated order fields must be retained, and
event fields other than those named above can be ignored.

The allowed state transitions are:

- `pending` can be paid or cancelled;
- `paid` can be packed, cancelled, or refunded;
- `packed` can be shipped or refunded;
- `shipped` can be delivered or refunded;
- `delivered`, `cancelled`, and `refunded` have no outgoing transitions.
(A1)

The event names are `pay`, `pack`, `ship`, `deliver`, `cancel`, and `refund`.
An event name outside that set returns
`{"ok": false, "error": "unknown_event", "event": event_type}`. A known
event that is not allowed from the current status returns
`{"ok": false, "error": "invalid_transition", "status": current_status,
"event": event_type}`. Check the transition before checking any event
payload. A `pay` event that is allowed but lacks a non-empty string
`payment_id` returns `{"ok": false, "error": "missing_payment_id"}`. An
allowed `ship` event without a non-empty string `tracking_number` returns
`{"ok": false, "error": "missing_tracking_number"}`. Whitespace around either
identifier is removed before it is stored.
(A2)

Do not add timestamps or other generated values. Errors must leave both input
objects unchanged just as successful calls do. (A3)
