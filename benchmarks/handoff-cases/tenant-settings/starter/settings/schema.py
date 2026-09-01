SUPPORTED_KINDS = {"bool", "int", "string"}


def setting_descriptor(name, kind, default):
    return {"name": name, "kind": kind, "default": default}
