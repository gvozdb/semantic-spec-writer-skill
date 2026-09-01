# Deterministic retry schedule

Implement `build_retry_schedule(attempts, base_delay, max_delay, retry_after=None)` for a worker that retries a failed operation. The function returns a list of integer delays, one delay for each retry, and must not change any input value.

The three required numeric arguments are non-negative Python integers. `retry_after` may be `None`, meaning that the remote service did not provide a minimum delay; otherwise it is also a non-negative integer. A boolean is not accepted as an integer argument. Raise `TypeError` with the message `"<name> must be an integer"` for a wrong type, where `<name>` is the argument name. Raise `ValueError` with the message `"<name> must be non-negative"` for a negative value. Validate arguments before calculating the schedule, including when `attempts` is zero.

For retry number one, start with `base_delay`. Each later retry doubles the previous uncapped delay, so the uncapped value at zero-based index `i` is `base_delay * 2**i`. If `retry_after` is present, it is a per-retry lower bound. Apply the maximum of the uncapped value and that lower bound, then apply `max_delay` as an upper bound. In other words, each result is `min(max_delay, max(base_delay * 2**i, retry_after or 0))`. The cap wins if the service’s lower bound is greater than the cap.

When `attempts` is zero, return an empty list. Otherwise return the calculated delays in retry order; do not include the initial attempt or any metadata.

## Acceptance criteria

`A1` covers valid schedules, including zero attempts, a zero base delay, a cap below the base delay, and a server lower bound. `A2` covers the specified type and non-negative-value errors.
