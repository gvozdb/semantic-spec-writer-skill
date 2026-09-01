from copy import deepcopy


def select_layer(name, default, request_values, tenant_values, global_values):
    for source, values in (
        ("request", request_values),
        ("tenant", tenant_values),
        ("global", global_values),
    ):
        if name in values:
            return {"source": source, "raw": deepcopy(values[name])}
    return {"source": "default", "raw": deepcopy(default)}
