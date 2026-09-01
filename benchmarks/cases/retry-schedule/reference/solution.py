def _require_non_negative_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def build_retry_schedule(attempts, base_delay, max_delay, retry_after=None):
    _require_non_negative_int("attempts", attempts)
    _require_non_negative_int("base_delay", base_delay)
    _require_non_negative_int("max_delay", max_delay)
    if retry_after is not None:
        _require_non_negative_int("retry_after", retry_after)

    floor = retry_after if retry_after is not None else 0
    return [
        min(max_delay, max(base_delay * (2**index), floor))
        for index in range(attempts)
    ]
