import hashlib


def evaluate_flags(user, flags):
    by_key = {flag["key"]: flag for flag in flags}
    memo = {}
    visiting = set()

    def resolve(key):
        if key in memo:
            return memo[key]
        if key in visiting:
            return False

        flag = by_key.get(key)
        if flag is None:
            memo[key] = False
            return False

        visiting.add(key)
        result = flag.get("enabled") is True
        dependency = flag.get("depends_on")
        if result and dependency is not None:
            result = resolve(dependency)

        gates = (
            ("allow_users", "id"),
            ("countries", "country"),
            ("plans", "plan"),
        )
        if result:
            for option, user_field in gates:
                if option in flag and user.get(user_field) not in flag[option]:
                    result = False
                    break

        if result:
            rollout = flag.get("rollout", 100)
            if rollout == 0:
                result = False
            elif rollout < 100:
                material = f'{user["id"]}:{key}'.encode("utf-8")
                bucket = int(hashlib.sha256(material).hexdigest()[:8], 16) % 100
                result = bucket < rollout

        visiting.remove(key)
        memo[key] = result
        return result

    return {flag["key"]: resolve(flag["key"]) for flag in flags}
