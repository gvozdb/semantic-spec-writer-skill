def setting_cache_key(tenant_id, setting_name):
    return f"tenant:{tenant_id}:setting:{setting_name}"


def cacheable(result):
    return result.get("source") != "request"
