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
- Web research needing several pages -> `research(question)` (tower searches + reads, returns a cited report).
- Code edits -> `code_change(instruction, files)`: the tower drafts, you review only the diff, then `apply_id` writes it.
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

## Quality loop (new_app/aws_quality.py)
- Before committing / deploying any change (yours or the tower's): `review_change(repo, intent)` = cloud review
  (3 finder passes + 3 verifier votes, Important / Nit / Pre-existing with file:line, risk tier, how to revert).
  `code_change` drafts include it. Max 3 rounds on the same files, then rethink the approach.
- Triage: "actionable" needs a second cloud call to agree (else downgraded, note "verifier disagreed").
- When you learn what a pattern really was: `triage_feedback(profile, error_type, signature, label)`; muting and
  dashboard tasks from an error (error_ref) count too. `quality_report()` (more_tools) = how often triage was right;
  `triage_eval(target)` re-scores the triage on the labeled set (weekly by itself; run it before switching models).
- After fixing an error: `sweep_bug_class(profile, error_type, signature)` finds the same weakness in other repos
  (nightly 02:xx for fixed / confirmed patterns; `sweep_results`). Bad deploys (worse / new errors) become a
  high-priority task with rollback steps.

## Backup when the Claude limit is used up
- `claude_on_ollama.py` (dashboard root, double-click; settings at the top) or `backup_ai/Claude on Ollama.cmd`: the same Claude Code, pointed at Ollama (`glm-5.3:cloud`, background
  `gpt-oss:120b-cloud`) with the aws-error-feed tools. Settings at the top of `backup_ai/claude_ollama.ps1`.
  If you are running on it: keep changes small, use review_change before committing, verify every diff.

## Rules
- Keep `old_app` and `new_app` in sync for shared fixes; they differ only in port (8765/8766), the local
  model default (off/on), `mcp_server.py` default URL and `install_autostart.ps1`.
- Read-only toward AWS. Never read secret values (Secrets Manager metadata only), never store Lambda env values.
- `config.json`, `errors.db*`, `tasks.db*`, `personal.db*`, `*.log`, `repos.json` are local and git-ignored — never commit them.
- The dashboard has no login: share only inside the tailnet (`tailscale serve`), never `tailscale funnel`.
- After changing `new_app`: the user runs `new_app/deploy_tower.py` on the tower (one step: compile check, local commit (no push - the user pushes manually), restart, status, "RESTART CLAUDE: YES/NO").

## Tasks (tasks.db)
- `new_app/aws_tasks.py`: the user's task list in its own SQLite file `tasks.db` (never cleaned up, local only,
  no ClickUp sync). Dashboard Tasks tab; connector `tasks_list` / `tasks_add` / `tasks_update`. From emails store only
  the request, the reason and links.

## Personal tasks (personal.db)
- `new_app/aws_personal.py`: personal to-dos, separate from work tasks, in git-ignored `personal.db`. Dashboard tab "Personal".
- A background researcher (Ollama cloud model, kind `personal`, background routing) plans searches, reads pages with
  Ollama web search/fetch (same key as `research`) and fills **What to do next** + findings + sources. Open tasks are
  re-researched every `REFRESH_DAYS` (7). Settings at the top of `aws_personal.py`.
- Connector: `personal_add`, `personal_list` (core), `personal_update` (more_tools). Never research personal tasks with
  Claude - the tower does it.
- API: GET `/api/personal?status=&q=`, `/api/personal/get|delete|research?id=`, `/api/personal/counts`;
  POST `/api/personal/add {items:[…]}`, `/api/personal/update {id, …, research: true}`.

## Handing changes to the user (always this short list, nothing else)
When code changes are ready, give the user only these steps (adapt the model line if no model changed):
1. On the tower, run `python new_app\deploy_tower.py` in the dashboard folder (or double-click the file).
2. Check the Status block: local model ready, cloud model ON (or the new model listed).
3. Read the last line: `RESTART CLAUDE: YES` or `NO`.
4. If YES: push from the tower yourself (the script only commits locally), then on the laptop run `git pull`, then quit Claude (tray icon -> Quit) and reopen it
   on the laptop and the tower.
5. Ask Claude "dashboard status" to confirm it works.
New Ollama model: set `MODEL_TO_INSTALL` (+ `SWITCH_TO_NEW_MODEL`) at the top of `deploy_tower.py` before step 1.
Cloud model (Ollama Pro, `gpt-oss:120b-cloud`): `cloud_llm` in `new_app/config.json`; all model work goes to the cloud,
background triage included (back to the local GPU after a cloud failure or above `BACKGROUND_CLOUD_MAX_PCT` of the
daily cap; `LOCAL_ONLY_KINDS` in `aws_local.py`, `cloud_llm.local_kinds` in config.json).
Long results of get_log_lines / list_error_patterns / investigate / clients_status come back as a cloud-model
summary; add `raw=True` only to check exact lines before editing. `tasks_list` is one line per task (`detail="full"` for more).

<!-- graphify-usage:start -->
## Graphify MCP (all client repos)
- MCP server `graphify` = one merged graph of every client repo (`Openflow Repos\_graphify\merged-graph.json`,
  refreshed nightly 03:15 by `setup_graphify.py`). Use it for structure: who calls what, impact of a change,
  the same pattern across clients. Text search -> `find_code`; errors -> `ask_tower` / `fix_brief`.
- Client known -> always pass `project_path` = that repo folder (e.g. `C:\Users\jagom\Documents\Openflow Repos\stellaromics`).
  Without it the merged graph mixes clients and returns unrelated files.
- Keep answers small: `token_budget` 500-1000, `depth` 1-2, `context_filter=["call"]` when tracing calls; raise only if needed.
- Never `get_node` / `get_neighbors` a generic name (`lambda_handler`, `handler`, `main`, `load_secrets`): it lists
  hundreds of matches. Use the full node id (`<repo>::<path>_<name>`) or a client-specific function name.
- `god_nodes` on the merged graph is copied boilerplate (load_secrets, is_running_in_lambda): use it per repo with
  `exclude_hubs_percentile=99`.
<!-- graphify-usage:end -->

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- When graphify-out/graph.json exists, run `graphify query "<question>"` before your first grep, search or source-file read for a codebase question, and put this rule in every subagent prompt that explores code. Read raw files only for lines the graph or an earlier read already located. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
- A committed graphify-out/graph.json is rewritten whole on every build, so a large diff to it in a pull request is expected. Do not flag it as churn or suggest skipping the graph rebuild or its hooks.
