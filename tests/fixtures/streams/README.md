# Stream fixtures

Every file here is copied verbatim from the named CLI's stdout. The JSON shape is
untouched. String values are scrubbed: session ids to a fixed uuid, home
directories to `C:\workspace`, the OS username to `user`, request and message ids
to `REDACTED`.

| file | CLI |
|---|---|
| `claude_capture.jsonl` | claude 2.1.267 |
| `codex_capture.jsonl` | codex 0.154.0 |
| `codex_rollout_capture.jsonl` | codex 0.156.1: `token_count` lines of a session's rollout file, not stdout |
| `codex_failed_file_change_capture.jsonl` | codex |
| `codex_failed_mcp_tool_capture.jsonl` | codex |
| `agy_capture.jsonl` | agy 1.2.1 |
| `agy_failing_command_capture.jsonl` | agy, gemini-3.6-flash-low |
| `agy_tool_error_capture.jsonl` | agy, gemini-3.6-flash-low |
| `agy_quota_capture.jsonl` | agy, gemini-3.8-flash-high: the `result` line of a turn refused for quota |
