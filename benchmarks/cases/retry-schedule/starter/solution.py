def build_retry_schedule(attempts, base_delay, max_delay, retry_after=None):
    return [
        min(max_delay, base_delay * (index + 1))
        for index in range(attempts)
    ]
