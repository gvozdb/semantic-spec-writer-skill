from copy import deepcopy


def copy_value(value):
    return deepcopy(value)


def remaining_refundable(order):
    return order.get("paid_cents", 0) - order.get("refunded_cents", 0)
