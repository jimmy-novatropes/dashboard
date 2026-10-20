# AWS Error Feed dashboard — notes for Claude

- `old_app/` — single-computer dashboard (port 8765, local model off). Stable; change only when asked.
- `new_app/` — the one in use: runs on the tower (`initium`, port 8766, shared over Tailscale at
  https://initium.tail11109.ts.net), local model (Ollama, qwen3:14b) for triage, log summaries and
  `local_read` / `local_git`. The laptop's Claude connector runs `new_app/mcp_server.py` against the tower.
- `logging_kit/` — `oflog.py` (structured log lines for client Lambdas) + `apply_oflog.py` (adds it to a repo).

## Save usage: let the local model read first
- Before reading big files, whole folders, long diffs or git history, use `local_read(path, question)` /
  `local_git(repo, what, question)`; then open only the lines you need. Verify before editing.
- `aws_error_feed.py` is ~3,000 lines: use `local_read("dashboard/new_app/aws_error_feed.py", "<question>")`
  or grep for the function instead of reading it all.

## Coding with the tower (fewer Claude tokens)
- **Find code**: `find_code(query, repo)` returns only the matching lines with file:line — use it instead of
  opening files to search. `repo_map(repo)` gives the layout + one-line purpose per file.
- **Fix an error**: `fix_brief(profile, function)` = error, preceding logs, failing code, runs + a local diagnosis in one call.
- **Small edits**: `local_draft_change(path, start_line, end_line, instruction)` lets the local model write the
  change and returns a diff; review it, then the same call with `apply=True` writes it. Write complex logic yourself.
- **Tests / checks**: `local_run(command, repo)` (pytest, local_tests, py_compile, npm test…) returns only failures.
- **Commit message**: `draft_commit_message(repo)` (drafts only, never commits).
- **One function, not the whole file**: `outline(file)` = signatures only; `get_symbol(name, repo)` = that function's source.
  `repo_map(repo, focus="...")` = only the files relevant to the task.
- Tools not listed (schedules, inventory, deploys, notes, mutes, reports…) are reached with `more_tools()`.
- Normal reading/editing still works as before — these are shortcuts, not requirements.

## Rules
- Keep `old_app` and `new_app` in sync for shared fixes; they differ only in port (8765/8766), the local
  model default (off/on), `mcp_server.py` default URL and `install_autostart.ps1`.
- Read-only toward AWS. Never read secret values (Secrets Manager metadata only), never store Lambda env values.
- `config.json`, `errors.db*`, `*.log`, `repos.json` are local and git-ignored — never commit them.
- The dashboard has no login: share only inside the tailnet (`tailscale serve`), never `tailscale funnel`.
- After changing `new_app`, the tower needs `git pull` + restart (stop the pythonw process, then
  `Start-ScheduledTask "AWS Error Feed"`); the laptop needs Claude restarted for connector changes.
