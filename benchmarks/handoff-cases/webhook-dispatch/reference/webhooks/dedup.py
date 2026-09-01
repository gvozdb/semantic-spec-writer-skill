from copy import deepcopy


def find_delivery(deliveries, event_id):
    for delivery in deliveries:
        if delivery.get("event_id") == event_id:
            return deepcopy(delivery)
    return None


def append_delivery(deliveries, record):
    updated = deepcopy(deliveries)
    updated.append(deepcopy(record))
    return updated
