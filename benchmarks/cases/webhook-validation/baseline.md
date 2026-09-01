# Webhook signature validation

Implement `validate_webhook(payload, signature_header, secret, now, tolerance=300)` for a receiver that checks a signed JSON webhook before processing it. Always return an object with exactly `valid` and `reason` fields. A successful check returns `{"valid": true, "reason": "ok"}`. The possible negative reasons are `"malformed"`, `"expired"`, and `"mismatch"`.

The payload can be any JSON value. Canonicalize it with UTF-8 JSON using sorted object keys, no spaces (`separators=(',', ':')`), and `ensure_ascii=False`. The signed message is the ASCII timestamp text, a period, and that canonical body. Compute an HMAC-SHA256 digest with the UTF-8 encoded shared secret and represent the digest as 64 lowercase hexadecimal characters.

The signature header is a comma-separated sequence of `key=value` components; whitespace around components, keys, and values may be ignored. It must contain exactly one `t` component whose value is non-empty ASCII decimal digits, and at least one `v1` component. Every `v1` value must be exactly 64 lowercase hexadecimal characters. Other well-formed components, such as an older `v0`, are ignored. A component without `=` or duplicate `t` values is malformed. An empty secret, a non-string header or secret, a non-integer `now` or `tolerance`, or a negative tolerance is also malformed.

Parse the timestamp as an integer and reject the request as expired when `abs(now - timestamp)` is greater than `tolerance`; the boundary is accepted. For a non-expired request, compare the computed digest against every `v1` value with constant-time comparison and accept if any one matches. Return `mismatch` when the header is well-formed and current but none matches. Do not mutate the payload or any other input, and do not use the current clock, randomness, or network access.

## Acceptance criteria

`A1` covers exact canonicalization, current timestamps, rotated signatures, and successful HMAC verification. `A2` covers malformed headers, stale timestamps, and current signatures that do not match.
