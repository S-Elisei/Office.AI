"""Stand-in for a vendor CLI binary."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-pct", type=float, default=5.0)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--limit", type=int, default=200_000)
    parser.add_argument("--dump", default=None)
    args, _ = parser.parse_known_args()

    line = sys.stdin.readline()
    try:
        envelope = json.loads(line) if line.strip() else {}
        blocks = envelope.get("message", {}).get("content", [])
        prompt_text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    except (json.JSONDecodeError, AttributeError, TypeError):
        prompt_text = line

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as handle:
            handle.write(prompt_text)

    session_id = os.environ.get("OFFICE_SESSION") or f"stub-{os.getpid()}"

    print(json.dumps({"type": "system", "subtype": "init", "session_id": session_id}), flush=True)
    print(
        json.dumps(
            {
                "type": "assistant",
                "session_id": session_id,
                "message": {
                    "id": "msg-stub",
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"ECHO:{prompt_text}"}],
                    "usage": {"input_tokens": int(args.limit * args.context_pct / 100.0)},
                },
            }
        ),
        flush=True,
    )

    if args.sleep:
        time.sleep(args.sleep)

    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session_id,
                "usage": {"input_tokens": int(args.limit * args.context_pct / 100.0)},
                "modelUsage": {"stub-model": {"contextWindow": args.limit}},
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
