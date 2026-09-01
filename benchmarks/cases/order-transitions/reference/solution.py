from copy import deepcopy


_TRANSITIONS = {
    "pending": {"pay": "paid", "cancel": "cancelled"},
    "paid": {"pack": "packed", "cancel": "cancelled", "refund": "refunded"},
    "packed": {"ship": "shipped", "refund": "refunded"},
    "shipped": {"deliver": "delivered", "refund": "refunded"},
    "delivered": {},
    "cancelled": {},
    "refunded": {},
}
_EVENTS = {"pay", "pack", "ship", "deliver", "cancel", "refund"}


def apply_order_event(order, event):
    event_type = event.get("type")
    if event_type not in _EVENTS:
        return {"ok": False, "error": "unknown_event", "event": event_type}

    status = order.get("status")
    next_status = _TRANSITIONS.get(status, {}).get(event_type)
    if next_status is None:
        return {
            "ok": False,
            "error": "invalid_transition",
            "status": status,
            "event": event_type,
        }

    if event_type == "pay":
        payment_id = event.get("payment_id")
        if not isinstance(payment_id, str) or not payment_id.strip():
            return {"ok": False, "error": "missing_payment_id"}
    elif event_type == "ship":
        tracking_number = event.get("tracking_number")
        if not isinstance(tracking_number, str) or not tracking_number.strip():
            return {"ok": False, "error": "missing_tracking_number"}

    updated = deepcopy(order)
    updated["status"] = next_status
    if event_type == "pay":
        updated["payment_id"] = payment_id.strip()
    elif event_type == "ship":
        updated["tracking_number"] = tracking_number.strip()
    return {"ok": True, "order": updated}
