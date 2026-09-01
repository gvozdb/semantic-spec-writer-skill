import hashlib
import hmac
import re


_SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")


def verify_signature(secret, body, signature):
    if not all(isinstance(value, str) for value in (secret, body, signature)):
        return False
    match = _SIGNATURE.fullmatch(signature)
    if match is None:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, match.group(1))
