# Atomic inventory reservation

Implement `reserve_inventory(inventory, request)` in `solution.py`. The first
argument is a JSON object mapping canonical, case-sensitive SKU strings to
non-negative integer stock counts. The request has a non-empty string
`reservation_id` and an `items` list. Each item contains a non-empty string
`sku` and a positive integer `quantity`.

Trim surrounding whitespace from the reservation ID and every requested SKU.
SKU matching remains case-sensitive. If the request repeats a SKU, combine
the quantities and keep the order in which each distinct trimmed SKU first
appeared. (A1)

The reservation must be atomic. First aggregate all quantities and compare
them with inventory. A request succeeds only if every requested SKU has at
least its aggregate quantity available; a missing SKU has availability zero.
On success, return exactly:

```text
{
  "ok": true,
  "reservation_id": normalized_id,
  "inventory": updated_inventory,
  "reserved": [{"sku": sku, "quantity": total}, ...]
}
```

`updated_inventory` retains every original inventory key and subtracts the
reserved quantities. The `reserved` list follows first-seen distinct SKU
order. On failure, return exactly an object with `ok: false`, the normalized
`reservation_id`, a copy of the unchanged inventory, and a `shortages` list.
Each shortage is `{"sku": sku, "requested": total, "available": count}` and
shortages follow first-seen order; omit `reserved` on failure. Do not partially
decrement stock. (A2)

An empty item list is a successful no-op. Reject invalid values with
`ValueError` using these exact messages: `inventory must be an object`,
`inventory quantities must be non-negative integers`,
`reservation_id must be a non-empty string`, `items must be a list`,
`each item must be an object`, `sku must be a non-empty string`, and
`quantity must be a positive integer`, as applicable. Boolean quantities and
stock counts are not integers. Neither input object may be mutated. (A3)
