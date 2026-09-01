def retry_delay_seconds(attempt):
    return min(300, 2 ** max(0, attempt))
