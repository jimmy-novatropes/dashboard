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

It talks to the dashboard at http://127.0.0.1:8765, and starts it in the background if it isn't
running (AWS_ERROR_FEED_AUTOSTART=0 disables that). Read-only: it never changes anything in AWS.

Several machines (e.g. tower runs the dashboard + local model, laptop runs Claude + repos):
  AWS_ERROR_FEED_URL   = "https://tower.your-tailnet.ts.net,http://127.0.0.1:8765"
                         tried in order; a local (127.0.0.1) entry is started automatically when
                         nothing earlier in the list answers - so the laptop still works when the
                         tower is off.
  AWS_ERROR_FEED_REPOS = "C:\\path\\to\\your\\repos"   (optional) look up stack-trace code in THIS
                         machine's repo copies, so file paths / VS Code links point at the laptop.
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
URLS = [u.strip().rstrip("/") for u in os.environ.get("AWS_ERROR_FEED_URL", "http://127.0.0.1:8765").split(",")
        if u.strip()] or ["http://127.0.0.1:8765"]
BASE = URLS[0]
AUTOSTART = os.environ.get("AWS_ERROR_FEED_AUTOSTART", "1") != "0"
REPOS = os.environ.get("AWS_ERROR_FEED_REPOS") or None
_active = {"url": None, "at": 0.0}
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

Infrastructure: schedules_health (jobs that silently stopped), inventory / upgrade_checklist
(runtimes, layers, packages, timeout/memory headroom), apis_and_alarms, secrets_overview (metadata
only; useful when auth errors start), cost_estimate, daily_digest (morning summary per client).

Big picture: clients_status (red/amber/green per client). Record questions ("what happened to
deal 991?"): trace_record. Run-level: list_runs / get_run; did_nothing_check for syncs that ran
but processed nothing. Fixing code: locate_code turns an error's stack trace into repo file paths
with the code around the failing line. Team notes on patterns appear as "NOTE from the team" -
respect them (e.g. don't re-investigate known, accepted issues). client_report = weekly report.

Saving context: lines tagged LOCAL-MODEL TRIAGE come from a small model on the user's machine -
use them to prioritise, but verify before acting. For "what's wrong with X" start with investigate
(one call). To understand many lines, use summarize_logs (the local model reads them) instead of
pulling raw lines with get_log_lines.

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


def _is_local(u):
    return urllib.parse.urlparse(u).hostname in ("127.0.0.1", "localhost", "::1")


def _reachable(u, timeout=3):
    try:
        urllib.request.urlopen(f"{u}/api/status", timeout=timeout).read()
        return True
    except Exception:
        return False


def base():
    """First dashboard in AWS_ERROR_FEED_URL that answers (re-checked every 60 s); if none does,
    start the local copy (when one is listed and autostart is on)."""
    global BASE
    if _active["url"] and time.time() - _active["at"] < 60:
        return _active["url"]
    for u in URLS:
        if _reachable(u):
            _active.update(url=u, at=time.time())
            BASE = u
            return u
    for u in URLS:
        if _is_local(u) and AUTOSTART and _start_dashboard():
            for _ in range(30):
                time.sleep(0.5)
                if _reachable(u, 2):
                    _active.update(url=u, at=time.time())
                    BASE = u
                    return u
    raise FeedDown("No AWS Error Feed dashboard is reachable (tried: " + ", ".join(URLS) + "). "
                   f"Start it with `python aws_error_feed.py` in {HERE} or check the tower / Tailscale.")


def api(path, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    for attempt in range(2):
        url = f"{base()}{path}?{q}"
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
            if attempt == 0:
                _active["at"] = 0                       # that dashboard went away: pick again
                continue
            raise FeedDown(
                f"The AWS Error Feed dashboard isn't reachable at {BASE} ({ex}). Start it with "
                f"`python aws_error_feed.py` in {HERE}.") from None


_code_index = {}


def local_code(r):
    """Re-resolve stack-trace frames against THIS machine's repos (AWS_ERROR_FEED_REPOS)."""
    if not REPOS or not r.get("function") or not r.get("message"):
        return r
    try:
        sys.path.insert(0, HERE)
        import aws_ops
        ci = _code_index.get("ci") or aws_ops.CodeIndex(REPOS, os.path.join(HERE, "repos.json"))
        _code_index["ci"] = ci
        ci.build([r["function"]])
        if ci.map.get(r["function"]):
            return {**r, "mapped": ci.map[r["function"]], "frames": ci.locate(r["function"], r["message"]),
                    "local": True}
    except Exception:
        pass
    return r


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
    if g.get("below_threshold"):
        f.append(f"below your ignore threshold of {g.get('ignore_below')}/day")
    out = (" [" + ", ".join(f) + "]") if f else ""
    if g.get("note"):
        out += f"\n    NOTE from the team: {g['note']}"
    a = g.get("ai")
    if a:
        out += (f"\n    LOCAL-MODEL TRIAGE ({a.get('model')}, {round(100 * (a.get('confidence') or 0))}% sure, unverified): "
                f"{a['verdict']} / {a['category']} - {a['summary']}"
                + (f" Cause: {a['likely_cause']}" if a.get("likely_cause") else "")
                + (f" Next: {a['next_step']}" if a.get("next_step") else ""))
    return out


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
    hot = [g for g in pats if (g.get("regression") or g.get("is_new") or g.get("is_spike")) and not g.get("below_threshold")]
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
            out.append(f"- [{g['level']}] {g['etype']}: {g['sig'][:140]} — {n}× in "
                       f"{top(g['log_groups'], 2)}; last {t(g['last_seen'])}{flags(g)}")
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


def _scan_note(profile=""):
    st = api("/api/infra/status", profile=profile)
    pend = [x["profile"] for x in st if not x["scanned_at"]]
    errs = [f"{x['profile']}: {k} ({v})" for x in st for k, v in x["errors"].items()]
    out = ""
    if pend:
        out += f"NOTE: infrastructure scan still running for {', '.join(pend)}; results incomplete.\n"
    if errs:
        out += "Unavailable (usually a missing read permission): " + "; ".join(errs[:12]) + "\n"
    return out


def _when(ms):
    return t(ms) if ms else "-"


@mcp.tool()
@guard
def schedules_health(profile: str = "", only_problems: bool = False) -> str:
    """EventBridge schedules / scheduled rules vs what actually ran: for each schedule its
    expression, target Lambda or state machine, expected vs actual runs in the last 24h, missed
    run times, last run (and whether it timed out / failed) and next run. Catches jobs that stopped
    silently (disabled schedule, missing target, never triggered) - those never log an error."""
    rows = api("/api/infra/schedules", profile=profile)
    bad = [r for r in rows if r["status"] in ("not running", "missed runs", "target missing", "last run failed")]
    out = [_scan_note(profile) + f"{len(rows)} schedules, {len(bad)} with problems — dashboard: {link(tab='schedules', profile=profile)}"]
    for r in (bad if only_problems else rows):
        out.append(f"- [{r['status'].upper()}] {r['profile']} | {r['name']} ({r['source']}) | {r['expression']}"
                   f"{' ' + r['tz'] if r.get('tz') not in (None, 'UTC') else ''} -> {r['function'] or r['state_machine'] or r['target']}"
                   f" | expected {r['expected']}, ran {r['actual']} | last run {_when(r['last_run'])}"
                   f"{' (' + r['last_run_status'] + ')' if r.get('last_run_status') else ''} | next {_when(r['next_run'])}"
                   + (f" | {r['detail']}" if r['detail'] else "")
                   + (f" | missed at: {', '.join(_when(m) for m in r['missed'][-5:])}" if r['missed'] else ""))
    return finish(out)


@mcp.tool()
@guard
def inventory(profile: str = "", function: str = "", only_flagged: bool = False) -> str:
    """Lambda inventory for a client (or one function): runtime + support status, layers (and
    whether a newer layer version exists), secrets it uses, timeout and memory headroom from the
    last 24h of runs, throttles, cold starts and estimated monthly cost."""
    rows = api("/api/infra/functions", profile=profile, function=function)
    if only_flagged:
        rows = [r for r in rows if r["flags"]]
    out = [_scan_note(profile) + f"{len(rows)} functions — dashboard: {link(tab='inventory', profile=profile)}"]
    for r in rows[:120]:
        out.append(f"- {r['profile']} | {r['name']} | {r['runtime'] or '?'} ({r['runtime_text']}) | timeout {r['timeout']}s"
                   f" (max run {r['max_ms'] or '-'} ms = {r['timeout_pct'] if r['timeout_pct'] is not None else '-'}%) | memory {r['memory']} MB"
                   f" (max used {r['mem_used_mb'] or '-'} MB) | {r['invocations_24h']} runs/24h | ~${r['cost_30d']}/30d"
                   + (f" | layers: {', '.join(l.split(':', 6)[-1] for l in r['layers'])}" if r['layers'] else "")
                   + (f" | secrets: {', '.join(r['secrets'])}" if r['secrets'] else "")
                   + (f" | NEEDS ATTENTION: {'; '.join(r['flags'])}" if r['flags'] else "")
                   + (f" ({', '.join(r['outdated_layers'])})" if r['outdated_layers'] else ""))
    return finish(out)


@mcp.tool()
@guard
def upgrade_checklist(profile: str = "") -> str:
    """What to upgrade, per client: functions on deprecated or soon-deprecated Lambda runtimes
    (with AWS's dates), functions on an older layer version than the latest, and the package
    versions inside each layer version in use (to compare clients / spot old libraries)."""
    fns = api("/api/infra/functions", profile=profile)
    lays = api("/api/infra/layers", profile=profile)
    out = [_scan_note(profile) + f"Upgrade checklist — dashboard: {link(tab='inventory', profile=profile)}"]
    rt = [f for f in fns if f["runtime_status"] in ("deprecated", "soon")]
    out.append(f"\n## Runtimes ({len(rt)} functions)")
    out += [f"- {f['profile']} | {f['name']}: {f['runtime']} - {f['runtime_text']}" for f in rt] or ["- all on supported runtimes"]
    old = [l for l in lays if l["outdated"]]
    out.append(f"\n## Outdated layer versions ({len(old)})")
    out += [f"- {l['profile']} | {l['layer']}:{l['version']} (latest {l['latest']}) used by {', '.join(l['functions'])}" for l in old] \
        or ["- none (or layer versions couldn't be read)"]
    out.append("\n## Packages per layer version in use")
    for l in lays:
        pk = ", ".join(f"{k} {v}" for k, v in sorted(l["packages"].items())) or (l.get("note") or "-")
        out.append(f"- {l['profile']} | {l['layer']}:{l['version']}: {cap(pk, 1200)}")
    return finish(out)


@mcp.tool()
@guard
def apis_and_alarms(profile: str = "") -> str:
    """API Gateway health for the last 24h (requests, 4xx, 5xx, p99 latency per stage, and which
    Lambda handles each route - 5xx on a webhook endpoint usually means lost events) plus
    CloudWatch alarms that are currently firing."""
    r = api("/api/infra/apis", profile=profile)
    out = [_scan_note(profile) + f"Dashboard: {link(tab='apis', profile=profile)}", "\n## API Gateway (24h)"]
    for a in r["apis"]:
        out.append(f"- {a['profile']} | {a['api']} ({a['kind']}) stage {a['stage']}: {a['requests']} requests, {a['e4']} 4xx, "
                   f"{a['e5']} 5xx, p99 {a['p99_ms'] or '-'} ms" + (f" | {'; '.join(a['flags'])}" if a['flags'] else "")
                   + " | routes: " + ", ".join(f"{x['route']}{' -> ' + x['function'] if x['function'] else ''}" for x in a["routes"][:8]))
    if not r["apis"]:
        out.append("- none found")
    firing = [a for a in r["alarms"] if a["state"] == "ALARM"]
    out.append(f"\n## Alarms firing ({len(firing)} of {len(r['alarms'])})")
    out += [f"- {a['profile']} | {a['name']} ({a['metric']}) since {_when(a['since'])}: {a['reason'][:200]}" for a in firing] or ["- none"]
    return finish(out)


@mcp.tool()
@guard
def secrets_overview(profile: str = "") -> str:
    """Secrets Manager metadata (never the values): last changed / rotated / read, which Lambdas
    use each secret, and flags - changed in the last 48h, not read in 30+ days, rotation overdue,
    and 'possibly related' when auth errors (401/403) started soon after a secret changed."""
    rows = api("/api/infra/secrets", profile=profile)
    out = [_scan_note(profile) + f"{len(rows)} secrets — dashboard: {link(tab='inventory', profile=profile)}"]
    for r in rows:
        out.append(f"- {r['profile']} | {r['name']} | changed {_when(r['last_changed'])} | rotated {_when(r['last_rotated'])} | "
                   f"read {_when(r['last_accessed'])} | rotation {'on' if r['rotation'] else 'off'} | used by "
                   f"{', '.join(r['functions']) or 'not found in env vars'}"
                   + (f" | {'; '.join(r['flags'])}" if r['flags'] else "") + (f" | {'; '.join(r['related'])}" if r['related'] else ""))
    return finish(out)


@mcp.tool()
@guard
def cost_estimate(profile: str = "") -> str:
    """Estimated Lambda compute cost per client for 30 days, from the last 24h of runs (billed
    duration x memory + requests at list price, no free tier), and the most expensive functions."""
    rows = api("/api/infra/cost", profile=profile)
    out = [f"Estimated Lambda cost (30 days, from last 24h of runs) — dashboard: {link(tab='inventory', profile=profile)}"]
    for r in rows:
        out.append(f"- {r['profile']}: ~${r['cost_30d']:.2f} | top: " + ", ".join(
            f"{x['name']} ${x['cost_30d']} ({x['invocations_24h']}/day, {x['memory']} MB, avg {x['avg_ms']} ms)"
            for x in r["top"][:4] if x["cost_30d"] > 0))
    return finish(out)


@mcp.tool()
@guard
def daily_digest(profile: str = "") -> str:
    """Per-client summary of what needs attention today: broken schedules, regressions, new and
    spiking errors, deploy results, partner API problems, firing alarms, API 5xx, functions near
    their limits and the upgrade backlog. Good as a morning check or a summary to post."""
    r = api("/api/digest", profile=profile)
    return finish([_scan_note(profile) + r["text"], f"\nDashboard: {link(tab='digest', profile=profile)}"])


@mcp.tool()
@guard
def rescan_infrastructure(profile: str = "") -> str:
    """Re-read schedules, functions, layers, secrets metadata, APIs and alarms now (normally every
    15 minutes) - e.g. right after deploying or changing a schedule. Takes about a minute."""
    api("/api/infra/rescan", profile=profile)
    return "Re-scan started; results update in about a minute."


def _find_group(function, profile):
    rows = api("/api/ops/groups", profile=profile, hours=72)
    m = [r for r in rows if r["function"] == function or r["group"] == function or (r["function"] or "").endswith(function)]
    return m


@mcp.tool()
@guard
def clients_status() -> str:
    """Start here for "how are my clients doing?" or "did my push reduce errors?": every client with
    errors / hidden errors for the last 1h, 6h and 24h vs the period before each, every deploy in the
    last 24h with its before/after error rate, red / amber / green status and
    the reasons (jobs not running, regressions, alarms, API 5xx, syncs that did nothing, new or
    spiking errors, partner API problems, deploy results, limits, deprecated runtimes), plus errors,
    runs, records and cost for the last 24h."""
    cards = api("/api/ops/clients")
    out = [f"Dashboard: {link(tab='clients')}"]
    for c in cards:
        trend = lambda a, b: "" if b is None else (" (up from %d)" % b if a > b * 1.2 and a - b > 2 else
                                                   " (down from %d)" % b if a < b * 0.8 and b - a > 2 else "")
        out.append(f"\n## {c['profile']}: {c['status'].upper()}")
        out.append(f"errors 24h {c['errors_24h']}{trend(c['errors_24h'], c['errors_prev'])}, hidden {c['hidden_24h']}, "
                   f"runs {c['runs_24h']} ({c['failed_runs_24h']} failed), records {c['records_24h']}"
                   + (f" (usually ~{c['records_usual']}/day)" if c.get("records_usual") else "")
                   + f", schedules {c['sched_ok']}/{c['sched_total']} ok, ~${c['cost_30d']}/30d"
                   + (f", last deploy {c['last_deploy']['function']} {t(c['last_deploy']['at'])}: {c['last_deploy']['verdict']}"
                      if c.get("last_deploy") else ""))
        w = c.get("windows") or {}
        if w:
            def pc(cur, prev):
                if prev is None:
                    return ""
                return f" (prev {prev})"
            out.append("errors / hidden: " + " | ".join(
                f"last {k}: {w[k]['errors']}{pc(w[k]['errors'], w[k]['prev_errors'])} / {w[k]['hidden']}"
                f"{pc(w[k]['hidden'], w[k]['prev_hidden'])}" for k in ("1h", "6h", "24h")))
        for d in c.get("recent_deploys") or []:
            rate = (lambda x: f"{100 * x:.1f}%/run" if d["rate_per"] == "invocation" else f"{x:.1f}/h")
            out.append(f"  deploy {d['function']} {t(d['at'])}: {d['verdict']}"
                       + (f" (errors+hidden {rate(d['rate_before'])} -> {rate(d['rate_after'])}, "
                          f"{d['window_hours']}h before vs {d['after_hours']}h after)" if d["before_loaded"] else ""))
        out += [f"- {r}" for r in c["reasons"][:10]]
    return finish(out)


@mcp.tool()
@guard
def trace_record(record_id: str = "", profile: str = "", days: float = 7, include_step_functions: bool = True,
                 job_id: str = "") -> str:
    """Follow one record (HubSpot deal id, NetSuite id, email, order number...) through the
    integrations: every log line, Lambda run and Step Functions execution (input/output) mentioning
    it, as a timeline. Searches CloudWatch directly for just that ID up to `days` back. Narrow with
    profile when you know the client - much faster. Takes 10s to a few minutes; if it's still
    running, call again with the returned job_id."""
    if not job_id:
        if not record_id.strip():
            return "Give a record_id (or a job_id from an earlier call)."
        job_id = api("/api/ops/trace/start", term=record_id.strip(), profile=profile, days=days,
                     sfn=1 if include_step_functions else 0)["id"]
    j = {}
    for _ in range(30):
        j = api("/api/ops/trace", id=job_id)
        if j.get("status") != "running":
            break
        time.sleep(1)
    if j.get("error"):
        return j["error"]
    out = [f"Trace of \"{j['term']}\" over {j['days']:g} days — {j['status'].upper()}: {len(j['hits'])} lines in "
           f"{len(j['timeline'])} runs, {len(j['sfn'])} Step Functions executions. job_id={job_id}",
           f"Dashboard: {link(tab='trace', trace=j['term'], profile=profile)}"]
    if j["status"] == "running":
        out.append(f"Still searching ({j['accounts_done']}/{j['accounts']} accounts, {j['groups_searched']} log groups). "
                   f"Call trace_record(job_id=\"{job_id}\") again for the rest.")
    for e in j["sfn"]:
        out.append(f"- STEP FUNCTIONS {t(e['start'])} | {e['profile']} | {e['state_machine']}/{e['execution']} | {e['status']}"
                   f" | id in {'input' if e['in_input'] else 'output'} | {e['arn']}")
    for g in j["timeline"]:
        out.append(f"\n### {t(g['start'])} | {g['profile']} | {g['group']} | {g['outcome']} | stream {g['stream']}")
        for l in g["lines"][:15]:
            out.append(f"  {t(l['ts'])} [{l['level']}] {cap(l['message'], 600)}  (event_id: {l['id']})")
        if len(g["lines"]) > 15:
            out.append(f"  ... {len(g['lines']) - 15} more lines")
    if j.get("partial") or j.get("errors"):
        out.append("\nNotes: " + "; ".join((j.get("partial") or []) + (j.get("errors") or []))[:1500])
    return finish(out)


@mcp.tool()
@guard
def list_runs(function: str, profile: str = "", hours: float = 24, only_problems: bool = False) -> str:
    """A Lambda's individual runs (newest first): end time, duration, records processed, errors,
    hidden errors, memory, outcome (ok / failed / timeout / hidden errors / no records) and the first
    problem line. Use get_run to read one run's full log."""
    m = _find_group(function, profile)
    if not m:
        return f"No Lambda runs loaded for '{function}'{' in ' + profile if profile else ''} (check the name / profile)."
    if len(m) > 1 and not profile:
        return "That function exists in several accounts: " + ", ".join(sorted({x['profile'] for x in m})) + ". Pass profile."
    g = m[0]
    runs = api("/api/ops/runs", profile=g["profile"], region=g["region"], grp=g["group"], hours=hours)
    if only_problems:
        runs = [r for r in runs if r["outcome"] != "ok"]
    out = [f"{g['profile']} | {g['group']}: {len(runs)} runs in {hours:g}h — dashboard: {link(tab='runs', profile=g['profile'])}"]
    for r in runs[:80]:
        out.append(f"- {t(r['end'])} | {r['duration_ms'] / 1000:.1f}s | records {r['records'] if r['records'] is not None else '-'} | "
                   f"errors {r['errors']} hidden {r['hidden']} | {r['mem_used']}/{r['memory']} MB | {r['outcome'].upper()}"
                   f"{' (cold start)' if r['cold_start'] else ''} | request_id {r['request_id']}"
                   + (f"\n    first problem: {r['first_problem']['line'][:250]}" if r.get("first_problem") else ""))
    return finish(out)


@mcp.tool()
@guard
def get_run(function: str, request_id: str, profile: str = "") -> str:
    """Everything one Lambda run logged, in order (request_id from list_runs) - the clearest way to
    read what happened inside a single failing or empty run."""
    m = _find_group(function, profile)
    if not m:
        return f"No Lambda runs loaded for '{function}'."
    g = m[0]
    r = api("/api/ops/run", profile=g["profile"], region=g["region"], grp=g["group"], request_id=request_id)
    if not r:
        return "Run not found in the loaded logs (it may be older than the 24-hour cache)."
    out = [f"{g['profile']} | {g['group']} | request {request_id} | ended {t(r['end'])} | {r['duration_ms'] / 1000:.1f}s | "
           f"{r['outcome'].upper()} | records {r['records']} | {r['mem_used']}/{r['memory']} MB | stream {r['stream']}"]
    for l in r.get("lines_list", []):
        out.append(f"{t(l['ts'])} [{l['level']}] {cap(l['message'], 2500)}")
    return finish(out)


@mcp.tool()
@guard
def did_nothing_check(profile: str = "") -> str:
    """Syncs that ran without erroring but processed 0 records or far fewer than usual (from lines
    like "Processed 25 deals"), and log groups that suddenly went quiet or silent compared with the
    previous week (usually the source system stopped sending data)."""
    rows = api("/api/ops/nothing", profile=profile)
    out = [f"{len(rows)} findings — dashboard: {link(tab='runs', profile=profile)}"]
    out += [f"- {d['profile']} | {d['group']} | {d['kind'].upper()}: {d['detail']}" for d in rows] or ["- nothing suspicious"]
    return finish(out)


@mcp.tool()
@guard
def locate_code(event_id: str) -> str:
    """For an error line (event_id from get_log_lines), the files and lines in YOUR repos from its
    stack trace: local path, VS Code link, GitHub link and the surrounding code. Functions are
    matched to repo folders automatically (repos.json next to the dashboard can override)."""
    r = local_code(api("/api/ops/code", id=event_id))
    if r.get("error"):
        return r["error"]
    if not r.get("mapped"):
        return (f"No repo folder found for function '{r.get('function')}'. Add it to repos.json next to the dashboard: "
                f'{{"{r.get("function")}": "C:\\path\\to\\its\\folder"}}')
    out = [f"Function {r['function']} -> repo {r['mapped']['repo']} ({r['mapped']['how']})"]
    own = [f for f in r["frames"] if f.get("path")]
    for f in own:
        out += [f"\n{f['path']}:{f['line']} in {f['func'] or '?'}" + (f"  ({f['github']})" if f.get("github") else ""),
                "```", f.get("snippet") or "", "```"]
    if not own:
        out.append("No stack-trace frames from your own code in this line (library-only or no traceback).")
    return finish(out)


@mcp.tool()
@guard
def add_note(error_type: str, signature: str, note: str, profile: str = "", ignore_below_per_day: int = 0) -> str:
    """Attach a team note to an error pattern ("known HubSpot rate limit, retry handles it") - shown
    in the dashboard and in every tool output for that pattern. ignore_below_per_day > 0 keeps it out
    of the 'needs attention' lists while it stays under that daily count. Empty note removes it.
    Only add notes when the user asks."""
    api("/api/ops/note", etype=error_type, sig=signature, profile=profile, note=note,
        ignore_below=ignore_below_per_day or "")
    return "Note saved." if note or ignore_below_per_day else "Note removed."


@mcp.tool()
@guard
def client_report(profile: str, days: float = 7) -> str:
    """Client-facing health report for one client over `days`: Lambda and workflow success rates
    (from CloudWatch metrics), records processed, errors vs the previous period, issues resolved,
    open items, scheduled-job problems, changes deployed, third-party service issues and recommended
    maintenance. Returned as markdown - offer to turn it into a doc. Printable version via the link."""
    r = api("/api/ops/report", profile=profile, days=days)
    if r.get("error"):
        return r["error"]
    return finish([r["markdown"], f"\nPrintable / PDF: {base()}/report?profile={urllib.parse.quote(profile)}&days={days:g}"])


def _wait_job(jid, seconds=45):
    j = {}
    for _ in range(int(seconds / 1.5)):
        j = api("/api/ai/job", id=jid)
        if j.get("status") != "running":
            return j
        time.sleep(1.5)
    return j


@mcp.tool()
@guard
def summarize_logs(question: str = "", profile: str = "", level: str = "", log_group: str = "",
                   log_group_prefix: str = "", search: str = "", error_type: str = "", signature: str = "",
                   hours: float = 24, limit: int = 300, job_id: str = "") -> str:
    """Have the LOCAL model (on the user's machine, free) read up to `limit` matching log lines and
    return a short summary / answer to `question`, instead of pulling all the raw lines into this
    conversation. Prefer this over get_log_lines when you need the gist of many lines; use
    get_log_lines / get_line_context only for the few exact lines you need. Local-model output can
    be wrong - verify anything important against the lines. If it's still working, call again with job_id."""
    if not job_id:
        r = api("/api/ai/summarize", kind="lines", question=question, profile=profile, level=level, grp=log_group,
                grp_prefix=log_group_prefix, q=search, etype=error_type, sig=signature if error_type else "",
                hours=hours, limit=max(10, min(limit, 800)))
        if r.get("error"):
            return r["error"] + " (Use get_log_lines instead.)"
        job_id, n, ids = r["id"], r["lines"], r.get("event_ids") or []
    else:
        n, ids = None, []
    j = _wait_job(job_id)
    if j.get("status") == "running":
        return f"The local model is still reading. Call summarize_logs(job_id=\"{job_id}\") again in ~30s."
    if j.get("error"):
        return f"Local model failed: {j['error']}. Use get_log_lines instead."
    return finish([f"Local-model summary{f' of {n} lines' if n else ''} (verify key facts):", j.get("answer") or "",
                   *([f"\nKey error lines (event_id for get_line_context / locate_code): {', '.join(ids)}"] if ids else [])])


@mcp.tool()
@guard
def investigate(profile: str, function: str = "", error_type: str = "", signature: str = "", hours: float = 24) -> str:
    """One call instead of four: for a client (optionally one function or one error pattern) returns
    the top problem patterns with team notes and local-model triage, the full latest example, what the
    function logged just before it, where it is in the code (if the repo is mapped), and the function's
    recent run outcomes. Start here when asked "what's wrong with <client/function>?"."""
    prefix = f"/aws/lambda/{function}" if function and not function.startswith("/") else function
    rows = api("/api/summary", hours=hours, profile=profile, level="error,suspect", hide_muted=1,
               grp_prefix=prefix or "", etype=error_type, sig=signature if error_type else "")
    out = [f"Investigation: {profile}{' / ' + function if function else ''}, last {hours:g}h — "
           f"dashboard: {link(tab='patterns', profile=profile, hours=int(hours))}"]
    if not rows:
        out.append("No errors or hidden errors (muted / fixed patterns excluded).")
    for i, g in enumerate(rows[:3], 1):
        out.append(f"\n## {i}. [{g['level']}] {g['etype']}: {g['sig'][:160]} — {g['count']}x{flags(g)}")
        out.append(f"log groups: {top(g['log_groups'], 3)} | first {t(g['first_seen'])}, last {t(g['last_seen'])}")
        if i > 2:
            continue
        ev = api("/api/events", profile=profile, etype=g["etype"], sig=g["sig"], hours=hours, limit=1)["events"]
        if not ev:
            continue
        e = ev[0]
        out += [f"latest ({t(e['ts'])}, event_id {e['id']}):", "```", cap(e["message"], 2000), "```"]
        ctx = api("/api/context", id=e["id"], before=2, after=0.5)
        before = [x for x in ctx.get("lines", []) if x["id"] != e["id"]][-12:]
        if before:
            out.append("logged just before:")
            out += [f"  {t(x['ts'])} [{x['level']}] {cap(x['message'], 300)}" for x in before]
        code = local_code(api("/api/ops/code", id=e["id"]))
        own = [f for f in code.get("frames", []) if f.get("path")]
        if own:
            f = own[-1]
            out += [f"code: {f['path']}:{f['line']}", "```", f.get("snippet") or "", "```"]
    groups = api("/api/ops/groups", profile=profile, hours=hours)
    if function:
        groups = [g for g in groups if g["function"] == function]
    elif rows:
        top_groups = list(rows[0]["log_groups"])[:1]
        groups = [g for g in groups if g["group"] in top_groups]
    for g in groups[:1]:
        runs = api("/api/ops/runs", profile=g["profile"], region=g["region"], grp=g["group"], hours=hours)
        if runs:
            from collections import Counter
            oc = Counter(r["outcome"] for r in runs)
            out.append(f"\nruns of {g['function']} ({hours:g}h): {len(runs)} total - " + ", ".join(f"{k} {v}" for k, v in oc.most_common())
                       + f"; latest {t(runs[0]['end'])} {runs[0]['outcome']}, records {runs[0]['records']}")
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
