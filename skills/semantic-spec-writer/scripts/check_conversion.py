#!/usr/bin/env python3
"""Check that a converted semantic document is smaller than its source."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def metrics(path: Path, encoder: Any | None = None) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    result = {
        "bytes": len(text.encode("utf-8")),
        "words": len(text.split()),
    }
    if encoder is not None:
        result["tokens"] = len(encoder.encode(text))
    return result


def load_encoder(name: str | None) -> Any | None:
    if not name:
        return None
    try:
        import tiktoken

        return tiktoken.get_encoding(name)
    except (ImportError, OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load tokenizer {name}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--encoding",
        default=os.environ.get("SEMANTIC_SPEC_TOKEN_ENCODING"),
        help="optional tiktoken encoding, for example o200k_base",
    )
    args = parser.parse_args()

    try:
        encoder = load_encoder(args.encoding)
        source = metrics(args.source, encoder)
        output = metrics(args.output, encoder)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = {
        "source": source,
        "output": output,
        "smaller_bytes": output["bytes"] < source["bytes"],
        "smaller_words": output["words"] < source["words"],
        "encoding": args.encoding,
    }
    if args.encoding:
        result["smaller_tokens"] = output["tokens"] < source["tokens"]
    print(json.dumps(result, sort_keys=True))
    checks = [result["smaller_bytes"], result["smaller_words"]]
    if args.encoding:
        checks.append(result["smaller_tokens"])
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
