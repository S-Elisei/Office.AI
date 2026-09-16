
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 24 * 60 * 60
URL_ENV = "OFFICE_MCP_URL"


def post(url: str, payload: bytes) -> bytes | None:
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def main() -> None:
    url = os.environ.get(URL_ENV)
    if not url:
        return

    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        try:
            body = post(url, line.encode("utf-8"))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if message.get("id") is not None:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32603, "message": f"office unreachable: {exc}"},
                    }
                )
            continue

        if not body:
            continue
        sys.stdout.write(body.decode("utf-8").strip() + "\n")
        sys.stdout.flush()


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
