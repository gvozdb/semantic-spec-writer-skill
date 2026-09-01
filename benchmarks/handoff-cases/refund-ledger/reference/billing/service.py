from billing.events import build_refund_event
from billing.ledger import append_entry, find_by_key
from billing.models import copy_value, remaining_refundable


def _trimmed_string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def execute_refund(order, request, ledger, events):
    key = _trimmed_string(request.get("key"))
    if key is None:
        return {"ok": False, "error": "invalid_key"}
    refund_id = _trimmed_string(request.get("refund_id"))
    if refund_id is None:
        return {"ok": False, "error": "invalid_refund_id"}
    reason = _trimmed_string(request.get("reason"))
    if reason is None:
        return {"ok": False, "error": "invalid_reason"}
    amount = request.get("amount_cents")
    if type(amount) is not int or amount <= 0:
        return {"ok": False, "error": "invalid_amount"}
    if order.get("status") not in {"paid", "shipped"}:
        return {"ok": False, "error": "order_not_refundable"}
    if amount > remaining_refundable(order):
        return {"ok": False, "error": "amount_exceeds_remaining"}

    existing = find_by_key(ledger, order.get("account_id"), key)
    if existing is not None:
        expected = (order.get("id"), refund_id, amount)
        actual = (
            existing.get("order_id"),
            existing.get("refund_id"),
            existing.get("amount_cents"),
        )
        if actual != expected:
            return {"ok": False, "error": "idempotency_conflict"}
        return {
            "ok": True,
            "replayed": True,
            "refund": existing,
            "order": copy_value(order),
            "ledger": copy_value(ledger),
            "events": copy_value(events),
        }

    entry = {
        "account_id": copy_value(order.get("account_id")),
        "order_id": copy_value(order.get("id")),
        "key": key,
        "refund_id": refund_id,
        "amount_cents": amount,
    }
    updated_order = copy_value(order)
    updated_order["refunded_cents"] = order.get("refunded_cents", 0) + amount
    updated_ledger = append_entry(ledger, entry)
    updated_events = copy_value(events)
    updated_events.append(build_refund_event(entry, reason))
    return {
        "ok": True,
        "replayed": False,
        "refund": entry,
        "order": updated_order,
        "ledger": updated_ledger,
        "events": updated_events,
    }
