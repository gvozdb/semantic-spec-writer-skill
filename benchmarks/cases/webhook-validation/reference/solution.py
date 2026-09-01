import hashlib
import hmac
import json


def _outcome(valid, reason):
    return {"valid": valid, "reason": reason}


def validate_webhook(payload, signature_header, secret, now, tolerance=300):
    if (
        not isinstance(signature_header, str)
        or not isinstance(secret, str)
        or not secret
        or type(now) is not int
        or type(tolerance) is not int
        or tolerance < 0
    ):
        return _outcome(False, "malformed")

    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return _outcome(False, "malformed")

    timestamps = []
    signatures = []
    for component in signature_header.split(","):
        component = component.strip()
        if "=" not in component:
            return _outcome(False, "malformed")
        key, value = component.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            signatures.append(value)

    if len(timestamps) != 1 or not signatures:
        return _outcome(False, "malformed")

    timestamp_text = timestamps[0]
    if not timestamp_text or not timestamp_text.isascii() or not timestamp_text.isdigit():
        return _outcome(False, "malformed")
    if any(
        len(signature) != 64
        or any(character not in "0123456789abcdef" for character in signature)
        for signature in signatures
    ):
        return _outcome(False, "malformed")

    timestamp = int(timestamp_text)
    if abs(now - timestamp) > tolerance:
        return _outcome(False, "expired")

    message = f"{timestamp_text}.{body}".encode("utf-8")
    expected = hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    if any(hmac.compare_digest(signature, expected) for signature in signatures):
        return _outcome(True, "ok")
    return _outcome(False, "mismatch")
