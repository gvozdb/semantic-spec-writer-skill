from billing.models import copy_value


def build_refund_event(entry, reason):
    return {
        "type": "refund.created",
        "account_id": copy_value(entry["account_id"]),
        "order_id": copy_value(entry["order_id"]),
        "refund_id": copy_value(entry["refund_id"]),
        "amount_cents": entry["amount_cents"],
        "reason": reason.strip(),
    }
