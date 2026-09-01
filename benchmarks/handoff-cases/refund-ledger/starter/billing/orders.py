from billing.models import copy_value


def mark_captured(order, payment_id):
    updated = copy_value(order)
    updated["status"] = "paid"
    updated["payment_id"] = payment_id
    return updated


def mark_shipped(order, tracking_number):
    updated = copy_value(order)
    updated["status"] = "shipped"
    updated["tracking_number"] = tracking_number
    return updated
