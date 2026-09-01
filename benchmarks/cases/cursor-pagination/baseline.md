# Cursor pagination

Implement `paginate(records, limit, cursor=None)` for an API that returns a slice of an already ordered record list. The result must be an object with exactly two fields: `items`, containing the selected records, and `next_cursor`, containing an opaque string when more records remain or `None` on the final page. The input list and its records must not be changed.

`records` must be a list. `limit` must be a Python integer other than a boolean and must be between 1 and 100 inclusive. Otherwise raise `ValueError` with the message `"limit must be an integer from 1 to 100"`. A missing cursor is represented by `None` and starts at offset zero.

The cursor encodes only an integer offset. To create one, serialize `{"offset": offset}` as UTF-8 JSON with sorted keys and the separators `(',', ':')`, encode those bytes with URL-safe Base64, and remove all trailing `=` padding. To read a cursor, add the required Base64 padding, decode it, and parse JSON. It is valid only when the decoded value is an object with exactly the `offset` key whose value is a non-negative integer that is not a boolean. Any other cursor value raises `ValueError("invalid cursor")`. An offset greater than the number of records raises `ValueError("cursor offset is past the end of records")`; an offset equal to the length is a valid empty final page.

Starting at the decoded offset, take at most `limit` records without reordering or filtering them. The next offset is the start offset plus the number of items returned. Encode that next offset using the cursor algorithm only when it is less than `len(records)`; otherwise set `next_cursor` to `None`. An empty input therefore produces an empty final page.

## Acceptance criteria

`A1` covers canonical cursors, page slicing, final-page behavior, empty inputs, and input preservation. `A2` covers invalid limits, malformed cursors, and offsets beyond the record list.
