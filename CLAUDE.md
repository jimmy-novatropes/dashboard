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

## Ask the tower first (fewest Claude tokens)
- "How are things?" -> `morning_brief()`. Any question about a client / function / error -> `ask_tower(question)`:
  the tower reads all the data with its own model and returns a short answer with evidence ids.
- Use the raw tools (investigate, get_log_lines, get_line_context, fix_brief) only to verify or before editing.
- `outline`, `get_symbol`, `repo_map`, `local_git`, `tasks_add`, `tasks_update` are reached with `more_tools()`.

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
- `config.json`, `errors.db*`, `tasks.db*`, `*.log`, `repos.json` are local and git-ignored — never commit them.
- The dashboard has no login: share only inside the tailnet (`tailscale serve`), never `tailscale funnel`.
- After changing `new_app`: the user runs `new_app/deploy_tower.py` on the tower (one step: compile check, local commit (no push - the user pushes manually), restart, status, "RESTART CLAUDE: YES/NO").

## Tasks (tasks.db)
- `new_app/aws_tasks.py`: the user's task list in its own SQLite file `tasks.db` (never cleaned up, local only,
  no ClickUp sync). Dashboard Tasks tab; connector `tasks_list` / `tasks_add` / `tasks_update`. From emails store only
  the request, the reason and links.

## Handing changes to the user (always this short list, nothing else)
When code changes are ready, give the user only these steps (adapt the model line if no model changed):
1. On the tower, run `python new_app\deploy_tower.py` in the dashboard folder (or double-click the file).
2. Check the Status block: local model ready, cloud model ON (or the new model listed).
3. Read the last line: `RESTART CLAUDE: YES` or `NO`.
4. If YES: push from the tower yourself (the script only commits locally), then on the laptop run `git pull`, then quit Claude (tray icon -> Quit) and reopen it
   on the laptop and the tower.
5. Ask Claude "dashboard status" to confirm it works.
New Ollama model: set `MODEL_TO_INSTALL` (+ `SWITCH_TO_NEW_MODEL`) at the top of `deploy_tower.py` before step 1.
Cloud model (Ollama Pro, `gpt-oss:120b-cloud`): `cloud_llm` in `new_app/config.json`; deep work goes to the cloud,
background triage stays local (see `LOCAL_ONLY_KINDS` in `aws_local.py`).
