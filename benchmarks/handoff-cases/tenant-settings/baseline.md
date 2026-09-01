# Layered tenant setting resolution

Implement `resolve_setting(name, kind, default, request_values,
tenant_values, global_values)` and its supporting layer-selection and coercion
helpers in the existing settings package. The functions must not mutate any
input.

Select the first layer that contains `name`, in this exact order: request,
tenant, global. Presence matters: a present value of `null` is an explicit
disable and must not fall through. If no layer contains the name, select the
provided default. `select_layer` returns exactly `{"source": "request" |
"tenant" | "global" | "default", "raw": selected_value}` and returns a deep
copy of a selected container value. (A1)

Coerce the selected raw value according to `kind`, which is one of `bool`,
`int`, or `string`. `null` remains `null` for every kind. For `bool`, accept
actual booleans or case-insensitive trimmed strings `true` and `false`. For
`int`, accept an integer that is not a boolean or a trimmed base-10 string with
an optional leading minus and at least one digit. For `string`, accept only a
string and trim it; an empty trimmed string is valid. Do not coerce floats.
(A2)

Invalid values raise `ValueError` with the exact message
`invalid <kind> setting`, where `<kind>` is the supplied kind. An unsupported
kind raises `ValueError("unsupported setting kind")`. Check kind support before
returning `null`, so `null` plus an unsupported kind still fails. (A2)

`resolve_setting` returns exactly `{"name": name, "value": coerced_value,
"source": selected_source}`. Preserve the name unchanged, including whitespace;
only setting values are trimmed. A selected mutable value and every source map
must remain independent from the result. (A3)

Use the repository's existing narrow Python checks and add no dependencies.
