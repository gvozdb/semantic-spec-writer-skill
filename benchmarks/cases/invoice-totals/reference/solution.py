def _non_negative_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def calculate_invoice(invoice):
    items = invoice.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")

    line_totals = []
    for item in items:
        quantity = _positive_int(item.get("quantity"), "quantity")
        unit_price = _non_negative_int(item.get("unit_price_cents"), "unit_price_cents")
        line_totals.append(quantity * unit_price)

    discount_requested = _non_negative_int(
        invoice.get("discount_cents", 0), "discount_cents"
    )
    shipping = _non_negative_int(invoice.get("shipping_cents", 0), "shipping_cents")
    tax_rate = invoice.get("tax_rate_bps", 0)
    if isinstance(tax_rate, bool) or not isinstance(tax_rate, int) or not 0 <= tax_rate <= 10000:
        raise ValueError("tax_rate_bps must be an integer from 0 to 10000")

    subtotal = sum(line_totals)
    discount = min(discount_requested, subtotal)
    taxable = subtotal - discount
    tax = (taxable * tax_rate + 5000) // 10000
    return {
        "line_totals_cents": line_totals,
        "subtotal_cents": subtotal,
        "discount_cents": discount,
        "taxable_cents": taxable,
        "tax_cents": tax,
        "shipping_cents": shipping,
        "total_cents": taxable + tax + shipping,
    }
