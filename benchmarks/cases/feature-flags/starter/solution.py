def evaluate_flags(user, flags):
    return {flag["key"]: bool(flag.get("enabled")) for flag in flags}
