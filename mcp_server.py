#!/usr/bin/env python3
"""
MCP server for the AWS Error Feed: lets Claude (Claude Code, Claude Desktop) ask the running
dashboard what's failing across your AWS accounts, read the surrounding log lines, and hand
you a link to the same view in the dashboard.

    pip install mcp

Claude Code (available in every repo):
    claude mcp add aws-error-feed -s user -- python "C:\\path\\to\\dashboard\\mcp_server.py"

Claude Desktop: add to claude_desktop_config.json
    "mcpServers": {"aws-error-feed": {"command": "python",
                   "args": ["C:\\\\path\\\\to\\\\dashboard\\\\mcp_server.py"]}}

It talks to the dashboard at http://127.0.0.1:8765 (override with AWS_ERROR_FEED_URL) and starts
the dashboard in the background if it isn't running (set AWS_ERROR_FEED_AUTOSTART=0 to disable).
Read-only: it never changes anything in AWS.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from mcp.server.mcpserver import MCPServer as Server      # mcp >= 2
except ImportError:
    from mcp.server.fastmcp import FastMCP as Server           # mcp 1.x

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("AWS_ERROR_FEED_URL", "http://127.0.0.1:8765").rstrip("/")
AUTOSTART = os.environ.get("AWS_ERROR_FEED_AUTOSTART", "1") != "0"
MAX_OUT = 60_000          # keep tool results a sensible size for the conversation

INSTRUCTIONS = """\
Read-only access to the user's AWS Error Feed dashboard, which watches CloudWatch Logs and
Step Functions across all of their AWS accounts (one AWS CLI profile per client/project).

Levels: error = logged as an error (ERROR/CRITICAL, tracebacks, Lambda crashes/timeouts, failed
Step Functions executions). suspect = "hidden error": not logged as an error but looks like one
(a try/except that print()s "failed to...", a 4xx/5xx status, a Step Functions run that only
succeeded because Retry/Catch absorbed a failure, an execution stuck RUNNING). warning, info,
platform (Lambda REPORT lines) = everything else.

Deploys: list_deploys shows recent Lambda deploys with a before/after verdict; after the user
deploys a fix, use did_my_fix_work(function, profile) to confirm the error is gone (and nothing new
appeared). If the deploy was minutes ago, say it's too early and offer to check again later.

Noise control: whats_new shows new / spiking / regressed patterns. Only mute or mark fixed
(mute_pattern) when the user asks; after a fix is deployed and did_my_fix_work confirms it, offer
to mark the pattern fixed so it's flagged if it ever returns.
Reproducing: for Step Functions failures, get_step_function_input gives the exact payload the
failing Lambda received - use it to write a local test that fails first, then fix.
Partner problems (expired tokens, 429s) across clients: partner_api_health.

Good workflow: error_overview -> list_error_patterns for the project -> get_log_lines for a
pattern -> get_line_context on one line to see what the code printed just before it -> find the
code in the repo (a log group like /aws/lambda/<name> is the Lambda function <name>;
stepfunctions:<name> is a state machine) -> propose a fix. Always include the dashboard link
from the tool output so the user can look at the same view. Only the last 24 hours are loaded
by default; use load_history for older ranges.
"""

mcp = Server("aws-error-feed", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------- helpers

class FeedDown(Exception):
    pass


def _start_dashboard():
    script = os.path.join(HERE, "aws_error_feed.py")
    if not os.path.exists(script):
        return False
    kw = dict(cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, script, "--no-browser"], **kw)
    return True


def api(path, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{BASE}{path}?{q}"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except TimeoutError:
            raise FeedDown(
                f"The dashboard at {BASE} is running but didn't answer within 40 s - it's probably "
                f"busy (e.g. an old version with a very large errors.db, or a big history load). "
                f"Restart it with `python aws_error_feed.py` in {HERE}.") from None
        except (urllib.error.URLError, ConnectionError) as ex:
            if isinstance(getattr(ex, "reason", None), TimeoutError):
                raise FeedDown(f"The dashboard at {BASE} didn't answer within 40 s - restart it.") from None
            if attempt == 0 and AUTOSTART and _start_dashboard():
                for _ in range(30):                     # give it up to ~15 s to come up
                    time.sleep(0.5)
                    try:
                        urllib.request.urlopen(f"{BASE}/api/status", timeout=2).read()
                        break
                    except Exception:
                        continue
                continue
            raise FeedDown(
                f"The AWS Error Feed dashboard isn't reachable at {BASE} ({ex}). Start it with "
                f"`python aws_error_feed.py` in {HERE}.") from None


def link(**params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{BASE}/" + (f"?{q}" if q else "")


def t(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)) if ms else "?"


def cap(text, n):
    return text if len(text) <= n else text[:n] + f"\n… [{len(text) - n} more chars]"


def flags(g):
    f = []
    if g.get("regression"):
        f.append("REGRESSION (was marked fixed)")
    if g.get("is_new"):
        f.append("NEW in last 24h")
    if g.get("is_spike"):
        f.append(f"SPIKE x{g.get('spike_ratio')} vs 7-day avg")
    if g.get("mute") and not g.get("regression"):
        f.append(g["mute"].upper())
    return (" [" + ", ".join(f) + "]") if f else ""


def top(d, n=4):
    items = list(d.items())
    s = ", ".join(f"{k} ({v})" for k, v in items[:n])
    return s + (f", +{len(items) - n} more" if len(items) > n else "")


def finish(lines):
    return cap("\n".join(lines), MAX_OUT)


def guard(fn):
    """Turn 'dashboard not running' into a readable answer instead of a tool crash."""
    import functools

    @functools.wraps(fn)
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except FeedDown as ex:
            return str(ex)
    return wrap


def ensure_range(hours):
    """If an older range is asked for, ask the dashboard to pull it and say how far it got."""
    if not hours or float(hours) <= 24:
        return ""
    api("/api/backfill", hours=hours)
    st = api("/api/status")
    want = time.time() * 1000 - float(hours) * 3_600_000
    behind = [k for k, v in st.items() if v.get("ok") and (v.get("covered_from") or 0) > want + 60_000]
    if behind:
        return (f"NOTE: history for the last {hours}h is still being pulled from AWS for "
                f"{len(behind)} account(s) ({', '.join(behind[:6])}); results may be incomplete. "
                f"Call dashboard_status to see progress.\n")
    return ""


# --------------------------------------------------------------------------- tools

@mcp.tool()
@guard
def dashboard_status() -> str:
    """Which AWS accounts/regions the dashboard watches, whether each is connected (expired
    logins show here), how many log groups / state machines it sees, and how far back history
    is loaded."""
    st = api("/api/status")
    now = time.time() * 1000
    out = [f"Dashboard: {link()}"]
    for k, v in sorted(st.items()):
        if not v.get("ok"):
            out.append(f"- {k}: NOT CONNECTED — {v.get('error')}")
            continue
        extra = []
        if v.get("state_machines"):
            extra.append(f"{v['state_machines']} state machines")
        if v.get("sfn_error"):
            extra.append(f"step functions problem: {v['sfn_error']}")
        if v.get("backfilling"):
            pct = 100 * (now - v["covered_from"]) / max(1, now - v["target"])
            extra.append(f"loading history {pct:.0f}%")
        if v.get("sampled"):
            extra.append("some busy log groups sampled")
        out.append(f"- {k} (account {v.get('account')}): {v.get('groups')} log groups, history "
                   f"loaded back to {t(v.get('covered_from'))}" + (f"; {'; '.join(extra)}" if extra else ""))
    return finish(out)


@mcp.tool()
@guard
def error_overview(hours: float = 24, profile: str = "") -> str:
    """Start here. Per AWS profile (client/project): how many errors and hidden errors in the last
    `hours`, and the top recurring problems. Leave profile empty for all accounts."""
    note = ensure_range(hours)
    pats = api("/api/summary", hours=hours, profile=profile, level="error,suspect", hide_muted=1)
    by = {}
    for g in pats:
        for prof, n in g["profiles"].items():
            b = by.setdefault(prof, {"error": 0, "suspect": 0, "pats": []})
            b[g["level"] if g["level"] in ("error", "suspect") else "error"] += n
            b["pats"].append((n, g))
    vol = api("/api/volume", hours=hours, profile=profile)
    lines_per = {}
    for v in vol:
        lines_per[v["profile"]] = lines_per.get(v["profile"], 0) + v["total"]
    out = [note + f"Last {hours}h — dashboard: {link(tab='patterns', hours=int(hours), profile=profile, level='error,suspect')}"]
    hot = [g for g in pats if g.get("regression") or g.get("is_new") or g.get("is_spike")]
    if hot:
        out.append("\n## Needs attention first (regressions / new / spiking)")
        for g in hot[:10]:
            out.append(f"- {g['etype']}: {g['sig'][:120]}{flags(g)} — {g['count']}× in {top(g['profiles'], 3)}")
    if pats and pats[0].get("baseline_hours", 999) < 48:
        out.append(f"(Baseline for NEW/SPIKE is still building: {pats[0]['baseline_hours']}h of history.)")
    if not by:
        out.append("No errors or hidden errors in this range.")
    for prof, b in sorted(by.items(), key=lambda x: -(x[1]["error"] * 3 + x[1]["suspect"])):
        out.append(f"\n## {prof}: {b['error']} errors, {b['suspect']} hidden errors "
                   f"({lines_per.get(prof, 0)} log lines total)")
        for n, g in sorted(b["pats"], key=lambda x: -x[0])[:5]:
            out.append(f"- [{g['level']}] {g['etype']}: {g['sig'][:140]}{flags(g)} — {n}× in "
                       f"{top(g['log_groups'], 2)}; last {t(g['last_seen'])}")
    quiet = sorted(set(lines_per) - set(by))
    if quiet:
        out.append(f"\nNo errors: {', '.join(quiet)}")
    out.append("\nMuted patterns and ones marked fixed are hidden (list_muted shows them).")
    return finish(out)


@mcp.tool()
@guard
def list_error_patterns(hours: float = 24, profile: str = "", level: str = "error,suspect",
                        log_group_prefix: str = "", search: str = "", limit: int = 25,
                        only_new_or_spiking: bool = False, include_muted: bool = False) -> str:
    """Recurring problems grouped by type and message (ids/numbers ignored), with counts, which
    log groups they come from, first/last seen and the latest full example.
    level: comma list of error,suspect,warning,info,platform. log_group_prefix e.g.
    '/aws/lambda/anew-' or 'stepfunctions:'. search: free text. Each pattern is tagged NEW (first
    seen in the last 24h), SPIKE (well above its 7-day average) or REGRESSION (marked fixed, came back).
    Muted / fixed patterns are hidden unless include_muted."""
    note = ensure_range(hours)
    rows = api("/api/summary", hours=hours, profile=profile, level=level,
               grp_prefix=log_group_prefix, q=search, hide_muted="" if include_muted else 1)
    if only_new_or_spiking:
        rows = [g for g in rows if g.get("is_new") or g.get("is_spike") or g.get("regression")]
    rows = rows[: max(1, min(limit, 100))]
    out = [note + f"{len(rows)} patterns, last {hours}h — dashboard: "
           f"{link(tab='patterns', hours=int(hours), profile=profile, level=level, q=search)}"]
    for i, g in enumerate(rows, 1):
        out += [f"\n### {i}. [{g['level']}] {g['etype']} — {g['count']}×{flags(g)}",
                f"message: {g['sig']}",
                *([f"flagged because: {g['hint']}"] if g.get("hint") else []),
                f"profiles: {top(g['profiles'])}",
                f"log groups: {top(g['log_groups'])}",
                f"first {t(g['first_seen'])}, last {t(g['last_seen'])}",
                *([f"trend: {g['last24']} in last 24h vs {g['prev7_daily_avg']}/day over the previous week"]
                  if g.get("last24") is not None else []),
                f"lines: get_log_lines(error_type={json.dumps(g['etype'])}, signature={json.dumps(g['sig'])}, hours={hours})",
                f"dashboard: {link(tab='errors' if g['level'] == 'error' else 'suspect' if g['level'] == 'suspect' else 'all', hours=int(hours), etype=g['etype'], sig=g['sig'])}",
                "latest example:", "```", cap(g["sample"], 1500), "```"]
    return finish(out)


@mcp.tool()
@guard
def get_log_lines(hours: float = 24, profile: str = "", level: str = "", log_group: str = "",
                  log_group_prefix: str = "", search: str = "", error_type: str = "",
                  signature: str = "", limit: int = 30) -> str:
    """Individual log lines (newest first) with full messages / stack traces and an event id for
    get_line_context. Filter by profile, level (comma list), exact log_group or prefix, free-text
    search, or a pattern (error_type + signature from list_error_patterns)."""
    note = ensure_range(hours)
    r = api("/api/events", hours=hours, profile=profile, level=level, grp=log_group,
            grp_prefix=log_group_prefix, q=search, etype=error_type,
            sig=signature if error_type else "", limit=max(1, min(limit, 200)))
    out = [note + f"Showing {len(r['events'])} of {r['total']} matching lines — dashboard: "
           f"{link(tab='all', hours=int(hours), profile=profile, level=level or 'error,suspect,warning,info', grp=log_group, q=search, etype=error_type, sig=signature if error_type else '')}"]
    budget = MAX_OUT // max(1, len(r["events"]))
    for e in r["events"]:
        out += [f"\n--- {t(e['ts'])} | {e['profile']} ({e['account']}, {e['region']}) | {e['group']} | "
                f"{e['level']}{' ' + e['etype'] if e['level'] in ('error', 'suspect') else ''}"
                + (f" | flagged: {e['hint']}" if e.get("hint") else ""),
                f"event_id: {e['id']}   stream: {e['stream']}",
                cap(e["message"], min(4000, max(500, budget)))]
    return finish(out)


@mcp.tool()
@guard
def get_line_context(event_id: str, minutes_before: float = 2, minutes_after: float = 1) -> str:
    """What the same log stream (same Lambda container / task) printed around one line — the
    best way to see the inputs and steps that led to an error. event_id comes from get_log_lines."""
    r = api("/api/context", id=event_id, before=minutes_before, after=minutes_after)
    if not r.get("event"):
        return "That event isn't loaded (it may be older than the 24-hour cache). Re-run get_log_lines."
    e = r["event"]
    out = [f"{e['profile']} | {e['group']} | stream {e['stream']} — {len(r['lines'])} lines from "
           f"{minutes_before} min before to {minutes_after} min after {t(e['ts'])}"]
    for x in r["lines"]:
        mark = ">>> " if x["id"] == event_id else "    "
        msg = x["message"] if x["id"] == event_id else cap(x["message"], 1500)
        out.append(f"{mark}{t(x['ts'])} [{x['level']}] {msg}")
    return finish(out)


@mcp.tool()
@guard
def log_group_volume(hours: float = 24, profile: str = "") -> str:
    """What's coming through each log group / state machine: total lines and the mix of
    errors, hidden errors, warnings, info and Lambda platform lines, plus when it last logged."""
    note = ensure_range(hours)
    rows = api("/api/volume", hours=hours, profile=profile)
    out = [note + f"{len(rows)} log groups, last {hours}h — dashboard: {link(tab='volume', hours=int(hours), profile=profile)}"]
    for v in rows[:150]:
        out.append(f"- {v['profile']} | {v['group']}: {v['total']} lines (errors {v['error']}, hidden "
                   f"{v['suspect']}, warn {v['warning']}, info {v['info']}, platform {v['platform']}); "
                   f"last {t(v['last'])}")
    return finish(out)


@mcp.tool()
@guard
def load_history(hours: float) -> str:
    """Ask the dashboard to pull an older range (e.g. 168 = 7 days, 720 = 30 days) from AWS.
    It loads in the background; other tools then cover that range. Pulled history is kept 24h."""
    api("/api/backfill", hours=hours)
    return ensure_range(hours) or f"The last {hours}h are already loaded."


@mcp.tool()
@guard
def whats_new(profile: str = "") -> str:
    """What changed recently: error patterns first seen in the last 24h, patterns spiking well
    above their usual daily rate, and patterns that were marked fixed but came back."""
    rows = api("/api/summary", hours=24, profile=profile, level="error,suspect", hide_muted=1)
    reg = [m for m in api("/api/mutes") if m.get("regression") and (not profile or m["profile"] in ("", profile))]
    out = [f"Last 24h{' for ' + profile if profile else ''} — dashboard: {link(tab='patterns', hours=24, profile=profile, level='error,suspect')}"]
    if rows and rows[0].get("baseline_hours", 999) < 48:
        out.append(f"Note: baseline still building ({rows[0]['baseline_hours']}h of history), so NEW/SPIKE are provisional.")
    if reg:
        out.append("\n## Regressions (marked fixed, came back)")
        out += [f"- {m['etype']}: {m['sig'][:140]} ({m['profile'] or 'all profiles'}) — {m['since_count']}× since "
                f"marked fixed {t(m['at'])}; last {t(m['last_seen'])}" for m in reg]
    for key, title in (("is_new", "New (first seen in the last 24h)"), ("is_spike", "Spiking")):
        sel = [g for g in rows if g.get(key)]
        if sel:
            out.append(f"\n## {title}")
            for g in sel[:20]:
                out.append(f"- [{g['level']}] {g['etype']}: {g['sig'][:140]} — {g['count']}× in {top(g['profiles'], 3)}, "
                           f"{top(g['log_groups'], 2)}" + (f"; {g['last24']} today vs {g['prev7_daily_avg']}/day before"
                                                           if key == "is_spike" else f"; first seen {t(g['first_seen'])}"))
    if len(out) == 1 or (len(out) == 2 and out[1].startswith("Note")):
        out.append("Nothing new, spiking or regressed in the last 24h.")
    return finish(out)


@mcp.tool()
@guard
def mute_pattern(error_type: str, signature: str, profile: str = "", status: str = "muted",
                 note: str = "") -> str:
    """Hide a known pattern from the feed, overviews and pop-ups. status="muted" hides it for good
    (known noise); status="fixed" hides what happened so far and flags it as a REGRESSION if it
    ever comes back. error_type + signature come from list_error_patterns. profile="" = all accounts.
    Only do this when the user asks to mute or confirms something is fixed."""
    api("/api/mute", etype=error_type, sig=signature, profile=profile,
        status="fixed" if status == "fixed" else "muted", note=note)
    return (f"{'Marked fixed' if status == 'fixed' else 'Muted'}: {error_type}: {signature[:120]} "
            f"({profile or 'all profiles'}).")


@mcp.tool()
@guard
def unmute_pattern(error_type: str, signature: str, profile: str = "") -> str:
    """Undo mute_pattern for a pattern."""
    r = api("/api/unmute", etype=error_type, sig=signature, profile=profile)
    return "Unmuted." if r.get("removed") else "No matching mute found (check profile)."


@mcp.tool()
@guard
def list_muted() -> str:
    """Patterns that are muted or marked fixed, when, and whether any fixed ones came back."""
    ms = api("/api/mutes")
    if not ms:
        return "Nothing is muted or marked fixed."
    return finish([f"- {m['status'].upper()}{' → REGRESSION' if m['regression'] else ''} | {m['etype']}: "
                   f"{m['sig'][:120]} | {m['profile'] or 'all profiles'} | since {t(m['at'])}"
                   + (f" | seen {m['since_count']}× since, last {t(m['last_seen'])}" if m['since_count'] else "")
                   + (f" | note: {m['note']}" if m.get("note") else "") for m in ms])


@mcp.tool()
@guard
def partner_api_health(hours: float = 24, profile: str = "") -> str:
    """API problems with partner services (HubSpot, NetSuite, Epicor, QuickBooks, ...) across all
    clients: auth (401 / expired token), permission (403), rate limit (429), server error (5xx),
    timeout. Flags problems hitting several clients in the last hour - usually the partner's side
    or a shared credential rather than one repo's code."""
    note = ensure_range(hours)
    rows = api("/api/partners", hours=hours, profile=profile)
    out = [note + f"Partner API problems, last {hours}h — dashboard: {link(tab='partners', hours=int(hours), profile=profile)}"]
    if not rows:
        out.append("None found.")
    for r in rows[:40]:
        out.append(f"- {r['service']} | {r['problem']}{' | SEVERAL CLIENTS IN LAST HOUR' if r['several_clients_now'] else ''}"
                   f" | {r['count']} lines ({r['last_hour']} in last hour) | clients: {top(r['profiles'], 4)} | "
                   f"functions: {top(r['functions'], 3)} | last {t(r['last'])}\n  example: "
                   + cap(r["sample"], 400).replace("\n", " "))
    return finish(out)


@mcp.tool()
@guard
def get_step_function_input(event_id: str = "", execution_arn: str = "", profile: str = "") -> str:
    """The data a failed (or retried) Step Functions run was working on: the run's input, and for
    each failed state its input and the exact payload sent to the Lambda/task. Use it to write a
    local test that reproduces the failure before fixing it. Pass event_id of a stepfunctions:...
    line from get_log_lines, or execution_arn + profile. The data can contain customer records:
    use it locally, and anonymise anything that goes into a committed test fixture."""
    r = api("/api/sfn_input", id=event_id, execution_arn=execution_arn, profile=profile)
    if r.get("error"):
        return r["error"]
    out = [f"Execution: {r['execution_arn']}", "\n## Run input", "```json", r.get("run_input") or "(none)", "```"]
    for f in r["failures"]:
        out.append(f"\n## Failed in state '{f['state']}' — {f['error'] or f['event']}"
                   + (f" (called {f['lambda']})" if f.get("lambda") else ""))
        if f.get("cause"):
            out.append("Cause: " + cap(f["cause"], 1500))
        if f.get("lambda_payload"):
            out += ["Payload sent to the Lambda/task:", "```json", f["lambda_payload"], "```"]
        if f.get("state_input") and f.get("state_input") != f.get("lambda_payload"):
            out += ["State input:", "```json", f["state_input"], "```"]
    if not r["failures"]:
        out.append("\n(No failed states in this run's history.)")
    return finish(out)


def _rate(x, per):
    return f"{100 * x:.1f}% of invocations" if per == "invocation" else f"{x:.1f}/hour"


@mcp.tool()
@guard
def list_deploys(profile: str = "", hours: float = 168, window_hours: float = 24) -> str:
    """Recent Lambda deploys (code changes) and settings changes, newest first, each with a quick
    before/after verdict (fixed / better / no change / worse / new errors / too early).
    A deploy is detected when the function's code fingerprint changes; the version comes from
    'git <sha>' in the function description or a GIT_SHA/VERSION env var if set."""
    rows = api("/api/deploys", profile=profile, hours=hours, window_hours=window_hours)
    out = [f"{len(rows)} deploys in the last {hours}h (comparing {window_hours}h before vs after) — "
           f"dashboard: {link(tab='deploys', hours=int(hours), profile=profile)}"]
    for r in rows:
        d = r["deploy"]
        out.append(
            f"- {t(d['deployed_at'])} | {d['profile']} | {d['function']} | {d['kind']}"
            + (f" {d['label']}" if d.get("label") else "")
            + f" | {r['verdict'].upper()} | errors+hidden {r['before']['error'] + r['before']['suspect']}"
            f" → {r['after']['error'] + r['after']['suspect']} ({_rate(r['rate_before'], r['rate_per'])} → "
            f"{_rate(r['rate_after'], r['rate_per'])}), {r['new_patterns']} new / {r['gone_patterns']} gone patterns"
            + ("" if r.get("before_loaded") else " [before-window not loaded; use did_my_fix_work]"))
    if not rows:
        out.append("No deploys recorded in this range. Deploys are recorded from when tracking started "
                   "(plus each function's most recent deploy).")
    return finish(out)


@mcp.tool()
@guard
def did_my_fix_work(function: str, profile: str = "", window_hours: float = 24,
                    deployed_at: str = "") -> str:
    """Compare a Lambda function's errors and hidden errors before vs after its latest deploy
    (equal windows, normalised per invocation): which error patterns are gone, which remain,
    which are new, and a verdict. `function` is the Lambda name (e.g. hubspot-changes-2-netsuite).
    Pass profile if the same function name exists in several accounts. deployed_at (epoch ms)
    picks an earlier deploy. Older history is pulled automatically if needed."""
    r = api("/api/deploy_impact", function=function, profile=profile, window_hours=window_hours,
            deployed_at=deployed_at)
    if r.get("error"):
        return r["error"]
    if r.get("matches"):
        return f"'{function}' exists in several accounts: {', '.join(r['matches'])}. Call again with profile=."
    d, b, a = r["deploy"], r["before"], r["after"]
    out = []
    if r.get("history_loading"):
        out.append("NOTE: the before-deploy window is still being pulled from AWS; re-run in a minute "
                   "for complete numbers.")
    out += [f"## {d['function']} ({d['profile']}, {d['region']}) — {r['verdict'].upper()}",
            f"Deploy: {d['kind']} change at {t(d['deployed_at'])}"
            + (f", version {d['label']}" if d.get("label") else "") + f", runtime {d.get('runtime') or '?'}",
            f"Windows: {r['window_hours']}h before vs {r['after_hours']}h after so far",
            f"Invocations: {b['invocations']} → {a['invocations']}",
            f"Errors: {b['error']} → {a['error']} | hidden errors: {b['suspect']} → {a['suspect']} | "
            f"warnings: {b['warning']} → {a['warning']}",
            f"Error rate: {_rate(r['rate_before'], r['rate_per'])} → {_rate(r['rate_after'], r['rate_per'])}",
            f"Dashboard: {link(tab='all', profile=d['profile'], grp=d['log_group'], hours=max(1, int(r['window_hours'] * 2)), level='error,suspect')}"]
    for status, title in (("new", "NEW since the deploy"), ("still", "Still happening"), ("gone", "Gone since the deploy")):
        rows = [p for p in r["patterns"] if p["status"] == status]
        if rows:
            out.append(f"\n### {title} ({len(rows)})")
            for p in rows[:15]:
                out.append(f"- [{p['level']}] {p['etype']}: {p['sig'][:160]} — {p['before']} → {p['after']}")
                if status != "gone":
                    out.append("  example: " + cap(p["sample"], 600).replace("\n", "\n  "))
    if r.get("previous"):
        out.append("\nEarlier deploys: " + "; ".join(
            f"{t(x['deployed_at'])} {x['kind']}{' ' + x['label'] if x.get('label') else ''}"
            for x in r["previous"]))
    return finish(out)


if __name__ == "__main__":
    mcp.run()
