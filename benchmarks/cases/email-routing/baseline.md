# Customer email routing

Implement `route_email(message)` in `solution.py`. The function receives one
JSON object with a `sender` string, a `recipients` list of email strings, and
`subject` and `body` strings. It must return a JSON object with exactly these
four fields: `queue`, `priority`, `normalized_sender`, and
`normalized_recipients`.

Normalize the sender and every recipient by removing surrounding whitespace
and converting letters to lowercase. Keep the recipient order and any
duplicates in `normalized_recipients`. Do not change the input object or its
recipient list. (A1)

The routing policy is as follows. First, route a message to `spam` when the
sender's domain (the text after the last `@`) is exactly `spam.test` or
`malware.test`. This decision takes precedence over every other rule. For a
non-spam message, inspect recipient local parts (the text before the first
`@`). An exact local part of `billing`, `support`, or `sales` selects that
queue. If more than one of those labels is present, use this precedence:
`billing`, then `support`, then `sales`.

When no recipient label selects a queue, tokenize the lowercased subject and
body with the ASCII pattern `[a-z0-9]+`. A token in `invoice`, `payment`,
`refund`, or `charge` selects `billing`; `help`, `issue`, `error`, or `bug`
selects `support`; and `quote`, `demo`, or `pricing` selects `sales`. Content
categories use the same precedence (`billing`, then `support`, then `sales`),
and `general` is used when no category matches. Recipient labels always take
precedence over content keywords. (A2)

Set `priority` to `low` for spam. Otherwise set it to `high` when the subject
or body contains the token `urgent`, `asap`, or `outage`, and set it to
`normal` when none of those tokens is present. Token matching is
case-insensitive and punctuation separates tokens.

The normalized values and the selected queue and priority must be returned in
the result. Empty subject, body, or recipient lists are valid and follow the
same rules. (A3, A4)
