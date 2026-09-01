import re

from settings.schema import SUPPORTED_KINDS


_INTEGER = re.compile(r"^-?[0-9]+$")


def coerce_value(raw, kind):
    if kind not in SUPPORTED_KINDS:
        raise ValueError("unsupported setting kind")
    if raw is None:
        return None
    if kind == "bool":
        if type(raw) is bool:
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
            return raw.strip().lower() == "true"
        raise ValueError("invalid bool setting")
    if kind == "int":
        if type(raw) is int:
            return raw
        if isinstance(raw, str) and _INTEGER.fullmatch(raw.strip()):
            return int(raw.strip())
        raise ValueError("invalid int setting")
    if isinstance(raw, str):
        return raw.strip()
    raise ValueError("invalid string setting")
