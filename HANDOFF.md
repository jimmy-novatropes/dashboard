# AWS Error Feed dashboard — handoff / context (as of 2026-10-20)

Paste this into a new chat to continue work on the dashboard. Owner: Jimmy (Open Flow). Preference: brief, direct answers.

---

## 1. What it is

A local Python app that watches **CloudWatch Logs + Step Functions across all AWS CLI profiles** (one profile = one client),
stores a 24h cache in SQLite, and serves a web dashboard + a **Claude connector (MCP server)**. A **local model (Ollama on the
tower)** does the heavy reading (triage, log/code summaries, small code edits) so Claude uses fewer tokens.

- Read-only toward AWS. Never reads secret values (Secrets Manager metadata only), never stores Lambda env values.
- No login → shared only inside the Tailscale tailnet (`tailscale serve`, never `funnel`).
- The user does NOT want event-logging changes on Lambdas/Step Functions (no CloudWatch config changes); errors are caught
  via `print` / structured log lines in the code instead.

## 2. Machines

| | Tower **initium** (does the work) | Laptop **adventum** (where repos are edited) | Phone |
|---|---|---|---|
| User / dashboard path | `C:\Users\jagom\Documents\Openflow Repos\dashboard` | `C:\Users\jagom.ADVENTUM\Documents\Openflow\dashboard` | — |
| Python | `C:\Users\jagom\AppData\Local\Programs\Python\Python311\python.exe` | `C:\Users\jagom.ADVENTUM\AppData\Local\Programs\Python\Python312\python.exe` | — |
| Runs | `new_app` dashboard (scheduled task **"AWS Error Feed"**, pythonw, port **8766**, log `new_app\dashboard.log`), Ollama **qwen3:14b** (RTX A2000 12 GB, 64 GB RAM, i9) | Claude Desktop connector → `new_app\mcp_server.py` pointed at the tower | Browser |
| Access | `https://initium.tail11109.ts.net` (Tailscale serve → 127.0.0.1:8766) | same URL | same URL (Tailscale app) |

Tower `new_app\config.json`:
```json
{ "local_llm": { "model": "qwen3:14b", "num_ctx": 8192 },
  "exclude_profiles": ["BSLLC", "codecommituser", "default", "super_suds", "supersuds"] }
```

Laptop `claude_desktop_config.json` entry:
```json
"mcpServers": { "aws-error-feed": {
  "command": "C:\\Users\\jagom.ADVENTUM\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
  "args": ["C:\\Users\\jagom.ADVENTUM\\Documents\\Openflow\\dashboard\\new_app\\mcp_server.py"],
  "env": { "AWS_ERROR_FEED_URL": "https://initium.tail11109.ts.net",
           "AWS_ERROR_FEED_REPOS": "C:\\Users\\jagom.ADVENTUM\\Documents\\Openflow" } } }
```
Tower's Claude Desktop runs `...\Openflow Repos\dashboard\new_app\mcp_server.py` with no env (defaults to 127.0.0.1:8766).

Watched accounts (16): anew, cielo, corganics, hiniker, hrs, intead, jackie_london, kindlyhuman, konsylcodecommituser,
ncc (eu-north-1), puretech, reliable_roofing, statewide, stellaromics, sterling, uniqode.

## 3. Repo layout (GitHub `jimmy-novatropes/dashboard`)

```
dashboard/
  old_app/       single-computer version (port 8765, local model OFF) - stable, change only shared fixes
  new_app/       the one in use (port 8766, local model ON, multi-device)
  logging_kit/   oflog.py (structured logs for client Lambdas) + apply_oflog.py (adds it to a repo)
  CLAUDE.md, README.md, .gitignore (errors.db*, *.log, config.json, repos.json, __pycache__, .idea)
```
`old_app` and `new_app` must stay in sync for shared fixes; they differ only in port, local-model default, `mcp_server.py`
default URL and `install_autostart.ps1` (new_app only).

### new_app modules
| File | Role |
|---|---|
| `aws_error_feed.py` (~3.1k lines) | Main app: Store (SQLite), Worker per profile/region, StepFunctionsScanner, classification, HTTP API + the whole HTML/JS page, `main()` |
| `aws_infra.py` | Infra scanner: schedules / missed runs, Lambda inventory + runtimes + layers, secrets metadata, API Gateway, alarms, cost |
| `aws_ops.py` | Runs (REPORT-based), did-nothing, trace a record, code index (function → repo folder), notes, client reports, client cards, structured-line parsing (`structured()`, `structured_count()`) |
| `aws_local.py` | Local model: LocalLLM (Ollama), background Triage (+ evidence check), Jobs (summaries, map-reduce reads, templates, file map), PCache (answers cached 30 days in `ai_cache`), savings counter (`ai_usage`), Gate (Claude requests before triage) |
| `aws_health.py` | Health tab: model-call telemetry (`llm_calls`), connector tool calls (`tool_calls`), daily `counters`, own error log (`sys_events`), Drain3 shadow grouping, "what to improve next" rules (thresholds at the top) |
| `tower_tune.ps1` | Tower: Ollama env (flash attention, q8_0 KV, keep_alive 24h), biggest num_ctx that fits the GPU, drain3 install, `tuning_note`, restarts Ollama + dashboard; `-Undo` |
| `mcp_server.py` | Claude connector (runs on whichever machine Claude is on; talks HTTP to the dashboard; reads local repos itself) |
| `install_autostart.ps1` | Registers the scheduled task + `tailscale serve --bg 8766`; `-Remove` undoes it |

## 4. Configuration (`config.json` next to `aws_error_feed.py` is auto-loaded; `-c` overrides; nested keys merge)
Useful keys: `profiles`, `exclude_profiles`, `regions`, `keep_hours` (24), `startup_hours` (2, fast start),
`slim_db_over_mb` (800, one-time slim on start), `local_llm` {enabled, url, model, num_ctx, triage_every_minutes,
triage_per_cycle, triage_top_n, max_input_chars}, `repos_root`, `record_count_patterns`, `partner_services`, `infra_*`,
`step_functions`, `track_deploys`, `port`.

## 5. Features (dashboard)

**Feed & classification**
- Levels: error / suspect ("hidden error": try/except prints, 4xx/5xx, caught SFN failures) / warning / info / platform.
- Patterns grouped by type + normalized message; NEW / SPIKE / REGRESSION; mute / mark fixed; team notes.
- **Structured lines** (oflog JSON with `"event"`): level from the line, grouped by event (+partner, status kept: 429 vs 500),
  `run_summary` gives exact record counts; a run with failed records = hidden error.
- **False-alarm reduction**: counters like `records=200, missing=49, skipped=3` and zero counts (`0 failed`, `errors=0`)
  are normal output; non-zero fail/error/timeout counters still flag.
- **"LOG FORMAT?" flag**: a hidden-error pattern in ≥80% of a function's runs (≥30 runs) is flagged as probably normal
  output (suggest mute/note). Never applied to errors or `run_summary` failed-records patterns.
- Partner API health, deploy tracking with before/after verdicts, did-my-fix-work.

**Clients tab**: red/amber/green cards sorted by most errors (#1, #2…), 1h/6h/24h windows, recent deploys, reasons.
"Connecting…" (amber) while an account's first pull runs.
**Other tabs**: Errors, Hidden errors, All logs, Patterns, Volume, Deploys, Partner APIs, Runs, Trace a record, Schedules,
Inventory, APIs & alarms, Digest. Account chips collapse into one "▸ Accounts: N connected" line (problems always shown).
Mobile-friendly layout.
**Performance**: fast start (2h live catch-up, rest backfilled), one-time DB slim, batched deletes, WAL size limit,
old long-range pulls not resumed after restart.

**Local model (tower)**
- Background triage of new/spiking/busy patterns: verdict, category, summary, cause, next step, **evidence quote**;
  claims (rate limit / auth / outage / timeout / config) not visible in the lines are downgraded to "unclear"
  (also applied to old cached verdicts when displayed).
- 🤖 line: model status, triaged count, **💰 savings** (tower tokens read vs Claude tokens received, % saved).
- Endpoints: `GET /api/ai/status|savings|job|triage_map|summarize|claude_usage`, `POST /api/ai/read` (map-reduce reading),
  `POST /api/ai/prompt` (templates: edit, commit, fix, narrative, run, filemap), `POST /api/ai/filemap[_known]`.

**Health tab** (`/api/health`, `/api/health/text`, connector `more_tools("system_health")`): what to improve next,
model speed / queue / GPU fit / inputs cut / answers cut / bad JSON / reloads, per-kind stats, triage quality (unclear,
claims not backed by lines), cache hit rate, day-by-day table with the settings active each day (before/after compare),
connector tool time/size/errors, Drain3 shadow merge candidates, system log (model errors, triage/job failures,
crashed threads, AWS accounts disconnecting). 🤖 line also shows queue, backlog, tok/s and GPU %.
Config: `local_llm.parallel` (1), `grouping.drain3` ("shadow" | "off"), `grouping.drain3_similarity` (0.4).

## 6. Claude connector tools

Default **core mode** (≈3.4k tokens of tool definitions per message instead of ≈6.4k). `AWS_ERROR_FEED_TOOLS=all` lists
everything; `AWS_ERROR_FEED_EXTRA_TOOLS="list_runs,get_run"` adds specific ones.

Core (19 + more_tools): `clients_status`, `dashboard_status`, `investigate`, `fix_brief`, `error_overview`,
`list_error_patterns`, `summarize_logs`, `get_log_lines`, `get_line_context`, `did_my_fix_work`, `find_code`, `get_symbol`,
`outline`, `repo_map`, `local_read`, `local_git`, `local_draft_change`, `local_run`, `usage_savings`, **`more_tools`**.

Via `more_tools(name, args=JSON)`: add_note, apis_and_alarms, client_report (`tower_summary`), cost_estimate, daily_digest,
did_nothing_check, draft_commit_message, get_run, get_step_function_input, inventory, list_deploys, list_muted, list_runs,
load_history, locate_code, log_group_volume, mute_pattern, partner_api_health, rescan_infrastructure, schedules_health,
secrets_overview, trace_record, unmute_pattern, upgrade_checklist, whats_new.

Key tools:
- `find_code(query, repo)` — laptop-side ranked line search (no model). `/regex/` supported.
- `outline(file)` signatures only; `get_symbol(name, repo|file)` one function's source (Python via ast; JS rough).
- `repo_map(repo, focus, budget)` — layout + cached one-line purposes; with `focus` returns only relevant files + callers.
- `fix_brief(profile, function, …)` — error, preceding logs (deduped), failing code **or the print that wrote the line**,
  runs, + tower diagnosis (evidence-bound). Poll with `job_id`.
- `local_read(path, question)` / `local_git(repo, what, …)` — tower reads files/folders/git; secrets-like files never sent
  (`.env`, keys, *secret*, *credential*, *password*, *.db); only inside `AWS_ERROR_FEED_REPOS` + dashboard folder.
- `local_draft_change(path, start, end, instruction, apply)` — tower writes a small edit → diff; `apply=True` writes it
  (syntax-checked, line endings kept).
- `local_run(command, repo)` — allow-listed checks only (pytest, py_compile, unittest, python <test script>, npm test/lint,
  node --test, git status, serverless print); no shell operators.
- Trimmed outputs: `get_run` / `list_runs` / `list_error_patterns` default to brief (`detail="full"` restores),
  `error_overview` top 3 per client, `MAX_OUT` 40k chars. Every tool reports its output size to the savings counter.
- Multi-machine: `AWS_ERROR_FEED_URL` (comma list, first reachable wins, local 127.0.0.1 entries can autostart),
  `AWS_ERROR_FEED_REPOS` (repos root on this machine, `;` for several).

## 7. Structured logging kit (oflog) and client repos

- `logging_kit/oflog.py`: `@oflog.run` on `lambda_handler` (run_start / run_summary / lambda_failed), `oflog.log(level,
  event, record=…, partner=…)`, `oflog.count()`, `oflog.check_response(resp, label)` (also catches HubSpot 207 partial
  failures, which were silent before).
- `apply_oflog.py <repo>`: copies oflog into every Lambda folder, converts shared `http_retry.py`, replaces
  `if not resp.ok: print("❌ Error …")` blocks, adds the decorator; keeps CRLF; idempotent.
- **HRS**: applied to all 14 Lambdas (warning: push to `main` deploys ALL Lambdas to prod). Others not converted yet.
- `CLAUDE.md` with "use the tower first" guidance added to: dashboard, hrs, anew, keap_praxis.

## 8. Runbook

Deploy an update:
```
# laptop (dashboard folder):  git add -A ; git commit -m "..." ; git push
# tower (dashboard folder):
git pull
Get-CimInstance Win32_Process -Filter "name='pythonw.exe' or name='python.exe'" | Where-Object { $_.CommandLine -match 'aws_error_feed' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask "AWS Error Feed"
Get-Content "C:\Users\jagom\Documents\Openflow Repos\dashboard\new_app\dashboard.log" -Tail 15   # expect "Config: …" and "Local model: qwen3:14b ready"
```
Then fully quit + reopen Claude (tray → Quit) on laptop and tower when `mcp_server.py` changed.

Troubleshooting:
- Path with a space → quote it in PowerShell.
- `tailscale … NoState` → `tailscale up` (same account on both machines); HTTPS must be enabled in the Tailscale admin DNS page.
- Laptop browser "DNS address could not be found" → Brave secure DNS bypasses MagicDNS; disable it or use another browser.
- Local model off / wrong model → `new_app\config.json` missing or misnamed; `ollama list`.
- Connector "can't open file" → wrong `args` path for that machine (tower user is `jagom`, laptop `jagom.ADVENTUM`).
- Port in use → stop the old pythonw process (command above) or `Get-NetTCPConnection -LocalPort 8766`.

## 9. Open items / next steps

- Verify the counter fix: `list_error_patterns(profile="hrs")` — the `missing=` progress pattern should stop growing.
- HRS findings (not acted on, user said hold HRS changes):
  - `hrs-assoc-compliance-2-companies-hs`: ~600 failed records per run, HubSpot `VALIDATION_ERROR … is not valid` on
    association batch create (`/crm/v4/associations/2-59343072/2-59343072/batch/create`), spiking.
  - `look-4-insurance-agents` progress print → a `local_draft_change` to convert it to `oflog.log("INFO","page_progress",…)`
    was drafted (handler.py lines 351-355) but NOT applied.
  - `hrs_record_creations` Step Function: `HRSSyncFailed` in `SetCompanyPropsInDealLambda` (34×/24h).
- **keap_praxis security**: `.env` (Keap API key + HubSpot token) committed and pushed since the first commit; no
  `.gitignore`; 5 customer export files tracked. Rotate both keys, add .gitignore, `git rm --cached`.
- Not built yet (discussed): Savings tab with charts; LLMLingua-2 compression on the tower; tower queue/GPU status in the
  🤖 line; Noise view; anew / other repos oflog conversion; CLAUDE.md for remaining client repos;
  optional Claude Code Router / ccusage.
- `.gitignore` + `logging_kit/` + `CLAUDE.md` in dashboard: make sure they're committed.
