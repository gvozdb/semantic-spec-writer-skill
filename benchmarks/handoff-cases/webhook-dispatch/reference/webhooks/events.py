from copy import deepcopy


def normalize_event(event):
    event_id = event.get("id")
    event_type = event.get("type")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("invalid webhook event")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("invalid webhook event")
    normalized = deepcopy(event)
    normalized["id"] = event_id.strip()
    normalized["type"] = event_type.strip()
    return normalized
