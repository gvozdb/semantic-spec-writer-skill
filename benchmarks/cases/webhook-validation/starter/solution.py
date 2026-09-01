import hashlib
import json


def validate_webhook(payload, signature_header, secret, now, tolerance=300):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{secret}.{body}".encode("utf-8")).hexdigest()
    valid = signature_header == digest
    return {"valid": valid, "reason": "ok" if valid else "mismatch"}
