# MCP Guardian — Demonstration guide

Record a 4–6 minute walkthrough using QuickTime / macOS screen recording (Shift+Cmd+5), Windows Snipping Tool screen recorder, or OBS. Keep the terminal and Claude window visible. Do not display generated keys, tokens or private files.

| Scene | Action | Evidence to capture |
|---|---|---|
| 1 | Start approval agent | Running status in the terminal |
| 2 | Claude Desktop asks for `create_file` on `desktop-demo.txt` | Popup with source, server, method, tool and arguments; click Allow once; file appears |
| 3 | Claude Desktop asks for `delete_file` on same file | Click Block; Claude reports denial; file remains |
| 4 | Claude Web with configured API-key connector asks for create | `Claude Web / HTTP` popup; allow; file exists |
| 5 | Claude Web asks for delete | Block; file still exists |
| 6 | Run `python manual_demo.py` | Two simultaneous requests, one allowed and one blocked; only one file exists |
| 7 | Optional timeout | Leave a request unanswered (default 45s); tool not executed |
| 8 | Run `python -m pytest -q` | Show actual test output |

If you want to shorten the timeout, stop all services and edit `.guardian/config.json` to set `timeout` to 10 before the recording. Restore 45 afterwards. Confirm all relevant services are restarted after editing.

## Suggested narration

“Guardian intercepts MCP JSON-RPC at the transport boundary. Claude Desktop launches our STDIO wrapper instead of the original server. Claude Web's connector calls our authenticated Streamable HTTP gateway. Both use the same local broker, which creates a unique pending approval and opens a native macOS confirmation dialog (or a Tkinter window on other platforms). An approved request is forwarded without changing its arguments, while a blocked request returns an error using the original JSON-RPC ID. Concurrent requests remain isolated, and a missing decision fails closed. The upstream never sees a blocked tool invocation.”

## Video link

[View demonstration release](https://github.com/AbhishekAgrahari22/mcp-guardian/releases/tag/v1.0.0) | [Download demonstration video (.mov)](https://github.com/AbhishekAgrahari22/mcp-guardian/releases/download/v1.0.0/mcp-guardian-demo.mov)

Published recording: `mcp-guardian-demo.mov` (8.12 MB), release v1.0.0. Live Claude Desktop and Claude Web Allow and Block tests have been confirmed by the project operator. The checklist above provides reproducible steps for reviewers; for Block, verify that the file remains and the audit shows `block`. Do not include secret headers or configuration values.
