from billing.models import copy_value


def find_by_key(entries, account_id, key):
    normalized = key.strip()
    for entry in entries:
        if entry.get("account_id") == account_id and entry.get("key") == normalized:
            return copy_value(entry)
    return None


def append_entry(entries, entry):
    updated = copy_value(entries)
    updated.append(copy_value(entry))
    return updated
