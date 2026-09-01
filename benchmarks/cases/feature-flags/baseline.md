# Targeted feature flags

Implement `evaluate_flags(user, flags)` for a service that decides which configured features a user may see. `user` is an object with a string `id` and optional `country` and `plan` values. `flags` is an ordered list of objects. Each flag has a unique string `key` and an `enabled` boolean. It may also contain `allow_users`, `countries`, `plans`, an integer `rollout` percentage, and a `depends_on` flag key.

Return one boolean for every flag, in a dictionary keyed by the flag key. Keep the input list order when constructing the dictionary, although consumers should treat the result as a mapping. Do not modify the user object, the flag objects, or any nested lists.

A flag is off unless its `enabled` value is exactly `true`. If `depends_on` is present, the dependency must also resolve to true for the same user. A missing dependency is false. Dependency cycles are false for every flag involved in the cycle, and a flag depending on such a cycle is false as well.

The optional targeting lists are gates, not overrides. When `allow_users` is present, the user’s `id` must be in it. When `countries` is present, the user’s `country` must be in it, and when `plans` is present, the user’s `plan` must be in it. Every present list must match; an empty list therefore matches nobody, and a missing user attribute does not match. These gates must pass before percentage rollout is considered.

If `rollout` is omitted, use 100. A rollout of 0 is always off and 100 is on after the dependency and targeting gates. For a value strictly between 0 and 100, form the UTF-8 string `<user id>:<flag key>`, hash it with SHA-256, take the first eight hexadecimal characters, convert them from base 16 to an integer, and calculate that integer modulo 100. The flag is on exactly when this bucket is less than the rollout percentage. Do not use random numbers, time, or network state, so the same inputs always produce the same mapping.

## Acceptance criteria

`A1` covers enabled-state, targeting-list, allowlist, and rollout decisions. `A2` covers dependency chains, missing dependencies, and cycles.
