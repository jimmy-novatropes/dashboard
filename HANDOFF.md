# AWS Error Feed dashboard — handoff / context (as of 2026-10-22, midday)

Paste this into a new chat to continue work on the dashboard. Owner: Jimmy (Open Flow). Preference: brief, direct answers,
settings as variables at the top of files, short step lists.
This replaces the 10-21 handoff and the addenda (tasks-and-efficiency, cloud-model-routing, usage-stats).

---

## 0. Current state (everything below is live)

- Tower dashboard running the latest code since 2026-10-22 12:11 (`deploy_tower.py` run). 16 AWS accounts connected.
- **Three AI layers, load-balanced by job type** (see §5): local GPU model for background work, Ollama Pro cloud model
  for deep work, Claude only gets short answers.
- Local model **qwen3:30b-a3b** (16k ctx, ~54% on GPU, ~36–44 tok/s, unclear 6.7% vs 13.3% on qwen3:14b → keep it).
- Cloud model **gpt-oss:120b-cloud** via Ollama Pro ($20/mo, $60 credit/cycle, renews the 22nd) — tested, working.
- Health tab shows local / cloud / Claude usage and "what to improve next".
- Git: last commit `4117bae` on the tower is **not pushed** (Jimmy pushes manually).

## 1. What it is

A local Python app that watches **CloudWatch Logs + Step Functions across all AWS CLI profiles** (one profile = one client),
stores a 24h cache in SQLite, and serves a web dashboard + a **Claude connector (MCP server)**. Model work is split:
**local model (Ollama on the tower GPU)** does background triage and small jobs, **cloud model (Ollama Pro)** does the
deep reading Claude asks for, so Claude itself uses few tokens.

- Read-only toward AWS. Never reads secret values (Secrets Manager metadata only), never stores Lambda env values.
- No login → shared only inside the Tailscale tailnet (`tailscale serve`, never `funnel`).
- No event-logging changes on Lambdas/Step Functions (no CloudWatch config changes); errors are caught via `print` /
  structured log lines in the code instead.
- Privacy note: cloud calls send log lines / code snippets to Ollama's servers (secret-like files are never sent).

## 2. Machines

| | Tower **initium** (does the work) | Laptop **adventum** (where repos are edited) | Phone |
|---|---|---|---|
| Dashboard path | `C:\Users\jagom\Documents\Openflow Repos\dashboard` | `C:\Users\jagom.ADVENTUM\Documents\Openflow\dashboard` | — |
| Python | `C:\Users\jagom\AppData\Local\Programs\Python\Python311\python.exe` | `C:\Users\jagom.ADVENTUM\AppData\Local\Programs\Python\Python312\python.exe` | — |
| Runs | `new_app` dashboard (scheduled task **"AWS Error Feed"**, pythonw, port **8766**, log `new_app\dashboard.log`), Ollama 0.40.7 signed in to Jimmy's ollama.com Pro account, models qwen3:30b-a3b (in use), qwen3:14b, gpt-oss:120b-cloud | Claude Desktop connector → `new_app\mcp_server.py` pointed at the tower | Browser |
| Access | `https://initium.tail11109.ts.net` (Tailscale serve → 127.0.0.1:8766) | same URL | same URL (Tailscale app) |

Tower hardware: Lenovo ThinkStation P360 Tower (30FM002KUS), i9-12900, 64 GB DDR5 (2 free slots, max 128), RTX A2000
12 GB (70 W), 750 W PSU (per retailer spec), 1 TB NVMe + 4 TB Samsung T7. Upgrade research done; decision was Ollama Pro
instead of hardware (best later options: RTX PRO 4000 Blackwell SFF 24 GB, or a 128 GB Strix Halo box).

Tower `new_app\config.json` (git-ignored; a model picked in the dashboard overrides `local_llm.model`):
```json
{ "local_llm": { "model": "qwen3:14b", "num_ctx": 16384, "parallel": 1 },
  "exclude_profiles": ["BSLLC", "codecommituser", "default", "super_suds", "supersuds"],
  "grouping": { "drain3": "on" },
  "tuning_note": "fa=1 kv=q8_0 keep=24h par=1 ctx=16384 2026-10-20",
  "cloud_llm": { "enabled": true, "model": "gpt-oss:120b-cloud", "daily_token_cap": 3000000, "parallel": 3,
                 "local_kinds": ["triage", "repo_map", "filemap", "other"],
                 "billing_day": 22, "monthly_credit_usd": 60 } }
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
Priorities: ★ high = hrs, stellaromics, anew, sterling, reliable_roofing, hiniker; others normal; none on-demand.

## 3. Deploying updates (one step) — `new_app/deploy_tower.py`

Run on the tower from the dashboard folder: `python new_app\deploy_tower.py` (or run/double-click it from PyCharm /
Explorer). Settings are variables at the top of the file. What it does, in order:
1. Compile check of `CHECK_FILES` → stops if broken (running dashboard untouched).
2. Local commit of `COMMIT_PATHS` (`COMMIT = True`; message auto "Update: <files>" unless `COMMIT_MESSAGE` set).
   **`PUSH = False`** — Jimmy pushes manually. `PUSH = True` = `git pull --rebase --autostash` + push.
3. Optional model download (`MODEL_TO_INSTALL`, `SWITCH_TO_NEW_MODEL`, `NEW_MODEL_CONTEXT`).
4. Stops the pythonw dashboard, starts scheduled task "AWS Error Feed", waits until it answers.
5. Status: local model, cloud model ON/OFF + today's usage, installed models, tasks.db, priorities, dashboard.log tail.
6. `RESTART CLAUDE: YES/NO` — YES only if `mcp_server.py` changed since the last run (hash in git-ignored
   `new_app/last_deploy.json`).

**Steps to give Jimmy after every change (always this list, nothing longer):**
1. On the tower, run `python new_app\deploy_tower.py`.
2. Check the Status block: local model ready, cloud model ON (or the new model listed).
3. Read the last line: `RESTART CLAUDE: YES` or `NO`.
4. If YES: push from the tower, `git pull` on the laptop, then quit Claude (tray → Quit) and reopen on laptop + tower.
5. Ask Claude "dashboard status" (or check the Health tab) to confirm.

Editing flow used so far: Claude edits the tower's files directly (Cowork, device bridge → connected folder
`dashboard`), with a python read-modify-write that keeps line endings, then `py_compile` + a fake-server test, then
Jimmy runs deploy_tower.py. No extra launcher files (.cmd) — Jimmy wants it all in deploy_tower.py.

## 4. Repo layout (GitHub `jimmy-novatropes/dashboard`)

```
dashboard/
  old_app/       single-computer version (port 8765, local model OFF) - stable, change only shared fixes
  new_app/       the one in use (port 8766, local + cloud model, multi-device)
  logging_kit/   oflog.py (structured logs for client Lambdas) + apply_oflog.py (adds it to a repo)
  CLAUDE.md, README.md, HANDOFF.md, .gitignore (errors.db*, tasks.db*, *.log, config.json, last_deploy.json,
                 repos.json, __pycache__, .idea)
```
`old_app` and `new_app` stay in sync for shared fixes; they differ in port, local-model default, `mcp_server.py` default
URL, `install_autostart.ps1`, tasks and cloud routing / cloud + Claude stats (new_app only).

### new_app modules
| File | Role |
|---|---|
| `aws_error_feed.py` (~3.4k lines) | Main app: Store (SQLite), Worker per profile/region, StepFunctionsScanner, classification, HTTP API + the whole HTML/JS page (`PAGE = r"""…"""`), `main()` |
| `aws_infra.py` | Infra scanner: schedules / missed runs, Lambda inventory + runtimes + layers, secrets metadata, API Gateway, alarms, cost |
| `aws_ops.py` | Runs, did-nothing, trace a record, code index, notes, client reports/cards, structured-line parsing |
| `aws_local.py` | `LocalLLM` (Ollama) with **local/cloud routing** (§5), Gate (priority queue), background Triage (+ evidence check), Jobs (summaries, map-reduce reads, templates, file map), PCache (30-day answer cache), savings counter |
| `aws_health.py` | Health tab: `llm_calls`, `tool_calls`, `counters`, `sys_events`, **`cloud_usage`** (400 days), Drain3, model comparison, **cloud stats, Claude stats, AI usage overview**, "what to improve next" rules (thresholds at the top) |
| `aws_prefs.py` | Project priority, dashboard-picked settings (meta table), ModelPicker (list / switch / download models without restart) |
| `aws_tasks.py` | Task list in its own `tasks.db` (git-ignored, never slimmed, local only, no ClickUp) |
| `tower_tune.ps1` | Ollama env tuning, biggest num_ctx that fits, drain3 install; `-Undo` |
| `mcp_server.py` | Claude connector (talks HTTP to the dashboard; reads local repos itself) |
| `install_autostart.ps1` | Scheduled task + `tailscale serve --bg 8766`; `-Remove` undoes it |
| `deploy_tower.py` | One-step deploy (§3) |

## 5. Load balancing between the three AI layers

| Layer | What runs there | Where |
|---|---|---|
| **Local model** (tower GPU, free) | background `triage` of new/spiking patterns, `repo_map` / `filemap` one-liners, `other` (`LOCAL_ONLY_KINDS` in aws_local.py = `cloud_llm.local_kinds`) | Ollama 127.0.0.1:11434, model from the picker |
| **Cloud model** (Ollama Pro, per-token) | everything someone waits for: `fix` (fix_brief diagnosis), `read` (local_read map-reduce), `summary` / `lines` (summarize_logs), `edit` (local_draft_change), `commit`, `narrative`, `run` | same local Ollama URL, model `gpt-oss:120b-cloud` (Ollama proxies it; tower is signed in) |
| **Claude** | gets only the short answers / trimmed tool outputs | via the connector |

- `LocalLLM.route(kind)` → "cloud" if cloud enabled, kind not local-only, and today's tokens < `daily_token_cap`.
- Cloud calls: own Gate (3 at once = Pro limit), 64k context, 180k input chars (bigger chunks → fewer map-reduce calls).
  Local calls: Gate(1); Claude-waiting calls go before background triage.
- **Fallback**: any cloud error (offline, credit used up, model missing) → same call on the local model; counted as
  `cloud:fallback`, logged in the Health system log (`cloud_model`).
- **Daily cap** 3M tokens (≈ $0.5–2/day) → local until midnight. Tokens = Ollama's counts, else chars/4.
- Cache keys include the routed model. Health "Model comparison" lists the cloud model as its own row.
- Off switch: `cloud_llm.enabled = false` + deploy. Usage baseline: ~68 model requests/day, ~2k tokens in / 170 out →
  ~$1/month on gpt-oss:120b (well inside the $60 credit).
- Ollama Pro billing: $60 credit/cycle, unused credit doesn't roll over; over-credit usage only from pre-bought credits
  (keep auto top-up OFF at ollama.com settings). Ollama emails at 90%.
- The Ollama requests every ~2–5 s in the Ollama log = the dashboard's 🤖 status line (each open tab polls every 5 s →
  /api/tags, /api/version, /api/ps). No tokens, local only.

## 6. Features (dashboard)

**Feed & classification**
- Levels: error / suspect ("hidden error": try/except prints, 4xx/5xx, caught SFN failures) / warning / info / platform.
- Patterns grouped by type + normalized message; NEW / SPIKE / REGRESSION; mute / mark fixed; team notes.
- **Structured lines** (oflog JSON with `"event"`): level from the line, grouped by event (+partner, status kept),
  `run_summary` exact record counts; failed records = hidden error. anew `{"check": "INVOCATION_SUMMARY"}` lines parsed
  (crashed=true → error; HTTP ≥400 / http_failures / exceptions_logged → suspect; 429 → warning; clean → info).
- **False-alarm reduction**: counters like `records=200, missing=49` and zero counts are normal output.
- **"EVERY RUN" badge**: hidden-error pattern in ≥80% of a function's runs (≥30 runs) = normal output or chronic issue.
- **Drain3 grouping ON** (templates with `<*>`; never merges different error types / HTTP codes / run_summary).
  Undo: `grouping.drain3 = "shadow"`.
- Partner API health, deploy tracking with before/after verdicts, did-my-fix-work.

**Tabs**: Clients (red/amber/green cards, ★ first, priority dropdown, on-demand strip, open-task count), Errors,
Hidden errors, All logs, Patterns, Volume, Deploys, Partner APIs, Runs, Trace a record, Schedules, Inventory,
APIs & alarms, Digest, **Tasks**, **Health**. Mobile-friendly.

**Status line (every tab)**: 🤖 local model, triaged count, queue, backlog, tok/s, GPU %, **☁ cloud calls / tokens
today (+ fallbacks)**, 💰 tower-read vs Claude-received tokens; 🔁 model picker (switch model + context without restart,
download new models).

**Health tab** (`/api/health`, `/api/health/text`, connector `more_tools("system_health")`):
- What to improve next (bad / warn / info / ok).
- Tower / local model: GPU fit, 1h/24h/7d calls, speed, waits, inputs/answers cut, bad JSON, reloads (local calls only).
- **AI usage overview**: Local / Cloud / Claude — calls today, tokens today + 7d, est. $ today + 7d.
- **Claude usage (via the connector)**: today / 7d / 14d calls + tokens (out_chars ÷ 4 of what the connector returned),
  per tool (share bar, avg/call, time, errors), per day. Claude plan usage itself has no API on Pro/Max/Team (Jimmy's
  plan is a team setup, unknown) → claude.ai Settings → Usage. If it's API/Console: Usage & Cost API (Admin key) could
  add real $; Enterprise: Enterprise Analytics API.
- **Cloud model (Ollama Pro)**: billing cycle (billing_day → next), $ used of credit (bar), projected cycle spend,
  today calls / tokens / $ / % of daily cap, 24h avg/p95, fallbacks, cloud share of model calls, per kind 7d
  (calls, tokens/call, time, $), per day 14d. $ = dashboard's own calls × `CLOUD_PRICES` (ollama.com/pricing Sep 2026,
  overridable in `cloud_llm.prices`); other Ollama use only on ollama.com.
- Model comparison (7d), by kind (24h, "(cloud)" marked), triage quality, day-by-day with settings, connector tools,
  Drain3, system log.
- Rules (constants at the top of aws_health.py): local errors/truncation/speed/queue/reloads/unclear %, plus
  **cloud**: fallbacks ≥10% (bad), projected spend >80% of credit, ≥80% of daily cap, any triage on the cloud,
  cloud p95 >60 s, answers cut >10%, an OK line with spend/projection; **Claude**: >150k connector tokens/day (warn),
  one tool >40% of 7d tokens (trim candidate).

**Tasks** (`aws_tasks.py`, `tasks.db`): title, request, reason, links, client, requester, source, status, priority, due,
external_id (dedupe), error_ref, notes, history. Tasks tab, "+ task" on patterns. Connector `tasks_list` / `tasks_add`
/ `tasks_update`. From emails only request + reason + links are stored.

## 7. Claude connector tools

Default **core mode** (≈3.4k tokens of tool definitions). `AWS_ERROR_FEED_TOOLS=all` lists everything;
`AWS_ERROR_FEED_EXTRA_TOOLS="list_runs,get_run"` adds specific ones.
Core: `clients_status`, `dashboard_status`, `investigate`, `fix_brief`, `error_overview`, `list_error_patterns`,
`summarize_logs`, `get_log_lines`, `get_line_context`, `did_my_fix_work`, `find_code`, `get_symbol`, `outline`,
`repo_map`, `local_read`, `local_git`, `local_draft_change`, `local_run`, `usage_savings`, `tasks_list`, `tasks_add`,
`tasks_update`, **`more_tools`** (add_note, apis_and_alarms, client_report, cost_estimate, daily_digest,
did_nothing_check, draft_commit_message, get_run, get_step_function_input, inventory, list_deploys, list_muted,
list_runs, load_history, locate_code, log_group_volume, mute_pattern, partner_api_health, project_priority,
rescan_infrastructure, schedules_health, secrets_overview, switch_model, system_health, trace_record, unmute_pattern,
upgrade_checklist, whats_new).
- `summarize_logs`, `fix_brief`, `local_read`, `local_draft_change`, `draft_commit_message`, client narratives now run
  on the **cloud model** (fallback local); output header still says "Local-model summary".
- Trimmed outputs by default (`detail="full"` for everything). `get_log_lines` brief: repeats folded, problems capped.
- `local_read` / `local_git`: secrets-like files never sent (`.env`, keys, *secret*, *credential*, *password*, *.db).
- `local_run`: allow-listed checks only. Multi-machine: `AWS_ERROR_FEED_URL` (comma list), `AWS_ERROR_FEED_REPOS`.

## 8. Structured logging kit (oflog) and client repos

- `logging_kit/oflog.py`: `@oflog.run` on `lambda_handler` (run_start / run_summary / lambda_failed),
  `oflog.log(level, event, record=…, partner=…)`, `oflog.count()`, `oflog.check_response(resp, label)`.
- `apply_oflog.py <repo>`: adds it to every Lambda folder; keeps CRLF; idempotent.
- **HRS**: applied to all 14 Lambdas (push to `main` deploys ALL Lambdas to prod). Others not converted yet.
- `CLAUDE.md` with "use the tower first" guidance in: dashboard, hrs, anew, keap_praxis.

## 9. Troubleshooting

- Path with a space → quote it in PowerShell. Port in use → stop the old pythonw (deploy_tower.py does it).
- `tailscale … NoState` → `tailscale up`; HTTPS enabled in the Tailscale admin DNS page. Laptop "DNS address could not
  be found" → Brave secure DNS bypasses MagicDNS.
- Local model off / wrong model → `config.json` missing or misnamed; `ollama list`; picker on the Health tab.
- Cloud falling back → `ollama signin` on the tower; check plan/credit at ollama.com; Health system log `cloud_model`.
- Connector "can't open file" → wrong `args` path (tower user `jagom`, laptop `jagom.ADVENTUM`). Connector output in the
  old format → Claude wasn't fully restarted (tray → Quit).
- git shows files modified but identical → `git config core.autocrlf true` then `git checkout -- new_app`.
- Dashboard started before Ollama → picks the model up within a minute.

## 10. Stats snapshot (2026-10-22)

- Savings 7d: tower read ~1.29M tokens, Claude received ~175k → ~87% saved.
- Claude via connector 7d: 55 calls, ~78k tokens; top senders get_log_lines 57%, list_error_patterns 21%.
- Local 24h: 123 calls, 1.6% failed, p95 ~24 s, Claude wait p95 0 s, backlog 0. Triage 7d unclear 16% overall.
- Cloud: cycle 10-22 → 11-22, ~$0 so far. Drain3: 191 raw patterns → 144 (−25%).
- Chronic client issues found (client code NOT changed): ncc `4_process_associations` MySQL deadlocks (1213/1205);
  ncc `7a_` HubSpot duplicate `order_id` VALIDATION_ERROR + broken error parsing; statewide `statewide-query` empty
  seller's-attorney email lookups + HubSpot 429 secondly limit; hrs `set-company-props-in-deal` HubSpot 429 retries.

## 11. Open items / next steps

- Push `4117bae` (and anything newer) from the tower when ready; laptop `git pull`; restart Claude on the laptop once
  (it may still lack the tasks tools / newest connector).
- Remove the 3 committed `new_app/config.json.bak-2026102*` files (harmless, no secrets) and git-ignore `config.json.bak*`.
- Check ollama.com billing settings: auto top-up OFF.
- Watch the Health tab for a few days: cloud spend/projection, fallbacks, Claude top tools (get_log_lines is the
  biggest → candidate for a briefer default or cloud summary first).
- Optional: connector output still says "Local-model summary" for cloud answers → relabel by routed model.
- Earlier backlog: trim `investigate` / `fix_brief` further; fix chronic ncc / statewide issues in client code (HRS on
  hold); codebase-memory-mcp on the laptop; keap_praxis security (`.env` with Keap + HubSpot keys committed → rotate
  keys, add .gitignore, `git rm --cached`); oflog for anew/other repos; CLAUDE.md for remaining client repos;
  HRS findings (assoc-compliance VALIDATION_ERROR ~600/run, look-4-insurance-agents progress print draft not applied,
  `HRSSyncFailed` in SetCompanyPropsInDealLambda).
- old_app is not synced with cloud routing / usage stats (by design: local model off there).
