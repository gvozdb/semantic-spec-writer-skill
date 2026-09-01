def _positive_quantity(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("quantity must be a positive integer")
    return value


def reserve_inventory(inventory, request):
    if not isinstance(inventory, dict):
        raise ValueError("inventory must be an object")
    if any(
        isinstance(stock, bool) or not isinstance(stock, int) or stock < 0
        for stock in inventory.values()
    ):
        raise ValueError("inventory quantities must be non-negative integers")

    reservation_id = request.get("reservation_id")
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise ValueError("reservation_id must be a non-empty string")
    reservation_id = reservation_id.strip()

    items = request.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")

    totals = {}
    order = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each item must be an object")
        sku = item.get("sku")
        if not isinstance(sku, str) or not sku.strip():
            raise ValueError("sku must be a non-empty string")
        sku = sku.strip()
        quantity = _positive_quantity(item.get("quantity"))
        if sku not in totals:
            totals[sku] = 0
            order.append(sku)
        totals[sku] += quantity

    unchanged = dict(inventory)
    shortages = []
    for sku in order:
        available = inventory.get(sku, 0)
        if available < totals[sku]:
            shortages.append({
                "sku": sku,
                "requested": totals[sku],
                "available": available,
            })

    if shortages:
        return {
            "ok": False,
            "reservation_id": reservation_id,
            "inventory": unchanged,
            "shortages": shortages,
        }

    updated = dict(inventory)
    for sku in order:
        updated[sku] = updated.get(sku, 0) - totals[sku]
    return {
        "ok": True,
        "reservation_id": reservation_id,
        "inventory": updated,
        "reserved": [
            {"sku": sku, "quantity": totals[sku]}
            for sku in order
        ],
    }
