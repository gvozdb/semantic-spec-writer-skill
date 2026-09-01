import re


_CATEGORY_KEYWORDS = {
    "billing": {"invoice", "payment", "refund", "charge"},
    "support": {"help", "issue", "error", "bug"},
    "sales": {"quote", "demo", "pricing"},
}
_CATEGORY_ORDER = ("billing", "support", "sales")
_URGENCY = {"urgent", "asap", "outage"}
_BLOCKED_DOMAINS = {"spam.test", "malware.test"}


def route_email(message):
    sender = message.get("sender", "").strip().lower()
    recipients = [value.strip().lower() for value in message.get("recipients", [])]
    subject = message.get("subject", "")
    body = message.get("body", "")
    tokens = set(re.findall(r"[a-z0-9]+", f"{subject} {body}".lower()))

    sender_domain = sender.rsplit("@", 1)[1] if "@" in sender else ""
    if sender_domain in _BLOCKED_DOMAINS:
        queue = "spam"
    else:
        queue = None
        for category in _CATEGORY_ORDER:
            if any(value.partition("@")[0] == category for value in recipients):
                queue = category
                break
        if queue is None:
            for category in _CATEGORY_ORDER:
                if tokens & _CATEGORY_KEYWORDS[category]:
                    queue = category
                    break
        if queue is None:
            queue = "general"

    priority = "low" if queue == "spam" else ("high" if tokens & _URGENCY else "normal")
    return {
        "queue": queue,
        "priority": priority,
        "normalized_sender": sender,
        "normalized_recipients": recipients,
    }
