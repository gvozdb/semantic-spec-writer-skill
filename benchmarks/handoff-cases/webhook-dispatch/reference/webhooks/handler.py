from webhooks.dedup import append_delivery, find_delivery
from webhooks.events import normalize_event
from webhooks.signature import verify_signature
from webhooks.subscriptions import SUPPORTED_EVENT_TYPES


def handle_webhook(secret, body, signature, event, deliveries):
    if not verify_signature(secret, body, signature):
        return {"ok": False, "status": 401, "error": "invalid_signature"}
    try:
        normalized = normalize_event(event)
    except (AttributeError, ValueError):
        return {"ok": False, "status": 400, "error": "invalid_event"}
    event_type = normalized["type"]
    if event_type not in SUPPORTED_EVENT_TYPES:
        return {
            "ok": False,
            "status": 422,
            "error": "unsupported_event",
            "type": event_type,
        }
    existing = find_delivery(deliveries, normalized["id"])
    if existing is not None:
        from copy import deepcopy

        return {
            "ok": True,
            "status": 200,
            "replayed": True,
            "delivery": existing,
            "deliveries": deepcopy(deliveries),
        }
    record = {"event_id": normalized["id"], "type": event_type}
    return {
        "ok": True,
        "status": 200,
        "replayed": False,
        "delivery": record,
        "deliveries": append_delivery(deliveries, record),
    }
