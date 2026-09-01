import base64
import binascii
import json


def _encode_cursor(offset):
    payload = json.dumps(
        {"offset": offset},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor):
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("invalid cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError("invalid cursor") from None

    if (
        type(value) is not dict
        or set(value) != {"offset"}
        or type(value["offset"]) is not int
        or value["offset"] < 0
    ):
        raise ValueError("invalid cursor")
    return value["offset"]


def paginate(records, limit, cursor=None):
    if not isinstance(records, list):
        raise TypeError("records must be a list")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")

    offset = 0 if cursor is None else _decode_cursor(cursor)
    if offset > len(records):
        raise ValueError("cursor offset is past the end of records")

    end = min(offset + limit, len(records))
    items = records[offset:end]
    next_cursor = _encode_cursor(end) if end < len(records) else None
    return {"items": items, "next_cursor": next_cursor}
