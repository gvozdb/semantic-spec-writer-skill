SUPPORTED_EVENT_TYPES = {"invoice.paid", "invoice.failed"}


def subscription_key(account_id, event_type):
    return f"{account_id}:{event_type}"
