# Stream fixtures

Every file here is copied verbatim from the named CLI's stdout. The JSON shape is
untouched. String values are scrubbed: session ids to a fixed uuid, home
directories to `C:\workspace`, the OS username to `user`, request and message ids
to `REDACTED`.

| file | CLI |
|---|---|
| `claude_capture.jsonl` | claude 2.1.267 |
| `claude_session_capture.jsonl` | claude 2.1.284, claude-haiku-4-5: the `assistant` and `user` lines of one turn of a session file, not stdout; its two message ids are `REDACTED-1` and `REDACTED-2` |
| `codex_capture.jsonl` | codex 0.154.0 |
| `codex_rollout_capture.jsonl` | codex 0.156.1: `token_count` lines of a session's rollout file, not stdout |
| `codex_rollout_reasoning_capture.jsonl` | codex 0.159.1, gpt-6.1-sol: two finished reasoning items of a session's rollout file, not stdout; `encrypted_content` is `REDACTED` and the thread and turn ids are the fixed uuid |
| `codex_failed_mcp_tool_capture.jsonl` | codex |
| `agy_failing_command_capture.jsonl` | agy, gemini-3.6-flash-low |
| `agy_tool_error_capture.jsonl` | agy, gemini-3.6-flash-low |
| `agy_quota_capture.jsonl` | agy, gemini-3.8-flash-high: the `result` line of a turn refused for quota |
| `agy_usage_capture.json` | agy: the stdout of `agy --output-format json --print /usage`, copied verbatim |
