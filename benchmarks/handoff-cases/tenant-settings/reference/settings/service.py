from settings.coercion import coerce_value
from settings.layers import select_layer


def resolve_setting(name, kind, default, request_values, tenant_values, global_values):
    selected = select_layer(
        name,
        default,
        request_values,
        tenant_values,
        global_values,
    )
    return {
        "name": name,
        "value": coerce_value(selected["raw"], kind),
        "source": selected["source"],
    }
