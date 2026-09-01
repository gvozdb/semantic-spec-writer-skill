def paginate(records, limit, cursor=None):
    start = int(cursor) if cursor else 0
    end = start + limit
    return {
        "items": records[start:end],
        "next_cursor": str(end) if end < len(records) else None,
    }
