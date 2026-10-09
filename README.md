# AWS Error Feed dashboard

| Folder | What it is | Port |
|---|---|---|
| `old_app/` | The working single-computer dashboard. Stable - use this day to day. Local model off by default. | 8765 |
| `new_app/` | The connected version: tower + laptop + phone over Tailscale, local model (Ollama) triage. | 8766 |

Both can run at the same time (different ports, separate `errors.db`).

Run:  `cd old_app` (or `new_app`) then `python aws_error_feed.py`

Claude Desktop connector: point `claude_desktop_config.json` at `old_app\mcp_server.py` or `new_app\mcp_server.py`.
