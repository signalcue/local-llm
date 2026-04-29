#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract unique prompt payloads from comparison logs.")
    parser.add_argument("input_path", help="Path to exported comparisons JSONL file")
    parser.add_argument(
        "output_path",
        nargs="?",
        help="Destination JSONL path for unique prompts",
    )
    args = parser.parse_args()

    input_path = Path(args.input_path)
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    output_path = Path(args.output_path) if args.output_path else input_path.with_name(f"{input_path.stem}-prompts.jsonl")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    written = 0

    with input_path.open("r", encoding="utf-8") as src, output_path.open("w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue

            record = json.loads(line)
            prompt = record.get("prompt")
            if not isinstance(prompt, dict):
                continue

            digest = sha256(json.dumps(prompt, sort_keys=True).encode("utf-8")).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)

            output_record = {
                "id": f"prompt-{written + 1:04d}",
                **prompt,
            }
            dst.write(json.dumps(output_record, ensure_ascii=True) + "\n")
            written += 1

    print(f"Extracted {written} unique prompts to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
