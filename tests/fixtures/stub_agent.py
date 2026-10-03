"""Stand-in for a vendor CLI binary."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-pct", type=float, default=5.0)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--limit", type=int, default=200_000)
    parser.add_argument("--dump", default=None)
    parser.add_argument("--die", action="store_true")
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
    message = {
        "id": "msg-stub",
        "role": "assistant",
        "content": [{"type": "text", "text": f"ECHO:{prompt_text}"}],
        "usage": {"input_tokens": int(args.limit * args.context_pct / 100.0)},
    }

    # Invented: the session file line of the message, as claude keeps one per session.
    projects = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "stub")
    os.makedirs(projects, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with open(os.path.join(projects, f"{session_id}.jsonl"), "a", encoding="utf-8") as handle:
        recorded = {**message, "usage": {**message["usage"], "output_tokens": 7}}
        handle.write(json.dumps({"type": "assistant", "timestamp": stamp, "message": recorded}) + "\n")

    print(json.dumps({"type": "system", "subtype": "init", "session_id": session_id}), flush=True)
    print(
        json.dumps({"type": "assistant", "session_id": session_id, "message": message}),
        flush=True,
    )

    if args.sleep:
        time.sleep(args.sleep)

    if args.die:
        # Invented: a vendor's error text, as a dying turn leaves it on stderr.
        print("Error: Selected model is at capacity. Please try a different model.",
              file=sys.stderr, flush=True)
        sys.exit(1)

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
