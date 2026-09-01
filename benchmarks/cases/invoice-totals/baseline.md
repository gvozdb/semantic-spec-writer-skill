# Invoice total calculation

Implement `calculate_invoice(invoice)` in `solution.py`. The input is a JSON
object whose `items` value is a list of line-item objects. Every line item has
a positive integer `quantity` and a non-negative integer `unit_price_cents`.
Other line-item fields, such as descriptions, do not affect the calculation.
All monetary values are integer cents, so do not use binary floating point.

The invoice may contain non-negative integer `discount_cents` and
`shipping_cents`; each defaults to zero when absent. It may also contain an
integer `tax_rate_bps` (basis points, where 100 bps is one percent), which
defaults to zero. The tax rate must be between 0 and 10,000 inclusive.

Calculate each line total as `quantity * unit_price_cents`, retaining the
input item order. The subtotal is the sum of those line totals. Apply the
discount to the subtotal, but cap the applied discount at the subtotal. Tax is
calculated only on the discounted subtotal, not on shipping. Compute tax as
`taxable_cents * tax_rate_bps / 10,000`, rounding a half cent upward to the
next cent. The total is discounted subtotal plus tax plus shipping. (A1)

Return exactly one JSON object with these fields:

```text
line_totals_cents, subtotal_cents, discount_cents, taxable_cents,
tax_cents, shipping_cents, total_cents
```

Here `discount_cents` is the capped amount actually applied, and every field
is an integer. An empty item list is valid and can still have shipping. Do not
modify the invoice or any of its item objects. (A2, A3)

Reject malformed numeric inputs with `ValueError`. Use the exact messages
`items must be a list`, `quantity must be a positive integer`,
`unit_price_cents must be a non-negative integer`,
`discount_cents must be a non-negative integer`,
`shipping_cents must be a non-negative integer`, or
`tax_rate_bps must be an integer from 0 to 10000`, as applicable. Boolean
values do not count as integers.
