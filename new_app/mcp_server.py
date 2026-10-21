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

It talks to the dashboard at http://127.0.0.1:8766, and starts it in the background if it isn't
running (AWS_ERROR_FEED_AUTOSTART=0 disables that). Read-only: it never changes anything in AWS.

Several machines (e.g. tower runs the dashboard + local model, laptop runs Claude + repos):
  AWS_ERROR_FEED_URL   = "https://tower.your-tailnet.ts.net,http://127.0.0.1:8766"
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
URLS = [u.strip().rstrip("/") for u in os.environ.get("AWS_ERROR_FEED_URL", "http://127.0.0.1:8766").split(",")
        if u.strip()] or ["http://127.0.0.1:8766"]
BASE = URLS[0]
AUTOSTART = os.environ.get("AWS_ERROR_FEED_AUTOSTART", "1") != "0"
REPOS = os.environ.get("AWS_ERROR_FEED_REPOS") or None
_active = {"url": None, "at": 0.0}
MAX_OUT = 40_000          # keep tool results a sensible size for the conversation

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

Only the everyday tools are listed; more_tools() lists and runs all the others (schedules, inventory,
deploys, partner APIs, notes, mutes, reports, runs, history...). For code: outline(file) gives
signatures only and get_symbol(name) returns one function's source - prefer them to reading files.
Coding with fewer tokens: find_code(query, repo) returns only matching lines (file:line) - use it
instead of opening files to search; repo_map(repo) gives the repo layout with one-line purposes;
fix_brief(profile, ...) bundles everything needed to fix one error plus a local diagnosis;
local_run(command, repo) runs tests/checks and returns only failures; local_draft_change(path,
start, end, instruction) lets the local model write a small edit and returns a diff (apply=True writes
it); draft_commit_message(repo) drafts a commit message. Review local-model output before relying on it.

Saving context for code and documents: before reading a big file, a whole folder, a long diff or
git history yourself, use local_read(path, question) or local_git(repo, what, question): the local
model reads it and returns a short summary with file:line references. Then open only the exact lines
you need to change. Its summaries can be wrong - verify before editing.

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


_NOTES = []          # notes for the current tool answer (e.g. "this on-demand project was asleep")
_AWAKE = {}          # profile -> time we last made sure it's awake


def _wake(profile):
    """On-demand projects aren't loaded until opened: wake it and say so if it was asleep."""
    if not profile or time.time() - _AWAKE.get(profile, 0) < 60:
        return
    _AWAKE[profile] = time.time()
    try:
        with urllib.request.urlopen(f"{base()}/api/prefs/wake?" + urllib.parse.urlencode({"profile": profile}),
                                    timeout=10) as r:
            w = json.loads(r.read().decode("utf-8"))
        if w.get("was_asleep"):
            _NOTES.append(f"NOTE: {profile} is an on-demand project and was asleep; it's loading the last 24h "
                          f"now (a few minutes), so this answer may be incomplete - ask again shortly.")
    except Exception:
        pass


def api(path, **params):
    if params.get("profile") and not path.startswith("/api/prefs"):
        _wake(params["profile"])
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


def api_post(path, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(f"{base()}{path}", data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError) as ex:
            if attempt == 0 and not isinstance(getattr(ex, "reason", None), TimeoutError):
                _active["at"] = 0
                continue
            raise FeedDown(f"The dashboard at {base()} didn't accept the request ({ex}).") from None


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


def chronic(g):
    """In (nearly) every run of its function and not new / spiking / regressed: normal output or a
    known, ongoing issue - listed compactly so it doesn't crowd out real changes."""
    return bool(g.get("likely_format")) and not (g.get("is_new") or g.get("is_spike") or g.get("regression"))


def flags(g, brief=False):
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
    if g.get("likely_format"):
        f.append(f"IN {g['likely_format']}% OF RUNS: normal output or a chronic issue")
    out = (" [" + ", ".join(f) + "]") if f else ""
    if g.get("note"):
        out += f"\n    NOTE from the team: {g['note']}"
    a = g.get("ai")
    if a and brief:
        out += (f"\n    tower triage ({round(100 * (a.get('confidence') or 0))}%, unverified): {a['verdict']} / "
                f"{a['category']} - {cap(a.get('summary') or '', 180)}")
    elif a:
        out += (f"\n    LOCAL-MODEL TRIAGE ({a.get('model')}, {round(100 * (a.get('confidence') or 0))}% sure, unverified): "
                f"{a['verdict']} / {a['category']} - {a['summary']}"
                + (f" Cause: {a['likely_cause']}" if a.get("likely_cause") else "")
                + (f" Next: {a['next_step']}" if a.get("next_step") else "")
                + (f" Evidence: \"{a['evidence']}\"" if a.get("evidence") else "")
                + (f" ({a['note']})" if a.get("note") else ""))
    return out


_NUM = __import__("re").compile(r"\d+")


def _problem_text(msg, width=1200):
    """A problem line for Claude: whole if short; a long traceback keeps its start and its end
    (where the exception is)."""
    if len(msg) <= width:
        return msg
    head = min(300, width // 3)
    return msg[:head] + f"\n   … [{len(msg) - width} chars] …\n" + msg[-(width - head):]


def _brief_lines(lines, max_lines=80):
    """Collapse repeated lines (same text apart from numbers); a problem line that already appeared
    earlier in the run is counted on its first occurrence instead of repeated; keep problem lines
    with the lines just before them."""
    rows, last_key, seen = [], None, {}
    for l in lines:
        key = (l["level"], _NUM.sub("#", l["message"][:200]))
        if key == last_key:
            rows[-1]["n"] += 1
            continue
        if l["level"] in ("error", "suspect") and key in seen:
            seen[key]["n"] += 1
            seen[key]["last"] = l["ts"]
            continue                      # (last_key unchanged: lines around it still fold together)
        last_key = key
        rows.append({"l": l, "n": 1})
        if l["level"] in ("error", "suspect"):
            seen[key] = rows[-1]
    def fmt(r):
        l = r["l"]
        msg = _problem_text(l["message"]) if l["level"] in ("error", "suspect") else cap(l["message"], 300)
        more = ""
        if r["n"] > 1:
            more = f"  (×{r['n']} similar" + (f", last at {t(r['last'])[11:]}" if r.get("last") else "") + ")"
        return f"{t(l['ts'])[11:]} [{l['level']}] {msg}{more}"
    if len(rows) <= max_lines:
        return [fmt(r) for r in rows]
    keep = set(range(10)) | set(range(len(rows) - 10, len(rows)))
    for i, r in enumerate(rows):
        if r["l"]["level"] in ("error", "suspect"):
            keep |= set(range(max(0, i - 3), i + 1))
    out, prev = [], -1
    for i in sorted(keep):
        if i - prev > 1:
            out.append(f"   … {i - prev - 1} line(s) omitted (detail=\"full\" shows all)")
        out.append(fmt(rows[i]))
        prev = i
    return out


def top(d, n=4):
    items = list(d.items())
    s = ", ".join(f"{k} ({v})" for k, v in items[:n])
    return s + (f", +{len(items) - n} more" if len(items) > n else "")


def finish(lines):
    notes = list(dict.fromkeys(_NOTES))
    _NOTES.clear()
    return cap("\n".join(notes + list(lines)), MAX_OUT)


def _prefs():
    try:
        return {x["profile"]: x for x in api("/api/prefs")["profiles"]}
    except Exception:
        return {}


def _star(p, prefs):
    return ("★ " if (prefs.get(p) or {}).get("priority") == "high" else "") + p


def _report_usage(tool, chars, ms=0, err=None):
    """Tell the dashboard how much this tool sent to Claude, how long it took and whether it failed
    (savings counter + Health tab). Never blocks."""
    def go():
        try:
            q = {"tool": tool, "chars": chars, "ms": int(ms)}
            if err:
                q["err"] = str(err)[:300]
            u = f"{base()}/api/ai/claude_usage?" + urllib.parse.urlencode(q)
            urllib.request.urlopen(u, timeout=5).read()
        except Exception:
            pass
    import threading
    threading.Thread(target=go, daemon=True).start()


def guard(fn):
    """Turn 'dashboard not running' into a readable answer instead of a tool crash,
    and count what each tool hands to Claude."""
    import functools

    @functools.wraps(fn)
    def wrap(*a, **kw):
        name = fn.__name__
        if name == "more_tools":
            name = f"more_tools:{kw.get('name') or (a[0] if a else '') or 'list'}"
        t0 = time.time()
        try:
            out = fn(*a, **kw)
        except FeedDown as ex:
            return str(ex)
        except Exception as ex:
            _report_usage(name, 0, 1000 * (time.time() - t0), f"{type(ex).__name__}: {ex}")
            raise
        if isinstance(out, str):
            _report_usage(name, len(out), 1000 * (time.time() - t0))
        return out
    return wrap


# Tool profile: "core" (default) exposes the everyday tools + more_tools (which reaches all the others),
# so every message carries far fewer tool definitions. AWS_ERROR_FEED_TOOLS=all exposes everything.
TOOLS_MODE = os.environ.get("AWS_ERROR_FEED_TOOLS", "core").strip().lower()
CORE = {"clients_status", "dashboard_status", "investigate", "fix_brief", "error_overview", "list_error_patterns",
        "summarize_logs", "get_log_lines", "get_line_context", "did_my_fix_work", "find_code", "get_symbol",
        "outline", "repo_map", "local_read", "local_git", "local_draft_change", "local_run", "usage_savings"}
CORE |= {x.strip() for x in os.environ.get("AWS_ERROR_FEED_EXTRA_TOOLS", "").split(",") if x.strip()}
REGISTRY, EXPOSED = {}, set()


def tool():
    def deco(f):
        REGISTRY[f.__name__] = f
        if TOOLS_MODE == "all" or f.__name__ in CORE:
            EXPOSED.add(f.__name__)
            return mcp.tool()(f)
        return f
    return deco


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

@tool()
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


@tool()
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
            out.append(f"- {g['etype']}: {g['sig'][:120]}{flags(g, brief=True)} — {g['count']}× in {top(g['profiles'], 3)}")
    if pats and pats[0].get("baseline_hours", 999) < 48:
        out.append(f"(Baseline for NEW/SPIKE is still building: {pats[0]['baseline_hours']}h of history.)")
    if not by:
        out.append("No errors or hidden errors in this range.")
    prefs = _prefs()
    rk = lambda p: {"high": 0, "normal": 1, "on_demand": 2}.get((prefs.get(p) or {}).get("priority"), 1)
    for prof, b in sorted(by.items(), key=lambda x: (rk(x[0]), -(x[1]["error"] * 3 + x[1]["suspect"]))):
        out.append(f"\n## {_star(prof, prefs)}: {b['error']} errors, {b['suspect']} hidden errors "
                   f"({lines_per.get(prof, 0)} log lines total)")
        pats_sorted = sorted(b["pats"], key=lambda x: -x[0])
        live = [x for x in pats_sorted if not chronic(x[1])]
        chron = [x for x in pats_sorted if chronic(x[1])]
        for n, g in live[:3]:
            out.append(f"- [{g['level']}] {g['etype']}: {g['sig'][:110]} — {n}× in "
                       f"{top(g['log_groups'], 2)}; last {t(g['last_seen'])}{flags(g, brief=True)}")
        if len(live) > 3:
            out.append(f"- +{len(live) - 3} more pattern(s) ({sum(n for n, _ in live[3:])} lines)")
        if chron:
            out.append(f"- chronic (in nearly every run, unchanged): " + "; ".join(
                f"{g['etype']}: {g['sig'][:70]} ({n}×)" for n, g in chron[:3])
                + (f"; +{len(chron) - 3} more" if len(chron) > 3 else ""))
    quiet = sorted(set(lines_per) - set(by))
    if quiet:
        out.append(f"\nNo errors: {', '.join(quiet)}")
    asleep = sorted(p for p, x in prefs.items() if x.get("asleep"))
    if asleep and not profile:
        out.append(f"On demand, not loaded: {', '.join(asleep)} (pass profile=<name> to load one).")
    out.append("\nMuted patterns and ones marked fixed are hidden (list_muted shows them).")
    return finish(out)


@tool()
@guard
def list_error_patterns(hours: float = 24, profile: str = "", level: str = "error,suspect",
                        log_group_prefix: str = "", search: str = "", limit: int = 12,
                        only_new_or_spiking: bool = False, include_muted: bool = False,
                        detail: str = "brief") -> str:
    """Recurring problems grouped by type+message with counts, groups, first/last seen and an example;
    tagged NEW / SPIKE / REGRESSION. level: comma list (error,suspect,warning,info). detail="full"
    for longer examples."""
    note = ensure_range(hours)
    rows = api("/api/summary", hours=hours, profile=profile, level=level,
               grp_prefix=log_group_prefix, q=search, hide_muted="" if include_muted else 1)
    if only_new_or_spiking:
        rows = [g for g in rows if g.get("is_new") or g.get("is_spike") or g.get("regression")]
    rows = rows[: max(1, min(limit, 100))]
    out = [note + f"{len(rows)} patterns, last {hours}h — dashboard: "
           f"{link(tab='patterns', hours=int(hours), profile=profile, level=level, q=search)}"]
    if detail != "full":
        out.append("Lines for a pattern: get_log_lines(error_type=<type>, signature=<message>); "
                   "deeper: investigate / fix_brief.")
        chron = [g for g in rows if chronic(g)]
        for i, g in enumerate([g for g in rows if not chronic(g)], 1):
            same_sample = g["sample"].strip()[:300] == g["sig"].strip()[:300]
            grp = top(g["log_groups"], 2)
            out += [f"\n### {i}. [{g['level']}] {g['etype']} — {g['count']}×{flags(g, brief=True)}",
                    f"message: {g['sig']}",
                    *([f"flagged because: {g['hint']}"] if g.get("hint") else []),
                    f"where: {top(g['profiles'], 3)} | {grp} | {t(g['first_seen'])[5:16]} → {t(g['last_seen'])[5:16]}"
                    + (f" | {g['last24']}/24h vs {g['prev7_daily_avg']}/day before" if g.get("last24") is not None else ""),
                    *([] if same_sample else ["example:", "```", _problem_text(g["sample"], 500), "```"])]
        if chron:
            out.append(f"\n### Chronic — in nearly every run of their function, not new or spiking "
                       f"(normal output or a known, ongoing issue; investigate / fix_brief for detail):")
            for g in chron:
                a = g.get("ai") or {}
                out.append(f"- [{g['level']}] {g['etype']}: {g['sig'][:160]} — {g['count']}× in {top(g['log_groups'], 1)}"
                           + (f" | tower: {a.get('verdict')}/{a.get('category')}" if a else "")
                           + (f" | note: {g['note'][:80]}" if g.get("note") else ""))
        return finish(out)
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
                *([f"dashboard: {link(tab='errors' if g['level'] == 'error' else 'suspect' if g['level'] == 'suspect' else 'all', hours=int(hours), etype=g['etype'], sig=g['sig'])}"]
                  if detail == "full" else []),
                "latest example:", "```", cap(g["sample"], 1500 if detail == "full" else 600), "```"]
    return finish(out)


@tool()
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


@tool()
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


@tool()
@guard
def log_group_volume(hours: float = 24, profile: str = "", limit: int = 25) -> str:
    """What's coming through each log group / state machine: total lines and the mix of
    errors, hidden errors, warnings, info and Lambda platform lines, plus when it last logged."""
    note = ensure_range(hours)
    rows = api("/api/volume", hours=hours, profile=profile)
    out = [note + f"{len(rows)} log groups, last {hours}h — dashboard: {link(tab='volume', hours=int(hours), profile=profile)}"]
    show = rows if limit <= 0 else rows[:limit]
    for v in show:
        out.append(f"- {v['profile']} | {v['group']}: {v['total']} lines (errors {v['error']}, hidden "
                   f"{v['suspect']}, warn {v['warning']}, info {v['info']}, platform {v['platform']}); "
                   f"last {t(v['last'])[5:16]}")
    rest = rows[len(show):]
    if rest:
        problem = [v for v in rest if v["error"] or v["suspect"]]
        out.append(f"+{len(rest)} quieter groups ({sum(v['total'] for v in rest)} lines; "
                   f"{sum(v['error'] for v in rest)} errors, {sum(v['suspect'] for v in rest)} hidden)"
                   + (": with problems - " + ", ".join(f"{v['profile']}|{v['group'].split('/')[-1]} "
                                                       f"(e{v['error']}/h{v['suspect']})" for v in problem[:15])
                      if problem else "") + ". limit=0 lists all.")
    return finish(out)


@tool()
@guard
def load_history(hours: float) -> str:
    """Ask the dashboard to pull an older range (e.g. 168 = 7 days, 720 = 30 days) from AWS.
    It loads in the background; other tools then cover that range. Pulled history is kept 24h."""
    api("/api/backfill", hours=hours)
    return ensure_range(hours) or f"The last {hours}h are already loaded."


@tool()
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


@tool()
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


@tool()
@guard
def unmute_pattern(error_type: str, signature: str, profile: str = "") -> str:
    """Undo mute_pattern for a pattern."""
    r = api("/api/unmute", etype=error_type, sig=signature, profile=profile)
    return "Unmuted." if r.get("removed") else "No matching mute found (check profile)."


@tool()
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


@tool()
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


@tool()
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


@tool()
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


@tool()
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


@tool()
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


@tool()
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


@tool()
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


@tool()
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


@tool()
@guard
def daily_digest(profile: str = "") -> str:
    """Per-client summary of what needs attention today: broken schedules, regressions, new and
    spiking errors, deploy results, partner API problems, firing alarms, API 5xx, functions near
    their limits and the upgrade backlog. Good as a morning check or a summary to post."""
    r = api("/api/digest", profile=profile)
    return finish([_scan_note(profile) + r["text"], f"\nDashboard: {link(tab='digest', profile=profile)}"])


@tool()
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


@tool()
@guard
def clients_status() -> str:
    """Red/amber/green status per client with reasons, error counts (1h/6h/24h) and recent deploys.
    Start here for "how are my clients?"."""
    cards = api("/api/ops/clients")
    out = [f"Dashboard: {link(tab='clients')} (★ = priority project)"]
    asleep = [c["profile"] for c in cards if c.get("asleep")]
    for c in [c for c in cards if not c.get("asleep")]:
        trend = lambda a, b: "" if b is None else (" (up from %d)" % b if a > b * 1.2 and a - b > 2 else
                                                   " (down from %d)" % b if a < b * 0.8 and b - a > 2 else "")
        out.append(f"\n## {'★ ' if c.get('priority') == 'high' else ''}{c['profile']}: {c['status'].upper()}"
                   + (" (on demand)" if c.get("priority") == "on_demand" else ""))
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
    if asleep:
        out.append(f"\nOn demand, not loaded: {', '.join(asleep)} (ask about one with profile=<name> to load it).")
    return finish(out)


@tool()
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


@tool()
@guard
def list_runs(function: str, profile: str = "", hours: float = 24, only_problems: bool = False,
              limit: int = 30, detail: str = "brief") -> str:
    """A Lambda's individual runs (newest first): end time, duration, records processed, errors,
    hidden errors, memory, outcome (ok / failed / timeout / hidden errors / no records) and the first
    problem line. detail="brief" (default) folds runs of consecutive clean OK runs into one line;
    detail="full" lists every run (up to `limit`). Use get_run to read one run's log."""
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
    shown, ok_run = 0, []

    def flush_ok():
        if not ok_run:
            return
        if len(ok_run) == 1 or detail == "full":
            for x in ok_run:
                out.append(f"- {t(x['end'])} | {x['duration_ms'] / 1000:.1f}s | records {x['records'] if x['records'] is not None else '-'} | OK | request_id {x['request_id']}")
        else:
            recs = [x["records"] for x in ok_run if x["records"] is not None]
            out.append(f"- {len(ok_run)} OK runs {t(ok_run[-1]['end'])} → {t(ok_run[0]['end'])}, "
                       f"avg {sum(x['duration_ms'] for x in ok_run) / len(ok_run) / 1000:.1f}s"
                       + (f", records {min(recs)}–{max(recs)}" if recs else "") + " (no problems)")
        ok_run.clear()
    same = []

    def pkey(r):
        fp = (r.get("first_problem") or {}).get("line") or ""
        return (r["outcome"], _NUM.sub("#", fp[:160]))

    def flush_same():
        if not same:
            return
        if len(same) > 2:
            x = same[0]
            errs = sum(y["errors"] for y in same)
            hid = sum(y["hidden"] for y in same)
            out.append(f"- {len(same)} more {x['outcome'].upper()} runs {t(same[-1]['end'])[5:16]} → {t(same[0]['end'])[5:16]} "
                       f"with the same first problem (errors {errs}, hidden {hid} in total; "
                       f"request_ids {same[-1]['request_id']} … {same[0]['request_id']})")
        else:
            for x in same:
                out.append(f"- {t(x['end'])} | {x['duration_ms'] / 1000:.1f}s | records {x['records'] if x['records'] is not None else '-'} | "
                           f"errors {x['errors']} hidden {x['hidden']} | {x['outcome'].upper()} | request_id {x['request_id']} (same problem as above)")
        same.clear()
    last_key = None
    for r in runs[: max(1, min(limit, 300))]:
        if detail != "full" and r["outcome"] == "ok" and not r.get("first_problem") and not r["errors"] and not r["hidden"]:
            flush_same()
            last_key = None
            ok_run.append(r)
            continue
        flush_ok()
        if detail != "full" and last_key is not None and pkey(r) == last_key:
            same.append(r)
            continue
        flush_same()
        last_key = pkey(r)
        out.append(f"- {t(r['end'])} | {r['duration_ms'] / 1000:.1f}s | records {r['records'] if r['records'] is not None else '-'} | "
                   f"errors {r['errors']} hidden {r['hidden']} | {r['mem_used']}/{r['memory']} MB | {r['outcome'].upper()}"
                   f"{' (cold start)' if r['cold_start'] else ''} | request_id {r['request_id']}"
                   + (f"\n    first problem: {r['first_problem']['line'][:250]}" if r.get("first_problem") else ""))
    flush_ok()
    flush_same()
    if len(runs) > limit:
        out.append(f"(+{len(runs) - limit} older runs; raise limit to see them)")
    return finish(out)


@tool()
@guard
def get_run(function: str, request_id: str, profile: str = "", detail: str = "brief") -> str:
    """What one Lambda run logged, in order (request_id from list_runs) - the clearest way to read
    what happened inside a single failing or empty run. detail="brief" (default) collapses repeated
    lines and keeps every error / hidden-error line with what came just before it; detail="full"
    returns every line. For a summary instead, use summarize_logs."""
    m = _find_group(function, profile)
    if not m:
        return f"No Lambda runs loaded for '{function}'."
    g = m[0]
    r = api("/api/ops/run", profile=g["profile"], region=g["region"], grp=g["group"], request_id=request_id)
    if not r:
        return "Run not found in the loaded logs (it may be older than the 24-hour cache)."
    out = [f"{g['profile']} | {g['group']} | request {request_id} | ended {t(r['end'])} | {r['duration_ms'] / 1000:.1f}s | "
           f"{r['outcome'].upper()} | records {r['records']} | {r['mem_used']}/{r['memory']} MB | stream {r['stream']}"]
    lines = r.get("lines_list", [])
    if detail == "full":
        for l in lines:
            out.append(f"{t(l['ts'])} [{l['level']}] {cap(l['message'], 2500)}")
        return finish(out)
    out += _brief_lines(lines)
    return finish(out)


@tool()
@guard
def did_nothing_check(profile: str = "") -> str:
    """Syncs that ran without erroring but processed 0 records or far fewer than usual (from lines
    like "Processed 25 deals"), and log groups that suddenly went quiet or silent compared with the
    previous week (usually the source system stopped sending data)."""
    rows = api("/api/ops/nothing", profile=profile)
    out = [f"{len(rows)} findings — dashboard: {link(tab='runs', profile=profile)}"]
    out += [f"- {d['profile']} | {d['group']} | {d['kind'].upper()}: {d['detail']}" for d in rows] or ["- nothing suspicious"]
    return finish(out)


@tool()
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
        ev = api("/api/context", id=event_id, before=0, after=0)
        msg = next((x["message"] for x in ev.get("lines", []) if x.get("id") == event_id), "") or r.get("message", "")
        sites = find_print_site(msg, r["mapped"].get("dir"))
        if sites:
            out.append("No stack trace - the code that printed this line is most likely:")
            for s in sites:
                out += [f"\n{s['path']}:{s['line']}", "```", s["snippet"], "```"]
        else:
            out.append("No stack-trace frames from your own code in this line (library-only or no traceback).")
    return finish(out)


@tool()
@guard
def add_note(error_type: str, signature: str, note: str, profile: str = "", ignore_below_per_day: int = 0) -> str:
    """Attach a team note to an error pattern ("known HubSpot rate limit, retry handles it") - shown
    in the dashboard and in every tool output for that pattern. ignore_below_per_day > 0 keeps it out
    of the 'needs attention' lists while it stays under that daily count. Empty note removes it.
    Only add notes when the user asks."""
    api("/api/ops/note", etype=error_type, sig=signature, profile=profile, note=note,
        ignore_below=ignore_below_per_day or "")
    return "Note saved." if note or ignore_below_per_day else "Note removed."


@tool()
@guard
def client_report(profile: str, days: float = 7, tower_summary: bool = False) -> str:
    """Client-facing health report for one client over `days`: Lambda and workflow success rates
    (from CloudWatch metrics), records processed, errors vs the previous period, issues resolved,
    open items, scheduled-job problems, changes deployed, third-party service issues and recommended
    maintenance. Returned as markdown - offer to turn it into a doc. Printable version via the link.
    tower_summary=True adds a short client-friendly summary paragraph written by the local model."""
    r = api("/api/ops/report", profile=profile, days=days)
    if r.get("error"):
        return r["error"]
    out = [r["markdown"], f"\nPrintable / PDF: {base()}/report?profile={urllib.parse.quote(profile)}&days={days:g}"]
    if tower_summary:
        jid, ans, err = _tower_job("narrative", r["markdown"], seconds=60)
        out.insert(0, f"**Summary** (drafted by the local model - review): {ans.strip()}\n" if ans else
                   f"(No local summary: {err or 'still being written, try again shortly'})\n")
    return finish(out)


def _wait_job(jid, seconds=45):
    j = {}
    for _ in range(int(seconds / 1.5)):
        j = api("/api/ai/job", id=jid)
        if j.get("status") != "running":
            return j
        time.sleep(1.5)
    return j


@tool()
@guard
def summarize_logs(question: str = "", profile: str = "", level: str = "", log_group: str = "",
                   log_group_prefix: str = "", search: str = "", error_type: str = "", signature: str = "",
                   hours: float = 24, limit: int = 300, job_id: str = "") -> str:
    """The LOCAL model reads up to `limit` matching log lines and returns a short summary / answer to
    `question` - prefer over get_log_lines for the gist. Poll with job_id if still working."""
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


@tool()
@guard
def investigate(profile: str, function: str = "", error_type: str = "", signature: str = "", hours: float = 24) -> str:
    """Broad look at a client (optionally one function / error): top patterns with notes and triage,
    latest example, preceding lines, code location and run outcomes."""
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


@tool()
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


@tool()
@guard
def did_my_fix_work(function: str, profile: str = "", window_hours: float = 24,
                    deployed_at: str = "") -> str:
    """After a deploy: compares errors before/after the latest deploy of `function` and says whether
    the fix worked (or if it's too early)."""
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


# --------------------------------------------------------------------------- local reading (code, docs, git)
import fnmatch
import re as _re

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", ".idea", ".vscode", "dist", "build",
             ".serverless", "site-packages", ".pytest_cache", ".mypy_cache", "archive", "coverage", ".next"}
# never sent anywhere: secrets, keys, credentials, local databases
DENY = _re.compile(r"(^\.env|secret|credential|password|\.pem$|\.key$|\.pfx$|\.p12$|id_rsa|\.db$|\.sqlite|"
                   r"\.db-wal$|\.db-shm$|\.aws)", _re.I)
TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".md", ".txt",
            ".csv", ".tsv", ".sql", ".sh", ".ps1", ".bat", ".html", ".css", ".xml", ".asl", ".graphql", ".java",
            ".cs", ".go", ".rb", ".php", ".r", ".ipynb", ".tf", ".hcl", ".env.example", ".log", ""}
MAX_FILE = 400_000
MAX_TOTAL = 4_000_000


def _roots():
    roots = [r for r in (REPOS or "").split(";") if r.strip()]
    roots.append(os.path.dirname(HERE))                   # the dashboard repo itself
    return [os.path.realpath(r.strip()) for r in roots]


def _resolve(path):
    """A path inside the allowed folders (your repos root and the dashboard), absolute or relative."""
    p = path.strip().strip('"')
    cands = [p] if os.path.isabs(p) else [os.path.join(r, p) for r in _roots()]
    for c in cands:
        real = os.path.realpath(c)
        if any(real == r or real.startswith(r + os.sep) for r in _roots()) and os.path.exists(real):
            return real
    raise ValueError(f"'{path}' isn't inside the allowed folders ({'; '.join(_roots())}). "
                     f"Set AWS_ERROR_FEED_REPOS to your repos folder (several: separate with ;).")


def _is_text(path):
    try:
        with open(path, "rb") as f:
            return b"\0" not in f.read(4096)
    except OSError:
        return False


def _collect(path, include, max_files):
    docs, skipped, total = [], [], 0
    base_dir = path if os.path.isdir(path) else os.path.dirname(path)
    files = [path] if os.path.isfile(path) else []
    if os.path.isdir(path):
        for dp, dns, fns in os.walk(path):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
            for fn in sorted(fns):
                files.append(os.path.join(dp, fn))
    pats = [x.strip() for x in include.split(",") if x.strip()]
    for f in files:
        rel = os.path.relpath(f, base_dir).replace("\\", "/")
        name = os.path.basename(f)
        if pats and not any(fnmatch.fnmatch(name, x) or fnmatch.fnmatch(rel, x) for x in pats):
            continue
        if DENY.search(name):
            skipped.append(f"{rel} (looks like secrets/credentials - never sent)")
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in TEXT_EXT or not _is_text(f):
            continue
        size = os.path.getsize(f)
        if size > MAX_FILE:
            skipped.append(f"{rel} (too big: {size // 1000} KB)")
            continue
        if len(docs) >= max_files or total + size > MAX_TOTAL:
            skipped.append(f"{rel} (limit reached)")
            continue
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        docs.append({"name": rel, "text": text})
        total += len(text)
    return docs, skipped


def _read_job(docs, question, kind, header, skipped=(), job_id=""):
    if not job_id:
        r = api_post("/api/ai/read", {"question": question, "docs": docs, "kind": kind})
        if r.get("error"):
            return r["error"]
        job_id = r["id"]
        header += f" ({r.get('chars', 0) // 1000}k chars, {r.get('parts', 1)} part(s))"
    j = _wait_job(job_id, seconds=50)
    if j.get("status") == "running":
        prog = f" ({j.get('done_parts')}/{j.get('parts')} parts read)" if j.get("parts", 1) > 1 else ""
        return (f"The local model is still reading{prog}. Call {kind == 'git' and 'local_git' or 'local_read'}"
                f"(job_id=\"{job_id}\") again in ~30s.")
    if j.get("error"):
        return f"Local model failed: {j['error']}. Read the files directly instead."
    out = [header + " - local-model summary, verify before editing:", j.get("answer") or ""]
    if skipped:
        out.append("Skipped: " + "; ".join(list(skipped)[:12]) + (f" (+{len(skipped) - 12} more)" if len(skipped) > 12 else ""))
    return finish(out)


@tool()
@guard
def local_read(path: str = "", question: str = "", include: str = "", max_files: int = 60, job_id: str = "") -> str:
    """The LOCAL model reads a file or folder from the repos (path relative to the repos root, e.g.
    "hrs/serverless_lambdas/hrs_deals") and returns a summary with file:line refs or an answer to
    `question`. include="*.py" filters. Secrets are never sent. Poll with job_id."""
    if job_id:
        return _read_job(None, question, "read", "Read", job_id=job_id)
    try:
        real = _resolve(path)
    except ValueError as ex:
        return str(ex)
    docs, skipped = _collect(real, include, max(1, min(max_files, 200)))
    if not docs:
        return f"No readable text files found at {real}" + (f" (skipped: {'; '.join(skipped[:5])})" if skipped else ".")
    return _read_job(docs, question, "read", f"Read {len(docs)} file(s) from {real}", skipped)


_REF_OK = _re.compile(r"^[\w./~^@{}:+-]{1,100}$")


@tool()
@guard
def local_git(repo: str = "", what: str = "log", ref: str = "", n: int = 30, path: str = "", question: str = "",
              job_id: str = "") -> str:
    """The LOCAL model summarises git history or changes: what="log" (last n commits), "diff"
    (uncommitted or ref like main...branch) or "show" (one commit). Read-only."""
    if job_id:
        return _read_job(None, question, "git", "Git", job_id=job_id)
    try:
        real = _resolve(repo)
    except ValueError as ex:
        return str(ex)
    if ref and not _REF_OK.match(ref):
        return f"'{ref}' doesn't look like a git ref."
    n = max(1, min(int(n or 30), 300))
    if what == "log":
        cmd = ["git", "log", f"-n{n}", "--stat", "--date=short", "--format=commit %h  %ad  %an%n    %s%n"]
        cmd += [ref] if ref else []
    elif what == "diff":
        cmd = ["git", "diff", "--stat", "--patch"] + ([ref] if ref else ["HEAD"])
    elif what == "show":
        if not ref:
            return "show needs a commit: ref=\"<hash>\""
        cmd = ["git", "show", "--stat", "--patch", ref]
    else:
        return 'what must be "log", "diff" or "show".'
    if path:
        cmd += ["--", path]
    try:
        r = subprocess.run(cmd, cwd=real, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return f"git failed: {ex}"
    text = r.stdout.decode("utf-8", errors="replace")
    if r.returncode != 0:
        return "git error: " + r.stderr.decode("utf-8", errors="replace")[:500]
    if not text.strip():
        return "Nothing to show (no commits / no changes)."
    if len(text) > MAX_TOTAL:
        text = text[:MAX_TOTAL] + "\n... (cut)"
    title = f"git {what} {ref}".strip() + (f" -- {path}" if path else "")
    return _read_job([{"name": title, "text": text}], question, "git", f"{title} in {real}")


# --------------------------------------------------------------------------- find code (no model)
import difflib as _difflib
import hashlib as _hashlib
import shlex as _shlex

CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".md", ".sql",
            ".sh", ".ps1", ".bat", ".html", ".css", ".xml", ".asl", ".graphql", ".java", ".cs", ".go", ".rb",
            ".php", ".tf", ".hcl"}
DATA_EXT = {".csv", ".tsv", ".xlsx", ".xls", ".log", ".txt", ".parquet", ".zip", ".pdf", ".png", ".jpg"}
STOP = set("the a an and or of to in on for is are was be by with from at as it this that where what how which "
           "does do set get when who why find code line lines file files function functions".split())
DECL = _re.compile(r"^\s*(?:async\s+)?(?:def|class|function|export\s+(?:default\s+)?(?:function|class|const)|const\s+\w+\s*=\s*(?:async\s*)?\(|"
                   r"[\w-]+:\s*$|CREATE\s+(?:TABLE|VIEW|FUNCTION))", _re.I)


def _code_files(root, include="", limit=4000):
    pats = [x.strip() for x in include.split(",") if x.strip()]
    files = [root] if os.path.isfile(root) else []
    if os.path.isdir(root):
        for dp, dns, fns in os.walk(root):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
            for fn in sorted(fns):
                files.append(os.path.join(dp, fn))
                if len(files) > limit:
                    break
    out = []
    base_dir = root if os.path.isdir(root) else os.path.dirname(root)
    for f in files:
        name, rel = os.path.basename(f), os.path.relpath(f, base_dir).replace("\\", "/")
        if DENY.search(name):
            continue
        if pats and not any(fnmatch.fnmatch(name, x) or fnmatch.fnmatch(rel, x) for x in pats):
            continue
        if not pats and os.path.splitext(name)[1].lower() not in CODE_EXT:
            continue
        try:
            if os.path.getsize(f) > MAX_FILE:
                continue
        except OSError:
            continue
        out.append((f, rel))
    return out


def _top_roots():
    rs = sorted({r for r in _roots() if os.path.isdir(r)}, key=len)
    return [r for i, r in enumerate(rs) if not any(r.startswith(o + os.sep) for o in rs[:i])]


@tool()
@guard
def find_code(query: str, repo: str = "", include: str = "", max_results: int = 30, context: int = 1) -> str:
    """Search the repos on the laptop (no model, instant): returns only the best-matching lines with
    file:line. query = words or /regex/; repo narrows it; include="*.py" filters."""
    try:
        root = _resolve(repo) if repo else None
    except ValueError as ex:
        return str(ex)
    roots = [root] if root else _top_roots()
    q = query.strip()
    rx = _re.compile(q[1:-1], _re.I) if len(q) > 2 and q.startswith("/") and q.endswith("/") else None
    terms = [] if rx else [w for w in _re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", q.lower()) if w not in STOP]
    if not rx and not terms:
        return "Give at least one word of 3+ letters (or a /regex/)."
    phrase = q.lower() if not rx and " " in q else None
    hits, scanned = [], 0
    for r in roots:
        for f, rel in _code_files(r, include):
            scanned += 1
            try:
                lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            shown_rel = os.path.relpath(f, r if os.path.isdir(r) else os.path.dirname(r)).replace("\\", "/")
            fname = rel.lower()
            for i, line in enumerate(lines):
                low = line.lower()
                if rx:
                    if not rx.search(line):
                        continue
                    score = 2 + (3 if DECL.match(line) else 0)
                else:
                    n = sum(1 for w in terms if w in low)
                    if not n or n < (len(terms) + 1) // 2:
                        continue
                    score = 2 * n + (3 if DECL.match(line) else 0) + (5 if phrase and phrase in low else 0) \
                        + sum(1 for w in terms if w in fname)
                hits.append((score, shown_rel, i, lines))
    if not hits:
        return f"No matches for {query!r} in {scanned} file(s)" + (f" under {root}" if root else "") + "."
    hits.sort(key=lambda h: (-h[0], h[1], h[2]))
    top_hits = hits[: max(1, min(max_results, 100))]
    by_file = {}
    for s, rel, i, lines in top_hits:
        by_file.setdefault(rel, []).append((i, lines))
    out = [f"{len(hits)} matching line(s) in {scanned} file(s){' under ' + root if root else ''}; best {len(top_hits)}:"]
    ctx = max(0, min(context, 3))
    for rel, items in by_file.items():
        out.append(f"\n{rel}")
        done = set()
        for i, lines in sorted(items, key=lambda x: x[0]):
            for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                if j in done:
                    continue
                done.add(j)
                mark = ">" if j == i else " "
                out.append(f"{mark}{j + 1:>5}| {lines[j][:200]}")
    out.append("\nOpen only the lines you need; local_read(path, question) explains a file without reading it all.")
    return finish(out)


# --------------------------------------------------------------------------- repo map (cached)
_SYM = {
    ".py": _re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)", _re.M),
    ".js": _re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\()", _re.M),
    ".ts": _re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\(|class\s+(\w+))", _re.M),
}
_map_jobs = {}


def _symbols(text, ext):
    rx = _SYM.get(ext) or (_SYM[".js"] if ext in (".jsx", ".tsx") else None)
    if not rx:
        return []
    out = []
    for m in rx.finditer(text):
        name = next((g for g in m.groups() if g), None) if m.groups() else m.group(1)
        if name and not name.startswith("_") and name not in out:
            out.append(name)
    return out


@tool()
@guard
def repo_map(repo: str, focus: str = "", budget: int = 0, describe: bool = True, max_entries: int = 250) -> str:
    """Repo layout: files, line counts, functions and a cached one-line purpose each. focus="words"
    returns only the most relevant files (and their callers) within `budget` tokens."""
    try:
        root = _resolve(repo)
    except ValueError as ex:
        return str(ex)
    files = _code_files(root, "")
    info, data_counts = [], {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            ext = os.path.splitext(fn)[1].lower()
            if ext in DATA_EXT:
                d = os.path.relpath(dp, root).replace("\\", "/")
                data_counts.setdefault(d, {}).setdefault(ext, 0)
                data_counts[d][ext] += 1
    for f, rel in files[:1500]:
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        ext = os.path.splitext(f)[1].lower()
        info.append({"name": rel, "sha": _hashlib.sha1(text.encode("utf-8", "replace")).hexdigest(),
                     "lines": text.count("\n") + 1, "syms": _symbols(text, ext), "text": text})
    purposes, note = {}, ""
    if describe and info:
        try:
            purposes = api_post("/api/ai/filemap_known", {"files": [{"name": i["name"], "sha": i["sha"]} for i in info]})
        except FeedDown:
            purposes = {}
        missing = [i for i in info if i["name"] not in purposes and i["name"].endswith(
            (".py", ".js", ".ts", ".tsx", ".jsx", ".sql", ".yml", ".yaml", ".asl", ".json")) and i["lines"] > 3]
        job = _map_jobs.get(root)
        if job:
            try:
                j = api("/api/ai/job", id=job)
                purposes.update({k: v for k, v in (j.get("purposes") or {}).items()})
                if j.get("status") == "running":
                    note = f"(The tower is describing files: {j.get('done_parts')}/{j.get('parts')} done - call again later for the rest.)"
                else:
                    _map_jobs.pop(root, None)
            except FeedDown:
                pass
        missing = [i for i in missing if i["name"] not in purposes]
        if missing and root not in _map_jobs:
            batch = [{"name": i["name"], "sha": i["sha"], "text": i["text"][:60000]} for i in missing[:400]]
            try:
                r = api_post("/api/ai/filemap", {"files": batch})
                if r.get("id"):
                    _map_jobs[root] = r["id"]
                    note = f"(Started describing {len(batch)} file(s) on the tower - purposes appear on later calls.)"
                elif r.get("error"):
                    note = f"(No purposes: {r['error']})"
            except FeedDown as ex:
                note = f"(No purposes: {ex})"
    if focus:
        return finish(_focused_map(root, info, purposes, focus, budget or 1500, note))
    by_dir = {}
    for i in info:
        d = os.path.dirname(i["name"]) or "."
        by_dir.setdefault(d, []).append(i)
    out = [f"Repo map: {root} — {len(info)} code/config files" + (f" {note}" if note else "")]
    shown = 0
    for d in sorted(by_dir):
        items = by_dir[d]
        dc = data_counts.get(d if d != "." else ".", {})
        extra = f"  + data: " + ", ".join(f"{n} {e}" for e, n in sorted(dc.items())) if dc else ""
        out.append(f"\n{d}/  ({len(items)} files){extra}")
        if shown >= max_entries:
            out.append("   (files not listed - raise max_entries or map a sub-folder)")
            continue
        for i in sorted(items, key=lambda x: x["name"]):
            shown += 1
            syms = (" · " + ", ".join(i["syms"][:8]) + ("…" if len(i["syms"]) > 8 else "")) if i["syms"] else ""
            p = purposes.get(i["name"])
            out.append(f"  {os.path.basename(i['name'])} ({i['lines']})" + (f" — {p}" if p else "") + syms)
    other = {d: c for d, c in data_counts.items() if d not in by_dir and d != "."}
    if other:
        out.append("\nData-only folders: " + "; ".join(f"{d}/ ({sum(c.values())} files)" for d, c in sorted(other.items())[:20]))
    return finish(out)


def _focused_map(root, info, purposes, focus, budget, note):
    terms = [w for w in _re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", focus.lower()) if w not in STOP]
    if not terms:
        return ["Give focus words of 3+ letters."]
    score = {}
    for i in info:
        low, name, p = i["text"].lower(), i["name"].lower(), (purposes.get(i["name"]) or "").lower()
        syms = [s.lower() for s in i["syms"]]
        score[i["name"]] = sum(3 * (w in name) + 2 * (w in p) + 2 * any(w in s for s in syms)
                               + min(3.0, low.count(w) / 5) for w in terms)
    relevant = [i for i in info if score[i["name"]] > 0]
    relevant.sort(key=lambda i: -score[i["name"]])
    defs = {}
    for i in relevant[:40]:
        for s in i["syms"]:
            if len(s) >= 6:
                defs.setdefault(s, i["name"])
    bonus = {}
    for i in info:                       # files that use what the relevant files define
        for s, owner in defs.items():
            if owner != i["name"] and s in i["text"]:
                bonus[i["name"]] = bonus.get(i["name"], 0) + 0.3 * score[owner]
                bonus[owner] = bonus.get(owner, 0) + 0.1 * score[owner]
    total = lambda i: score[i["name"]] + bonus.get(i["name"], 0)
    ranked = sorted([i for i in info if total(i) > 0], key=lambda i: -total(i))
    out = [f"Repo map for {focus!r} in {root} — most relevant first (~{budget} tokens; {len(info)} files in repo)"
           + (f" {note}" if note else "")]
    size, limit = len(out[0]), budget * 4
    for i in ranked:
        syms = (" · " + ", ".join(i["syms"][:10]) + ("…" if len(i["syms"]) > 10 else "")) if i["syms"] else ""
        p = purposes.get(i["name"])
        line = f"  {i['name']} ({i['lines']})" + (f" — {p}" if p else "") + syms
        if size + len(line) > limit:
            out.append(f"  … {len(ranked) - len(out) + 1} more relevant file(s) - raise budget to see them")
            break
        out.append(line)
        size += len(line)
    if len(out) == 1:
        out.append("No files match that focus - try other words, or call repo_map without focus.")
    out.append("Next: outline(file) for signatures, get_symbol(name) for one function.")
    return out


# --------------------------------------------------------------------------- fix brief
def _tower_job(template, text, question="", seconds=50):
    """(job_id if still running, answer, error)"""
    r = api_post("/api/ai/prompt", {"template": template, "text": text, "question": question})
    if r.get("error"):
        return None, None, r["error"]
    j = _wait_job(r["id"], seconds=seconds)
    if j.get("status") == "running":
        return r["id"], None, None
    if j.get("error"):
        return None, None, j["error"]
    return None, j.get("answer") or "", None


@tool()
@guard
def fix_brief(profile: str, function: str = "", error_type: str = "", signature: str = "", hours: float = 24,
              job_id: str = "") -> str:
    """Everything to fix ONE error in one call: pattern, triage, latest example, preceding logs, the
    failing code (or the print that wrote it), recent runs + a local diagnosis (verify it). Poll
    with job_id."""
    if job_id:
        j = _wait_job(job_id, seconds=50)
        if j.get("status") == "running":
            return f"Still writing the diagnosis - call fix_brief(profile=\"{profile}\", job_id=\"{job_id}\") again shortly."
        return finish(["TOWER DIAGNOSIS (local model - verify before editing):", j.get("answer") or j.get("error") or ""])
    prefix = f"/aws/lambda/{function}" if function and not function.startswith("/") else function
    rows = api("/api/summary", hours=hours, profile=profile, level="error,suspect", hide_muted=1,
               grp_prefix=prefix or "", etype=error_type, sig=signature if error_type else "")
    if not rows:
        return f"No errors or hidden errors for {profile}{' / ' + function if function else ''} in {hours:g}h."
    g = rows[0]
    b = [f"FIX BRIEF: {profile} — [{g['level']}] {g['etype']}: {g['sig'][:200]} — {g['count']}x in {hours:g}h{flags(g)}",
         f"log groups: {top(g['log_groups'], 3)} | first {t(g['first_seen'])}, last {t(g['last_seen'])}"]
    ev = api("/api/events", profile=profile, etype=g["etype"], sig=g["sig"], hours=hours, limit=1)["events"]
    if ev:
        e = ev[0]
        b += [f"\nlatest ({t(e['ts'])}, event_id {e['id']}):", cap(e["message"], 1800)]
        ctx = api("/api/context", id=e["id"], before=2, after=0.5)
        before, seen_msg = [], set()
        for x in reversed([x for x in ctx.get("lines", []) if x["id"] != e["id"]]):
            if x["message"] in seen_msg:
                continue
            seen_msg.add(x["message"])
            before.insert(0, x)
            if len(before) >= 8:
                break
        if before:
            b.append("\nlogged just before:")
            b += [f"  {t(x['ts'])} [{x['level']}] {cap(x['message'], 220)}" for x in before]
        code = local_code(api("/api/ops/code", id=e["id"]))
        own = [f for f in code.get("frames", []) if f.get("path")]
        if own:
            f = own[-1]
            b += [f"\ncode: {f['path']}:{f['line']}", f.get("snippet") or ""]
        elif code.get("mapped"):
            sites = find_print_site(e["message"], code["mapped"].get("dir"))
            if sites:
                s0 = sites[0]
                b += [f"\ncode that printed this line: {s0['path']}:{s0['line']}", s0["snippet"]]
                b += [f"(also possible: {x['path']}:{x['line']})" for x in sites[1:2]]
            else:
                b.append(f"\nrepo folder: {code['mapped'].get('dir')} (no stack frame and no matching print found - use find_code)")
        if str(e.get("group", "")).startswith("stepfunctions:"):
            b.append("(Step Functions failure: get_step_function_input gives the exact input to reproduce it.)")
    groups = [x for x in api("/api/ops/groups", profile=profile, hours=hours) if x["group"] in list(g["log_groups"])[:1]]
    for grp in groups[:1]:
        runs = api("/api/ops/runs", profile=grp["profile"], region=grp["region"], grp=grp["group"], hours=hours)
        if runs:
            from collections import Counter
            oc = Counter(r["outcome"] for r in runs)
            b.append(f"\nruns of {grp['function']}: {len(runs)} - " + ", ".join(f"{k} {v}" for k, v in oc.most_common())
                     + f"; latest {t(runs[0]['end'])} {runs[0]['outcome']}")
    brief = "\n".join(b)
    try:
        jid, ans, err = _tower_job("fix", brief)
    except FeedDown as ex:
        jid, ans, err = None, None, str(ex)
    tail = (["\nTOWER DIAGNOSIS (local model - verify before editing):", ans] if ans else
            [f"\n(No local diagnosis: {err})"] if err else
            [f"\n(The tower is still writing a diagnosis: fix_brief(profile=\"{profile}\", job_id=\"{jid}\").)"])
    return finish([brief, *tail])


# --------------------------------------------------------------------------- run tests / checks
_SAFE_CMDS = [["pytest"], ["python", "-m", "pytest"], ["python", "-m", "py_compile"], ["python", "-m", "unittest"],
              ["py", "-m", "pytest"], ["py", "-m", "py_compile"], ["python3", "-m", "pytest"],
              ["npm", "test"], ["npm", "run", "test"], ["npm", "run", "lint"], ["node", "--test"],
              ["git", "status"], ["serverless", "print"], ["sls", "print"]]
_BAD_CHARS = _re.compile(r"[;&|<>`$\n\r]")


def _allowed(argv):
    if any(argv[:len(p)] == p for p in _SAFE_CMDS):
        return True
    # python <test script> e.g. python local_tests/run_tests.py
    return len(argv) >= 2 and argv[0] in ("python", "python3", "py") and _re.search(r"(^|[/\\])(test|tests|local_tests)[/\\]|(^|[/\\])test_\w+\.py$|_tests?\.py$|run_tests\.py$", argv[1]) is not None


@tool()
@guard
def local_run(command: str, repo: str, timeout_s: int = 600, job_id: str = "") -> str:
    """Run a safe check on the laptop (pytest, python -m pytest/unittest/py_compile, python <test
    script>, npm test/lint, node --test, git status, serverless print) and get only the result and
    failures. No shell operators."""
    if job_id:
        j = _wait_job(job_id, seconds=50)
        if j.get("status") == "running":
            return f"Still summarising - call local_run(command=..., repo=..., job_id=\"{job_id}\") again shortly."
        return finish(["Summary of the output (local model):", j.get("answer") or j.get("error") or ""])
    if _BAD_CHARS.search(command):
        return "Not allowed: no shell operators (; & | < > ` $)."
    try:
        argv = _shlex.split(command, posix=(os.name != "nt"))
        real = _resolve(repo)
    except ValueError as ex:
        return str(ex)
    if not argv or not _allowed(argv):
        return "Not an allowed command. Allowed: " + "; ".join(" ".join(p) for p in _SAFE_CMDS) + "; python <test script>."
    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=real if os.path.isdir(real) else os.path.dirname(real), capture_output=True,
                           timeout=max(10, min(timeout_s, 1800)), stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return f"'{argv[0]}' isn't installed / on PATH on this machine."
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout_s}s."
    text = (r.stdout + b"\n" + r.stderr).decode("utf-8", errors="replace").strip()
    head = f"`{command}` in {real}: exit code {r.returncode} ({time.time() - t0:.0f}s, {len(text.splitlines())} lines of output)"
    if len(text) <= 3000:
        return finish([head, "```", text, "```"])
    keylines = [l for l in text.splitlines() if _re.search(r"FAILED|ERROR|Error|Traceback|assert|passed|failed", l)][-25:]
    try:
        jid, ans, err = _tower_job("run", text[-60000:], f"Command: {command} (exit code {r.returncode})")
    except FeedDown:
        jid, ans, err = None, None, "tower unreachable"
    out = [head]
    if ans:
        out += ["Summary (local model):", ans]
    elif jid:
        out.append(f"(Summary still being written: local_run(command=..., repo=..., job_id=\"{jid}\").)")
    out += ["Key lines:", "```", *[l[:300] for l in keylines], "```"]
    return finish(out)


# --------------------------------------------------------------------------- drafts by the tower
@tool()
@guard
def draft_commit_message(repo: str) -> str:
    """The tower's local model writes a commit message for the staged changes (or, if nothing is
    staged, all uncommitted changes). Only drafts text - never commits."""
    try:
        real = _resolve(repo)
    except ValueError as ex:
        return str(ex)
    def git(*a):
        return subprocess.run(["git", *a], cwd=real, capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    diff = git("diff", "--cached", "--stat", "--patch")
    which = "staged"
    if not diff.strip():
        diff, which = git("diff", "HEAD", "--stat", "--patch"), "uncommitted"
    if not diff.strip():
        return "No changes to describe."
    jid, ans, err = _tower_job("commit", diff[:200000], seconds=60)
    if err:
        return f"The local model couldn't write it: {err}"
    if not ans:
        return f"The tower is still writing it - try again in a moment."
    return finish([f"Draft commit message for the {which} changes (local model - review it):", "```", ans.strip(), "```"])


@tool()
@guard
def local_draft_change(path: str, start_line: int, end_line: int, instruction: str, apply: bool = False,
                       job_id: str = "") -> str:
    """The LOCAL model writes a small edit (<=150 lines) for path lines start_line-end_line from
    `instruction` and returns a diff; the same call with apply=True writes it (syntax-checked).
    Review first; write complex logic yourself."""
    try:
        real = _resolve(path)
    except ValueError as ex:
        return str(ex)
    if not os.path.isfile(real) or DENY.search(os.path.basename(real)):
        return "Not a file I can edit."
    raw = open(real, "rb").read()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    s, e = max(1, int(start_line)), min(len(lines), int(end_line))
    if e < s or e - s > 150:
        return "Give a range of 1-150 lines (start_line <= end_line)."
    lo, hi = max(1, s - 40), min(len(lines), e + 40)
    excerpt = "\n".join((">>> " if s <= i <= e else "    ") + f"{i:>5}| {lines[i - 1]}" for i in range(lo, hi + 1))
    prompt = (f"File: {os.path.basename(real)}\nInstruction for lines {s}-{e} (marked >>>): {instruction}\n"
              f"Return the new text for lines {s}-{e} only.\n\n{excerpt}")
    if job_id:
        j = _wait_job(job_id, seconds=60)
        jid, ans, err = (job_id, None, None) if j.get("status") == "running" else (None, j.get("answer"), j.get("error"))
    else:
        jid, ans, err = _tower_job("edit", prompt, seconds=90)
    if err:
        return f"The local model couldn't draft it ({err}) - make this change yourself."
    if ans is None:
        return (f"The tower is still writing it - call local_draft_change(... same arguments ..., job_id=\"{jid}\") again shortly.")
    new = _re.sub(r"^\s*```[\w-]*\n", "", ans.rstrip())
    new = _re.sub(r"\n```\s*$", "", new).strip("\n")
    if new.strip().startswith("CANNOT:"):
        return "The local model declined: " + new.strip() + " - make this change yourself."
    new_lines = [l.rstrip() for l in new.split("\n")]
    # models sometimes drop the block's indentation: shift it back to where the original lines were
    ind = lambda l: len(l) - len(l.lstrip(" \t"))
    orig = [l for l in lines[s - 1:e] if l.strip()]
    newnb = [l for l in new_lines if l.strip()]
    if orig and newnb:
        want, got = ind(orig[0]), min(ind(l) for l in newnb)
        if got < want:
            pad = orig[0][:want - got] if orig[0][:want - got].strip() == "" else " " * (want - got)
            new_lines = [(pad + l) if l.strip() else l for l in new_lines]
    updated = lines[:s - 1] + new_lines + lines[e:]
    rel = os.path.basename(real)
    diff = list(_difflib.unified_diff(lines, updated, f"a/{rel}", f"b/{rel}", n=2, lineterm=""))
    if not diff:
        return "The local model returned the same code - nothing to change."
    src = "\n".join(updated)
    if real.endswith(".py"):
        try:
            compile(src, real, "exec")
        except SyntaxError as ex:
            return finish([f"Draft has a syntax error (line {ex.lineno}: {ex.msg}) - not applied. Diff:", "```diff", *diff, "```"])
    if apply:
        if open(real, "rb").read() != raw:
            return "The file changed while drafting - run it again."
        with open(real, "wb") as f:
            f.write((src.replace("\n", "\r\n") if crlf else src).encode("utf-8"))
        return finish([f"Applied to {real} (local-model draft - review and test):", "```diff", *diff, "```"])
    return finish([f"Draft change for {real} lines {s}-{e} (local model - review; same call with apply=True writes it):",
                   "```diff", *diff, "```"])


# --------------------------------------------------------------------------- symbol-level code access
import ast as _ast


def _py_symbols(text):
    """[(qualified name, kind, start, end, signature, doc first line)] for a Python file."""
    try:
        tree = _ast.parse(text)
    except SyntaxError:
        return []
    out = []

    def walk(nodes, prefix=""):
        for n in nodes:
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                name = prefix + n.name
                start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
                end = getattr(n, "end_lineno", n.lineno)
                if isinstance(n, _ast.ClassDef):
                    sig = f"class {n.name}"
                    kind = "class"
                else:
                    try:
                        args = _ast.unparse(n.args)
                    except Exception:
                        args = "..."
                    ret = ""
                    if n.returns is not None:
                        try:
                            ret = " -> " + _ast.unparse(n.returns)
                        except Exception:
                            pass
                    sig = f"{'async ' if isinstance(n, _ast.AsyncFunctionDef) else ''}def {n.name}({args}){ret}"
                    kind = "method" if prefix else "function"
                doc = (_ast.get_docstring(n) or "").strip().splitlines()
                out.append((name, kind, start, end, sig, doc[0][:120] if doc else ""))
                if isinstance(n, _ast.ClassDef):
                    walk(n.body, name + ".")
    walk(tree.body)
    return out


_JS_DEF = _re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s+(\w+)|class\s+(\w+)|"
                      r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)")


def _other_symbols(text):
    """Rough outline for JS/TS/other files: definition lines; a block ends where the next one at the same
    indentation (or less) starts."""
    lines = text.split("\n")
    found = []
    for i, l in enumerate(lines):
        m = _JS_DEF.match(l)
        if m:
            name = next(g for g in m.groups()[1:] if g)
            found.append((name, len(m.group(1)), i + 1, l.strip()[:160]))
    out = []
    for k, (name, ind, start, sig) in enumerate(found):
        end = len(lines)
        for name2, ind2, start2, _ in found[k + 1:]:
            if ind2 <= ind:
                end = start2 - 1
                break
        out.append((name, "function", start, end, sig, ""))
    return out


def _symbols_of(path, text):
    return _py_symbols(text) if path.endswith(".py") else _other_symbols(text)


@tool()
@guard
def outline(path: str) -> str:
    """Signatures of every class / function / method in a file (or every code file in a folder), with
    line ranges and the first docstring line - without the bodies. Much cheaper than reading the
    file; then use get_symbol for the one you need."""
    try:
        real = _resolve(path)
    except ValueError as ex:
        return str(ex)
    files = [(real, os.path.basename(real))] if os.path.isfile(real) else _code_files(real, "*.py,*.js,*.ts,*.jsx,*.tsx")[:80]
    out = []
    for f, rel in files:
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        syms = _symbols_of(f, text)
        out.append(f"\n{rel} ({text.count(chr(10)) + 1} lines)")
        for name, kind, s, e, sig, doc in syms:
            depth = name.count(".")
            out.append(f"{'  ' * (depth + 1)}{s}-{e}: {sig}" + (f"  # {doc}" if doc else ""))
        if not syms:
            out.append("  (no functions/classes found)")
    return finish(out or ["No code files found."])


@tool()
@guard
def get_symbol(name: str, repo: str = "", file: str = "", max_matches: int = 3) -> str:
    """Exact source of ONE function/class/method (e.g. "send_batch_upsert" or "Client.patch") with line
    numbers, instead of reading the file. repo/file narrow the search."""
    try:
        roots = [_resolve(file)] if file else ([_resolve(repo)] if repo else _top_roots())
    except ValueError as ex:
        return str(ex)
    want = name.strip()
    matches = []
    for r in roots:
        for f, rel in _code_files(r, "*.py,*.js,*.ts,*.jsx,*.tsx"):
            try:
                text = open(f, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if want.split(".")[-1] not in text:
                continue
            for qn, kind, s, e, sig, doc in _symbols_of(f, text):
                if qn == want or qn.split(".")[-1] == want or (("." in want) and qn.endswith(want)):
                    matches.append((f, rel, s, e, text))
    if not matches:
        return f"No function/class named {name!r} found" + (f" under {repo or file}" if (repo or file) else "") + \
            ". Try find_code for a text search."
    seen, uniq = set(), []
    for m in matches:                      # identical copies (shared helper per Lambda) count once
        body = "\n".join(m[4].split("\n")[m[2] - 1:m[3]])
        h = _hashlib.sha1(body.encode()).hexdigest()
        uniq.append((m, h in seen))
        seen.add(h)
    out = []
    if len(matches) > 1:
        out.append(f"{len(matches)} definitions of {name!r}:")
        for (f, rel, s, e, _), dup in uniq[:20]:
            out.append(f"  {f}:{s}-{e}" + ("  (identical copy)" if dup else ""))
    shown = 0
    for (f, rel, s, e, text), dup in uniq:
        if dup or shown >= max(1, min(max_matches, 5)):
            continue
        lines = text.split("\n")
        if e - s > 400:
            e = s + 400
        out += [f"\n{f}:{s}-{e}", "```", *[f"{i:>5}| {lines[i - 1]}" for i in range(s, e + 1)], "```"]
        shown += 1
    return finish(out)


# --------------------------------------------------------------------------- where a print() came from
def _message_fragments(message):
    """The fixed parts of a log line (what the code literally wrote), longest first."""
    first = message.strip().splitlines()[0] if message.strip() else ""
    parts = _re.split(r"\{.*?\}|'[^']*'|\"[^\"]*\"|\b\d[\d.,:-]*\b|\b[0-9a-f]{8,}\b", first)
    frags = []
    for p in parts:
        p = p.strip(" ,;|")
        for seg in _re.split(r"\s*[,;|]\s*", p):
            seg = seg.strip()
            if len(_re.sub(r"[^A-Za-z]", "", seg)) >= 4:
                frags.append(seg)
    return sorted(set(frags), key=len, reverse=True)[:8]


def find_print_site(message, folder, limit=3):
    """Lines in `folder` whose string literals contain the fixed parts of `message` (for errors that
    come from print/log statements rather than a stack trace)."""
    frags = _message_fragments(message)
    if not frags or not folder or not os.path.isdir(folder):
        return []
    hits = []
    for f, rel in _code_files(folder, "*.py,*.js,*.ts"):
        try:
            lines = open(f, encoding="utf-8", errors="replace").read().split("\n")
        except OSError:
            continue
        for i, l in enumerate(lines):
            if "print" not in l and "log" not in l and '"' not in l and "'" not in l:
                continue
            window = " ".join(lines[i:i + 3])
            n = sum(1 for fr in frags if fr in window)
            if n:
                hits.append((n, f, i + 1, lines))
    hits.sort(key=lambda h: -h[0])
    out, used = [], set()
    for n, f, ln, lines in hits:
        if (f, ln // 5) in used or n < max(1, min(2, len(frags))):
            continue
        used.add((f, ln // 5))
        s, e = max(1, ln - 4), min(len(lines), ln + 6)
        out.append({"path": f, "line": ln, "score": n,
                    "snippet": "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(s, e + 1))})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- core tool set + the rest on demand
@mcp.tool()
@guard
def more_tools(name: str = "", args: str = "{}") -> str:
    """Every other aws-error-feed tool (infrastructure, schedules, deploys, partner APIs, notes, mutes,
    reports, runs, history, system_health, project_priority, switch_model...). Call with no name for the
    list, then more_tools(name, args=JSON)."""
    if not name:
        rows = []
        for n, f in sorted(REGISTRY.items()):
            if n in EXPOSED:
                continue
            import inspect
            sig = ", ".join(p for p in inspect.signature(f).parameters)
            rows.append(f"- {n}({sig}): {((f.__doc__ or '').strip().splitlines() or [''])[0][:150]}")
        return "Call more_tools(name=\"<tool>\", args='{\"param\": value}'):\n" + "\n".join(rows)
    f = REGISTRY.get(name)
    if not f:
        return f"Unknown tool {name!r}. Call more_tools() for the list."
    try:
        kw = json.loads(args or "{}")
    except ValueError as ex:
        return f"args must be JSON: {ex}"
    try:
        return getattr(f, "__wrapped__", f)(**kw)
    except TypeError as ex:
        import inspect
        return f"Wrong arguments for {name}: {ex}. Signature: {name}{inspect.signature(f)}"


@tool()
@guard
def usage_savings(days: int = 7) -> str:
    """How much reading the local model (on the user's tower) did instead of Claude, per day:
    tokens it read, tokens it passed on, and everything this connector returned to Claude.
    Use when the user asks whether the local model is saving Claude usage."""
    r = api("/api/ai/savings", days=days)
    k = lambda n: f"{n / 1000:.1f}k" if n >= 1000 else str(n)
    tot = r["total"]
    out = [f"Savings, last {days} day(s) (tokens estimated as characters / {r['chars_per_token']}):",
           f"TOTAL: local model read ~{k(tot['local_read'])} tokens of logs/code ({tot['summaries']} summaries/reads, "
           f"{tot['triaged']} triaged patterns) and passed on ~{k(tot['local_wrote'])}; Claude received "
           f"~{k(tot['claude_received'])} from {tot['claude_calls']} tool calls -> ~{tot['saved_pct']}% of the "
           f"log reading happened locally (~{k(tot['avoided'])} tokens avoided)."]
    for d in r["days"]:
        tools = ", ".join(f"{t} ~{k(v)}" for t, v in d["tools"].items())
        out.append(f"- {d['day']}: local read ~{k(d['local_read'])}, Claude received ~{k(d['claude_received'])} "
                   f"({d['claude_calls']} calls), ~{d['saved_pct']}% saved" + (f". Biggest: {tools}" if tools else ""))
    out.append("Background triage counts as avoided only if Claude would otherwise have read those lines; "
               "summaries are a direct saving. Claude's own reasoning and code work aren't included.")
    return finish(out)


@tool()
@guard
def project_priority(profile: str = "", priority: str = "") -> str:
    """List or set project priority. priority: high (starred: first everywhere, triaged first),
    normal, or on_demand (not loaded/polled until someone opens it, then loaded for a couple of hours).
    No arguments = list all."""
    if profile and priority:
        r = api("/api/prefs/set", profile=profile, priority=priority)
        if r.get("error"):
            return r["error"]
    r = api("/api/prefs")
    out = [("Updated. " if profile and priority else "") + f"Project priority (on-demand stays loaded "
           f"{r['wake_hours']:g}h after it's opened):"]
    for x in r["profiles"]:
        st = ""
        if x["priority"] == "on_demand":
            st = " - asleep" if x["asleep"] else f" - loaded, {x['awake_minutes_left']} min left"
        out.append(f"- {_star(x['profile'], {x['profile']: x})}: {x['priority']}{st}")
    return finish(out)


@tool()
@guard
def switch_model(name: str = "", num_ctx: int = 0, download: bool = False) -> str:
    """The tower's local model: no arguments = list installed models and the one in use; name = switch to
    it (no restart; optional num_ctx e.g. 8192/16384); download=True = download that model first
    (runs in the background, check again with no arguments)."""
    if name and download:
        r = api("/api/ai/pull", name=name)
        return r.get("error") or r.get("message")
    if name:
        r = api("/api/ai/model", name=name, num_ctx=num_ctx or None)
        return r.get("error") or r.get("message")
    m = api("/api/ai/models")
    out = [f"In use: {m['current']} (context {m['num_ctx']}, set in {m['source']})" + (f" - {m['error']}" if m.get("error") else ""),
           "Installed: " + ", ".join(f"{x['name']} ({x['size_gb']} GB)" for x in m["installed"])]
    for n, p in (m.get("pulls") or {}).items():
        out.append(f"Download {n}: " + (f"failed - {p['error']}" if p.get("error") else "done" if p.get("done") else f"{p['status']} {p['pct']}%"))
    out.append("Compare models with system_health (day-by-day table records which model each day used).")
    return finish(out)


@tool()
@guard
def system_health() -> str:
    """Is the tower / local-model setup working? What to improve next, model speed (tokens/s, p95),
    queue waits, GPU fit, inputs cut by the context, triage quality (unclear / unbacked claims),
    cache hits, token savings per day with the settings active that day, Drain3 shadow grouping
    and the dashboard's own recent errors. Use when the user asks whether the setup/optimisations work."""
    try:
        with urllib.request.urlopen(f"{base()}/api/health/text", timeout=40) as r:
            return finish([r.read().decode("utf-8")])
    except urllib.error.HTTPError as ex:
        return f"The dashboard doesn't have the Health page yet (HTTP {ex.code}) - pull and restart it on the tower."


if __name__ == "__main__":
    mcp.run()
