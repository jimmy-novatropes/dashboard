# AWS Error Feed dashboard — handoff / context (as of 2026-10-21)

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
| Runs | `new_app` dashboard (scheduled task **"AWS Error Feed"**, pythonw, port **8766**, log `new_app\dashboard.log`), Ollama 0.40.x **qwen3:14b** (RTX A2000 12 GB, 64 GB RAM, i9); `qwen3:30b-a3b` being installed as an alternative | Claude Desktop connector → `new_app\mcp_server.py` pointed at the tower | Browser |
| Access | `https://initium.tail11109.ts.net` (Tailscale serve → 127.0.0.1:8766) | same URL | same URL (Tailscale app) |

Tower `new_app\config.json` (written by `tower_tune.ps1`; a model picked in the dashboard overrides it):
```json
{ "local_llm": { "model": "qwen3:14b", "num_ctx": 16384, "parallel": 1 },
  "grouping": { "drain3": "on" },
  "tuning_note": "fa=1 kv=q8_0 keep=24h par=1 ctx=16384 …",
  "exclude_profiles": ["BSLLC", "codecommituser", "default", "super_suds", "supersuds"] }
```
Tower Ollama user env (set by `tower_tune.ps1`): `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`,
`OLLAMA_KEEP_ALIVE=24h`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`. Tower git: `core.autocrlf true`.

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
| `aws_prefs.py` | Project priority (high ★ / normal / on_demand = not polled until opened, then awake `on_demand_wake_hours`), dashboard-picked settings (meta table), ModelPicker (list / switch / download Ollama models without restart) |
| `tower_tune.ps1` | Tower: Ollama env (flash attention, q8_0 KV, keep_alive 24h), biggest num_ctx that fits the GPU, drain3 install, `tuning_note`, restarts Ollama + dashboard; `-Undo` |
| `mcp_server.py` | Claude connector (runs on whichever machine Claude is on; talks HTTP to the dashboard; reads local repos itself) |
| `install_autostart.ps1` | Registers the scheduled task + `tailscale serve --bg 8766`; `-Remove` undoes it |
| `deploy_tower.py` | Tower helper: (optional git push, off) → download a model (`MODEL_TO_INSTALL`, default qwen3:30b-a3b) → restart dashboard → status. Settings at the top |

## 4. Configuration (`config.json` next to `aws_error_feed.py` is auto-loaded; `-c` overrides; nested keys merge)
Useful keys: `profiles`, `exclude_profiles`, `regions`, `keep_hours` (24), `info_keep_hours` (8: plain info lines dropped
sooner; errors/hidden/warnings/platform keep 24h), `full_lines_per_pattern_hour` (200) + `repeat_line_chars` (1000: later
lines of a busy pattern stored as start+end; JSON lines always whole), `startup_hours` (2, fast start), `slim_db_over_mb`
(800, one-time slim on start), `local_llm` {enabled, url, model, num_ctx, parallel, triage_every_minutes, triage_per_cycle,
triage_top_n, max_input_chars}, `grouping` {drain3: "on" | "shadow" | "off", drain3_similarity 0.4}, `on_demand_wake_hours`
(2), `profile_priority` ({"hrs": "high", …} defaults; dashboard wins), `repos_root`, `record_count_patterns`,
`partner_services`, `infra_*`, `step_functions`, `track_deploys`, `port`.
Settings picked in the dashboard (model, context, project priorities) live in errors.db (`meta`, `profile_prefs`).

## 5. Features (dashboard)

**Feed & classification**
- Levels: error / suspect ("hidden error": try/except prints, 4xx/5xx, caught SFN failures) / warning / info / platform.
- Patterns grouped by type + normalized message; NEW / SPIKE / REGRESSION; mute / mark fixed; team notes.
- **Structured lines** (oflog JSON with `"event"`): level from the line, grouped by event (+partner, status kept: 429 vs 500),
  `run_summary` gives exact record counts; a run with failed records = hidden error.
- **False-alarm reduction**: counters like `records=200, missing=49, skipped=3` and zero counts (`0 failed`, `errors=0`)
  are normal output; non-zero fail/error/timeout counters still flag.
- **"EVERY RUN" badge** (was "LOG FORMAT?"): a hidden-error pattern in ≥80% of a function's runs (≥30 runs) = normal output
  **or a chronic issue** (not "mute it"). Never applied to errors or `run_summary` failed-records patterns. Chronic,
  unchanged patterns are re-triaged weekly (spikes re-triage at once) and listed compactly by the connector.
- **Drain3 grouping ON**: patterns are Drain3 templates (`<*>` = variable part). At start, stored patterns, hourly baseline,
  mutes, notes and triage are renamed to their template; live template changes are remapped. Never merged: different error
  types, different HTTP codes (429 vs 500), `run_summary`, messages < 3 tokens. Undo: `grouping.drain3 = "shadow"`.
  Side effect: a mute on one old pattern covers its whole merged group.
- Partner API health, deploy tracking with before/after verdicts, did-my-fix-work.

**Clients tab**: red/amber/green cards, ★ priority projects first, then by most errors (#1, #2…), 1h/6h/24h windows,
recent deploys, reasons, priority dropdown on each card. On-demand projects sit asleep in a strip below with "load ▸".
"Connecting…" (amber) while an account's first pull runs.
**Other tabs**: Errors, Hidden errors, All logs, Patterns, Volume, Deploys, Partner APIs, Runs, Trace a record, Schedules,
Inventory, APIs & alarms, Digest, **Health**. Account chips collapse into one "▸ Accounts: N connected" line (problems always shown).
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

**Model picker** (top of Health tab; connector `more_tools("switch_model")`): list installed Ollama models, switch model +
context without restart (10–30 s load), download new ones in the background; persists over config.json; each switch is
logged in the day-by-day table. Endpoints `/api/ai/models`, `/api/ai/model?name=&num_ctx=`, `/api/ai/pull?name=`.
**Project priority** (Clients tab dropdown, profile menu shows ★ / on demand; connector `more_tools("project_priority")`):
`high` = first everywhere (clients, overview, digest, menu) + triaged first; `normal`; `on_demand` = asleep: not polled,
not infra-scanned, not triaged, excluded from all-client views/summary/events/volume/partners. Opening it (card "load ▸",
picking it in the profile menu, or any connector call with `profile=`) wakes it: catches up 2h live + backfills 24h,
stays awake `on_demand_wake_hours`, connector answers carry a "was asleep, loading" note. Endpoints `/api/prefs`,
`/api/prefs/set?profile=&priority=`, `/api/prefs/wake?profile=`.

**Health tab** (`/api/health`, `/api/health/text`, connector `more_tools("system_health")`): what to improve next,
model speed / queue / GPU fit / inputs cut / answers cut / bad JSON / reloads, per-kind stats, triage quality (unclear,
claims not backed by lines), cache hit rate, day-by-day table with the settings active each day (before/after compare),
connector tool time/size/errors, Drain3 shadow merge candidates, system log (model errors, triage/job failures,
crashed threads, AWS accounts disconnecting). 🤖 line also shows queue, backlog, tok/s and GPU %.
Model calls go through a priority gate: requests Claude is waiting for run before background triage.

## 6. Claude connector tools

Default **core mode** (≈3.4k tokens of tool definitions per message instead of ≈6.4k). `AWS_ERROR_FEED_TOOLS=all` lists
everything; `AWS_ERROR_FEED_EXTRA_TOOLS="list_runs,get_run"` adds specific ones.

Core (19 + more_tools): `clients_status`, `dashboard_status`, `investigate`, `fix_brief`, `error_overview`,
`list_error_patterns`, `summarize_logs`, `get_log_lines`, `get_line_context`, `did_my_fix_work`, `find_code`, `get_symbol`,
`outline`, `repo_map`, `local_read`, `local_git`, `local_draft_change`, `local_run`, `usage_savings`, **`more_tools`**.

Via `more_tools(name, args=JSON)`: add_note, apis_and_alarms, client_report (`tower_summary`), cost_estimate, daily_digest,
did_nothing_check, draft_commit_message, get_run, get_step_function_input, inventory, list_deploys, list_muted, list_runs,
load_history, locate_code, log_group_volume (top 25 + summary, `limit=0` all), mute_pattern, partner_api_health,
**project_priority**, rescan_infrastructure, schedules_health, secrets_overview, **switch_model**, **system_health**,
trace_record, unmute_pattern, upgrade_checklist, whats_new.

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
- Trimmed outputs (brief by default; `detail="full"`, `investigate`, `fix_brief`, `get_log_lines` keep full detail):
  `list_error_patterns` (compact "where" line, one-line tower verdict, example only if it adds something, tracebacks keep
  start + exception, chronic every-run patterns one line each at the end), `error_overview` (chronic folded per client,
  ★ first, asleep listed), `list_runs` (consecutive runs with the same problem folded), `get_run` (a problem line repeated
  in a run is counted once; info runs folded), `log_group_volume` (top 25). Test fixture: ~102k → ~12k chars (−89%);
  live `list_error_patterns(ncc)` ~8.5k → ~2.8k. `MAX_OUT` 40k. Every tool reports size, time and errors (Health tab).
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
Shortcut on the tower: `python new_app\deploy_tower.py` (model download + restart + status).
Tower tuning: `powershell -ExecutionPolicy Bypass -File ".\new_app\tower_tune.ps1"` (`-Undo` reverts).

Troubleshooting:
- Path with a space → quote it in PowerShell.
- `tailscale … NoState` → `tailscale up` (same account on both machines); HTTPS must be enabled in the Tailscale admin DNS page.
- Laptop browser "DNS address could not be found" → Brave secure DNS bypasses MagicDNS; disable it or use another browser.
- Local model off / wrong model → `new_app\config.json` missing or misnamed; `ollama list`.
- Connector "can't open file" → wrong `args` path for that machine (tower user is `jagom`, laptop `jagom.ADVENTUM`).
- Port in use → stop the old pythonw process (command above) or `Get-NetTCPConnection -LocalPort 8766`.
- git shows files modified but content identical → line endings: `git config core.autocrlf true` then `git checkout -- new_app`.
- Connector output still in the old format → Claude Desktop wasn't fully restarted (tray → Quit).
- Dashboard started before Ollama → it picks the model up within a minute by itself.

## 9. What the stats showed (2026-10-20/21)

- Savings: ~90% of log reading on the tower (7d: tower read ~1.2M tokens, Claude received ~120k).
- Tower after tuning: qwen3:14b 100% on GPU at 16k context, 0 failed calls, ~20–21 tok/s, avg ~13s / p95 ~28s per call,
  Claude-request wait p95 0–11s, triage 13.5% unclear, 8.7% unbacked claims, backlog ~0. Cache hit 0% (expected for logs).
- errors.db was 1.5 GB → slimmed to ~0.8 GB; info lines are ~95% of volume (~2.2M lines/day).
- Drain3: 213 → 111 patterns in shadow (−48%); live ~140 → ~89.
- Biggest senders to Claude before trimming: get_run (~26k), list_runs (~22k), list_error_patterns (~14k), error_overview (~11k).
- Biggest Claude credit levers left (user side): new chat per task, turn off unused connectors (Gmail/Drive/Calendar/
  ClickUp/Fireflies) in dev chats, smaller model for routine checks.

**Chronic issues found (real bugs, not noise; ~75k problem lines/day, client code NOT changed yet):**
- ncc `4_process_associations`: MySQL deadlocks (1213) + lock wait timeouts (1205), transactions rolled back, ~26–69k/day,
  every run → needs retry-with-backoff / smaller transactions / ordered locking.
- ncc `7a_`: HubSpot rejects `order_id` already on another deal (VALIDATION_ERROR), ~4–10k/day, each failure logged twice,
  and the code then fails parsing the error (`Expecting property name enclosed in double quotes` — parses a Python repr /
  bytes as JSON).
- statewide `statewide-query`: empty seller's-attorney email `''` looked up in contact map, ~43k/day, spiked 3.4× → skip empty.
- hrs `set-company-props-in-deal`: HubSpot 429 retries ~81k/day on `/crm/v3/objects/deals/search` (handled by retry; only a
  failure if retries run out) → throttle/batch searches. HRS changes on hold (push to main deploys all Lambdas).

## 10. Open items / next steps

- **Pending on the tower**: run `python new_app\deploy_tower.py` (downloads qwen3:30b-a3b, restarts the dashboard), then
  fully quit + reopen Claude. Then set ★ / on-demand projects on the Clients tab. Try the 30B model with the picker and
  compare unclear % / tok/s / p95 / Claude wait in the Health day-by-day table for 1–2 days (expect slower calls: it
  doesn't fit 12 GB, part runs on CPU; maybe context 8k).
- Commits since 404294e (0f9f424 model picker + priority, c77beaa/19a799f deploy_tower.py, this HANDOFF) were made on the
  tower: `git push` there, then `git pull` on the laptop.
- Optional: quiet the harmless `ConnectionAbortedError` tracebacks in dashboard.log (client closed the request early).
- Next options discussed: trim `get_log_lines` / `investigate` / `fix_brief` further (tower summary + key lines);
  fix the chronic ncc / statewide issues in client code (HRS on hold); install codebase-memory-mcp on the laptop
  (code graph MCP, ~10× fewer tokens for code exploration); rtk only if using Claude Code.
- Verify the counter fix: `list_error_patterns(profile="hrs")` — the `missing=` progress pattern should stop growing.
- HRS findings (not acted on, user said hold HRS changes):
  - `hrs-assoc-compliance-2-companies-hs`: ~600 failed records per run, HubSpot `VALIDATION_ERROR … is not valid` on
    association batch create (`/crm/v4/associations/2-59343072/2-59343072/batch/create`), spiking.
  - `look-4-insurance-agents` progress print → a `local_draft_change` to convert it to `oflog.log("INFO","page_progress",…)`
    was drafted (handler.py lines 351-355) but NOT applied.
  - `hrs_record_creations` Step Function: `HRSSyncFailed` in `SetCompanyPropsInDealLambda` (34×/24h).
- **keap_praxis security**: `.env` (Keap API key + HubSpot token) committed and pushed since the first commit; no
  `.gitignore`; 5 customer export files tracked. Rotate both keys, add .gitignore, `git rm --cached`.
- Not built yet (discussed): Savings charts; LLMLingua-2 compression on the tower (after Drain3, lower value);
  anew / other repos oflog conversion; CLAUDE.md for remaining client repos.
- Researched and skipped: houtini-lm / OllamaClaude (duplicate the local_* tools), token-optimizer (overlaps core mode).
