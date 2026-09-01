# Idempotent refund ledger workflow

Implement `execute_refund(order, request, ledger, events)` and the supporting
refund ledger and event helpers in the existing billing package. All inputs are
JSON-compatible values and must remain unchanged.

The order contains `id`, `account_id`, `status`, `paid_cents`, and
`refunded_cents`, plus arbitrary unrelated fields. A refund is allowed only for
an order whose status is `paid` or `shipped`. `request.key`,
`request.refund_id`, and `request.reason` must be trimmed non-empty strings.
`request.amount_cents` must be an integer greater than zero; booleans are not
integers for this contract. Validate in this order: request fields, order
status, remaining refundable amount, then idempotency. (A1)

The remaining refundable amount is `paid_cents - refunded_cents`. An amount
above it returns exactly `{"ok": false, "error": "amount_exceeds_remaining"}`.
Invalid fields return `invalid_key`, `invalid_refund_id`, `invalid_reason`, or
`invalid_amount` in the same exact error shape. An ineligible status returns
`{"ok": false, "error": "order_not_refundable"}`. (A1)

Ledger idempotency is scoped by the trimmed key and `account_id`.
`find_by_key(entries, account_id, key)` returns a deep copy of the first match
or `None`. A matching account/key with the same `order_id`, `refund_id`, and
`amount_cents` is a replay. Return success with `replayed: true`, the existing
entry, and deep copies of the unchanged order, ledger, and events. A scoped
match with any of those three values different returns exactly
`{"ok": false, "error": "idempotency_conflict"}`. (A2)

For a new refund, append this exact ledger entry without mutating the list:
`account_id`, `order_id`, trimmed `key`, trimmed `refund_id`, and
`amount_cents`. Update a deep copy of the order by adding the amount to
`refunded_cents`. Append one event built by `build_refund_event(entry, reason)`:
`{"type": "refund.created", "account_id": ..., "order_id": ...,
"refund_id": ..., "amount_cents": ..., "reason": trimmed_reason}`. The event
builder and `append_entry(entries, entry)` also preserve their inputs. (A3)

New success returns exactly `{"ok": true, "replayed": false, "refund": entry,
"order": updated_order, "ledger": updated_ledger, "events": updated_events}`.
Replay uses the same keys with `replayed: true` and no state changes. Retain all
unrelated order fields and add no timestamps or generated identifiers. (A4)

Use the repository's existing narrow Python checks. Do not add dependencies.
