#!/usr/bin/env python3
"""
AWS Error Feed — quick & dirty multi-account CloudWatch Logs watcher.

Pulls the log events from every AWS profile you have (~/.aws/config + credentials)
and sorts each line into a level:

    error     logged as an error (ERROR/CRITICAL/FATAL, tracebacks, Lambda crashes/timeouts)
    suspect   NOT logged as an error but looks like one, e.g. a try/except that just
              print()s "failed to ...", "could not ...", a 500 status, "ValueError: ..."
    warning   logged as a warning
    info      everything else your code prints / logs
    platform  Lambda START / END / REPORT lines

Tabs: Errors · Hidden errors? · All logs · Patterns (grouped) · Volume (per log group).
Pick a longer time range and older history is pulled from CloudWatch in the background.

    pip install boto3
    python aws_error_feed.py                 # all profiles, their default region
    python aws_error_feed.py -c config.json  # pick profiles/regions/log groups
    python aws_error_feed.py --list-profiles # show what it will watch

Opens http://127.0.0.1:8766. Click "Enable notifications" once for desktop pop-ups.
errors.db (SQLite, next to this script) is only a 24-hour cache: older ranges are pulled from
AWS when you pick them and deleted again 24 hours later. Deleting the file is always safe.

Needs only read access in each account: logs:DescribeLogGroups, logs:FilterLogEvents,
for Step Functions: states:ListStateMachines, states:ListExecutions, states:GetExecutionHistory,
for deploy tracking: lambda:ListFunctions, and for the infrastructure tabs (each optional):
lambda:ListLayerVersions, lambda:GetLayerVersion, scheduler:ListSchedules, scheduler:GetSchedule,
events:ListRules, events:ListTargetsByRule, secretsmanager:ListSecrets (metadata only - secret
values are never read), apigateway:GET, cloudwatch:GetMetricData, cloudwatch:DescribeAlarms.

Step Functions: failed / timed-out / aborted executions show under Errors (with the failing
state, error code and cause). Successful runs that only got through because a Retry or Catch
absorbed a failure, and executions stuck RUNNING too long, show under Hidden errors?.
Express workflows are covered through their CloudWatch log group (if logging is enabled).
"""
import argparse
import csv
import datetime
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:
    sys.exit("boto3 is required:  pip install boto3")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aws_infra  # noqa: E402  (infrastructure scan: schedules, layers, secrets, APIs, alarms)
import aws_ops  # noqa: E402    (runs, did-nothing, trace, code links, notes, reports, clients)
import aws_local  # noqa: E402  (optional local model via Ollama: triage + summaries)
DAY_MS = 86_400_000
LEVELS = ("error", "suspect", "warning", "info", "platform")

# --------------------------------------------------------------------------- config

# Used as a second, targeted pass when a log group is too busy to pull everything.
# CloudWatch filter syntax: terms are case-sensitive, ? = OR.
INTERESTING_PATTERN = (
    '?ERROR ?Error ?error ?CRITICAL ?FATAL ?Traceback ?Exception ?exception ?"Task timed out" '
    '?failed ?Failed ?FAILED ?failure ?Failure ?"could not" ?"Could not" ?"unable to" ?"Unable to" '
    '?cannot ?Cannot ?denied ?Denied ?refused ?timeout ?Timeout ?"timed out" ?invalid ?Invalid '
    '?WARN ?WARNING ?Warning ?warning ?retry ?Retry ?retrying ?Retrying'
)

DEFAULTS = {
    "profiles": [],              # [] = every profile found on this machine
    "exclude_profiles": [],
    "regions": [],               # [] = each profile's own default region (fallback us-east-1)
    "log_group_prefixes": [],    # [] = all log groups; e.g. ["/aws/lambda/", "/ecs/"]
    "exclude_log_group_substrings": [],
    "capture_all_logs": True,    # False = only pull lines matching filter_pattern
    "filter_pattern": INTERESTING_PATTERN,
    "skip_platform_lines": ["START", "END", "INIT_START"],   # Lambda noise; REPORT is kept
    # errors.db is a short-lived cache: anything older than this is deleted, unless you pulled
    # an older range in the last keep_hours (then it's kept until keep_hours after that pull)
    "keep_hours": 24,
    "poll_seconds": 30,
    "lookback_minutes": 60,      # how far back to look the very first time
    "group_refresh_minutes": 10,
    "max_groups_per_region": 300,
    "live_max_events_per_group": 10000,            # per poll; beyond this it's sampled
    "backfill_block_days": 3,
    "backfill_max_events_per_group_block": 5000,
    # Step Functions (Standard workflows, read from execution history)
    "step_functions": True,
    "sfn_inspect_succeeded": True,       # look inside successful runs for caught/retried failures
    "sfn_max_succeeded_per_poll": 25,    # per state machine, newest first
    "sfn_stuck_minutes": 60,             # flag RUNNING executions older than this
    "sfn_lookback_hours": 24,            # how long an execution may run and still be picked up
    "sfn_poll_seconds": 120,
    # Deploy tracking (lambda:ListFunctions). A deploy = the function's code fingerprint changed.
    "track_deploys": True,
    # Code links: folder holding your repos (default: the folder this dashboard folder sits in),
    # plus optional manual overrides in repos.json: {"function-name": "C:\\path\\to\\its\\folder"}
    "repos_root": None,
    "repos_file": "repos.json",
    # Extra regexes (one capture group = the number) for "N records processed" lines
    "record_count_patterns": [],
    # Optional local model (Ollama) that triages errors and summarises logs on your machine,
    # so Claude gets short answers instead of raw lines. Ignored if Ollama isn't running.
    "local_llm": {"enabled": True, "url": "http://localhost:11434", "model": "gpt-oss:20b",
                  "num_ctx": 16384, "triage_every_minutes": 5, "triage_per_cycle": 6, "triage_top_n": 8},
    # Infrastructure scan (schedules, Lambda config + layers, secrets metadata, API Gateway, alarms)
    "infra_scan": True,
    "infra_refresh_minutes": 15,
    "infra_scan_layer_packages": True,   # download each layer version once to list its packages
    "infra_max_layer_mb": 100,
    # Baseline for "new since yesterday" / spikes: hourly error counts kept this many days
    "baseline_days": 30,
    "seed_baseline_days": 7,      # one-time errors-only pull per account to start the baseline
    # Partner API health: service -> words that identify it in a log line (case-insensitive)
    "partner_services": {
        "HubSpot": ["hubapi", "hubspot"],
        "NetSuite": ["netsuite", "suitetalk", "restlet"],
        "Epicor": ["epicor"],
        "QuickBooks": ["quickbooks", "intuit", "qbo"],
        "Salesforce": ["salesforce", "force.com"],
        "Keap": ["keap", "infusionsoft"],
    },
    # extra words that identify a service in a *function name* only (e.g. cp-sync-company-2-hs)
    "partner_function_hints": {"HubSpot": ["-2-hs", "hs-", "-hs-", "_hs"], "NetSuite": ["ns-", "-2-ns"]},
    "deploy_check_seconds": 120,             # Step Functions APIs are rate-limited; check less often
    "db_file": "errors.db",
    "port": 8766,
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


# --------------------------------------------------------------------------- classification

EXC_LINE = re.compile(
    r"^\s*(?:\[\w+\]\s*)?([A-Za-z_][\w.$]*(?:Error|Exception|Exit|Interrupt|Fault|Timeout))\b:?\s*(.*)$"
)
EXC_ANYWHERE = re.compile(r"\b([A-Za-z_][\w.$]*[a-z](?:Exception|Error))\b")
LAMBDA_JSON_TYPE = re.compile(r'"errorType"\s*:\s*"([^"]+)"')
LAMBDA_JSON_MSG = re.compile(r'"errorMessage"\s*:\s*"((?:[^"\\]|\\.)*)"')
NOISE = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\b"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b(?:arn:aws[\w-]*:[^\s'\"]+)"), "<arn>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?"), "<n>"),
]
LEVEL_PREFIX = re.compile(
    r"^\s*(?:\[?(?:ERROR|CRITICAL|FATAL|WARN|WARNING|INFO|DEBUG)\]?(?::[\w.]+:)?[\s:|-]*)+")

PLATFORM_RX = re.compile(
    r"^(START|END|REPORT|INIT_START|INIT_REPORT|INIT_RUNTIME_DONE|EXTENSION|TELEMETRY|RESTORE_START|RESTORE_REPORT)\b"
    r"\s+(?:RequestId|Runtime|Name|Version|Duration|Init|Restore)")
ERROR_RX = re.compile(   # case-sensitive on purpose: "ERROR" is a log level, "error" is just a word
    r"\b(?:ERROR|CRITICAL|FATAL)\b|Traceback \(most recent call last\)|Task timed out|\"errorType\"\s*:")
ERROR_RX_CI = re.compile(
    r"[\"']?(?:level|severity|levelname|lvl)[\"']?\s*[:=]\s*[\"']?(?:error|err|critical|fatal|crit|alert|emerg)\b",
    re.I)
WARN_RX = re.compile(r"\b(?:WARN|WARNING)\b|\[warn(?:ing)?\]", re.I)
WARN_LEVEL_RX = re.compile(r"\b(?:WARN|WARNING)\b|\[(?:warn|warning)\]"
                           r"|[\"']?(?:level|severity|levelname)[\"']?\s*[:=]\s*[\"']?warn", re.I)
SUSPECT_RX = re.compile(
    r"\b(errors?|exceptions?|failed|failure|failing|fails?|traceback|stack ?trace|could ?n[o']t|"
    r"cannot|can't|unable to|invalid|denied|refused|forbidden|unauthori[sz]ed|timed? ?out|timeout|"
    r"retry(?:ing)?|crash(?:ed)?|abort(?:ed)?|panic|rollback|rolled back|not found|missing|"
    r"unavailable|bad gateway|internal server error|too many requests|rate[ -]?limit(?:ed)?|throttl\w*)\b",
    re.I)
STATUS_RX = re.compile(
    r"(?:status(?:_?code)?|statusCode|http_status)[\"']?\s*[:=]\s*[\"']?([45]\d\d)\b|HTTP/\d(?:\.\d)?\"?\s+([45]\d\d)\b",
    re.I)
BENIGN_RX = re.compile(
    r"[\"']?(?:errors?|error_?count|failures?|failed(?:_?count)?|exceptions?)[\"']?\s*[:=]\s*"
    r"(?:0|null|none|false|\[\]|\{\}|\"\"|'')(?![\w.])"
    r"|\b(?:no|0|zero|without|none)\s+(?:errors?|failures?|exceptions?)\b",
    re.I)


# Step Functions log lines (Express workflows, or Standard with logging on) are JSON with "type"
SFN_TYPE_RX = re.compile(r'"type"\s*:\s*"(\w*(?:Failed|TimedOut|Aborted))"')
SFN_ERRFIELD_RX = re.compile(r'"error"\s*:\s*"([^"]+)"')
SFN_CAUSE_RX = re.compile(r'"cause"\s*:\s*"((?:[^"\\]|\\.)*)"')


def classify_level(msg):
    """Return (level, hint). hint = the words that made a 'suspect' line look like an error."""
    m = SFN_TYPE_RX.search(msg)
    if m:
        if m.group(1).startswith("Execution"):
            return "error", ""
        return "suspect", m.group(1)        # a state failed; the workflow may have caught/retried it
    if PLATFORM_RX.match(msg):
        return "platform", ""
    if ERROR_RX.search(msg) or ERROR_RX_CI.search(msg):
        return "error", ""
    if WARN_LEVEL_RX.search(msg):
        return "warning", ""
    clean = BENIGN_RX.sub(" ", msg)
    hits = []
    for m in EXC_ANYWHERE.finditer(clean):
        hits.append(m.group(1).split(".")[-1])
    for m in SUSPECT_RX.finditer(clean):
        hits.append(m.group(1).lower())
    for m in STATUS_RX.finditer(clean):
        hits.append("HTTP " + (m.group(1) or m.group(2)))
    if hits:
        seen = []
        for h in hits:
            if h not in seen:
                seen.append(h)
        return "suspect", ", ".join(seen[:4])
    return "info", ""


def kind_of(msg, level):
    if level != "error":
        return level
    if "Traceback" in msg or "\tat " in msg or "stackTrace" in msg:
        return "traceback"
    if "Task timed out" in msg:
        return "timeout"
    if "CRITICAL" in msg or "FATAL" in msg:
        return "critical"
    return "error"


def normalize(text):
    """Strip the bits that change every time (ids, numbers, timestamps) so similar lines group."""
    for rx, rep in NOISE:
        text = rx.sub(rep, text)
    return re.sub(r"\s+", " ", text).strip()[:200]


def strip_prefix(line):
    """Drop Lambda/logging prefixes: '[INFO]\\t<ts>\\t<request-id>\\tmsg' -> 'msg'."""
    parts = line.split("\t")
    if len(parts) >= 3 and re.match(r"^\[?\w+\]?$|^\d{4}-\d\d-\d\d", parts[0].strip()):
        line = parts[-1]
    return LEVEL_PREFIX.sub("", line).strip()


def error_signature(msg):
    """(error_type, normalized message) for real errors."""
    if "Task timed out" in msg:
        return "LambdaTimeout", "Task timed out after <n> seconds"
    m = LAMBDA_JSON_TYPE.search(msg)
    if m:
        mm = LAMBDA_JSON_MSG.search(msg)
        return m.group(1), normalize(mm.group(1) if mm else "")
    lines = [l for l in msg.splitlines() if l.strip()]
    for line in reversed(lines):            # Python puts the real exception on the LAST line
        if line.lstrip().startswith(("File ", "at ", "Traceback")):
            continue
        m = EXC_LINE.match(strip_prefix(line)) or EXC_LINE.match(line)
        if m:
            return m.group(1).split(".")[-1], normalize(m.group(2))
    first = strip_prefix(lines[0] if lines else msg)
    m = EXC_ANYWHERE.search(first)
    if m:
        return m.group(1).split(".")[-1], normalize(first)
    level = "CRITICAL" if ("CRITICAL" in msg or "FATAL" in msg) else "ERROR"
    return level, normalize(first)


def signature(msg, level, hint):
    m = SFN_TYPE_RX.search(msg)
    if m:
        err, cause = SFN_ERRFIELD_RX.search(msg), SFN_CAUSE_RX.search(msg)
        return (err.group(1) if err else m.group(1)), normalize(
            m.group(1) + (": " + cause.group(1)[:150] if cause else ""))
    if level == "error":
        return error_signature(msg)
    lines = [l for l in msg.splitlines() if l.strip()]
    first = strip_prefix(lines[0] if lines else msg)
    if level == "platform":
        return first.split(" ")[0] if first else "PLATFORM", normalize(first)
    if level == "suspect":
        for line in reversed(lines):
            m = EXC_LINE.match(strip_prefix(line))
            if m:
                return m.group(1).split(".")[-1] + " (not logged as error)", normalize(m.group(2))
        return f'"{hint.split(", ")[0]}"' if hint else "SUSPECT", normalize(first)
    return level.upper(), normalize(first)


# --------------------------------------------------------------------------- store (SQLite)

COLS = ('seq, id, ts, profile, account, region, grp AS "group", stream, kind, etype, sig, '
        'message, live, level, hint')


SCHEMA_VERSION = 3      # 3 = 24-hour cache


def reset_old_db(path):
    """The database is only a cache now; an older (archive-style) one is simply replaced."""
    if not os.path.exists(path):
        return
    v = None
    c = sqlite3.connect(path)
    try:
        v = c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    except sqlite3.Error:
        pass
    finally:
        c.close()          # must close before deleting, or Windows keeps the file locked
    if v and int(v[0]) >= SCHEMA_VERSION:
        return
    try:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    except OSError:
        sys.exit(f"Couldn't replace {os.path.basename(path)} - is another copy of the feed still "
                 f"running? Stop it and start this again.")
    print(f"Replaced the old {os.path.basename(path)}; it's now a 24-hour cache that is "
          f"re-pulled from AWS as needed.")


class Store:
    def __init__(self, path, cfg):
        self.lock = threading.Lock()
        self.slock = threading.Lock()   # status has its own lock so it answers even mid-query
        self.status = {}
        self.skip_platform = set(cfg["skip_platform_lines"])
        reset_old_db(path)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(f"""
            PRAGMA auto_vacuum=INCREMENTAL;
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, ts INTEGER,
                profile TEXT, account TEXT, region TEXT, grp TEXT, stream TEXT,
                kind TEXT, etype TEXT, sig TEXT, message TEXT, live INTEGER, level TEXT, hint TEXT);
            CREATE TABLE IF NOT EXISTS coverage(
                key TEXT PRIMARY KEY, covered_from INTEGER, last_polled INTEGER, target INTEGER);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS pattern_hours(
                profile TEXT, grp TEXT, etype TEXT, sig TEXT, level TEXT, hour INTEGER, n INTEGER,
                PRIMARY KEY(profile, grp, etype, sig, hour));
            CREATE INDEX IF NOT EXISTS ix_ph_hour ON pattern_hours(hour);
            CREATE TABLE IF NOT EXISTS mutes(
                etype TEXT, sig TEXT, profile TEXT, status TEXT, at INTEGER, note TEXT,
                PRIMARY KEY(etype, sig, profile));
            CREATE TABLE IF NOT EXISTS infra_snap(key TEXT PRIMARY KEY, at INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS layer_pkgs(arn TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS deploys(
                profile TEXT, region TEXT, function TEXT, log_group TEXT, deployed_at INTEGER,
                code_sha TEXT, kind TEXT, label TEXT, runtime TEXT,
                PRIMARY KEY(profile, region, function, deployed_at));
            INSERT OR REPLACE INTO meta VALUES ('schema', '{SCHEMA_VERSION}');
        """)
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS ix_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS ix_lvl_ts ON events(level, ts);
            CREATE INDEX IF NOT EXISTS ix_prof_ts ON events(profile, ts);
            CREATE INDEX IF NOT EXISTS ix_grp_ts ON events(grp, ts);
            CREATE INDEX IF NOT EXISTS ix_sig ON events(etype, sig);
        """)

    # ---- writes
    def add_many(self, evs, live):
        rows = []
        for e in evs:
            msg = e["message"]
            if "level" in e:                 # pre-classified (Step Functions findings)
                level, hint, etype, sig, kind = e["level"], e["hint"], e["etype"], e["sig"], e["kind"]
            else:
                level, hint = classify_level(msg)
                if level == "platform" and msg.split(" ", 1)[0] in self.skip_platform:
                    continue
                etype, sig = signature(msg, level, hint)
                kind = kind_of(msg, level)
            rows.append((e["id"], e["ts"], e["profile"], e["account"], e["region"], e["group"],
                         e["stream"], kind, etype, sig,
                         msg[:20000] if level in ("error", "suspect") else msg[:4000],
                         1 if live else 0, level, hint))
        if not rows:
            return 0
        with self.lock:
            cur = self.db.executemany(
                "INSERT OR IGNORE INTO events(id,ts,profile,account,region,grp,stream,kind,etype,sig,"
                "message,live,level,hint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()
            return cur.rowcount

    def delete_older(self, cutoff, profile, region):
        """Drop one account/region's lines older than cutoff."""
        with self.lock:
            n = self.db.execute("DELETE FROM events WHERE profile=? AND region=? AND ts < ?",
                                (profile, region, cutoff)).rowcount
            self.db.commit()
        return n

    def delete_orphans(self, cutoff, keys):
        """Drop old lines from accounts that are no longer watched."""
        with self.lock:
            q = "DELETE FROM events WHERE ts < ?"
            if keys:
                q += f" AND profile || '/' || region NOT IN ({','.join('?' * len(keys))})"
            n = self.db.execute(q, [cutoff] + list(keys)).rowcount
            self.db.commit()
        return n

    def shrink(self):
        """Give freed space back to the disk."""
        with self.lock:
            self.db.execute("PRAGMA incremental_vacuum")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.commit()

    def get_coverage(self, key):
        with self.lock:
            r = self.db.execute("SELECT * FROM coverage WHERE key=?", (key,)).fetchone()
            return dict(r) if r else None

    def set_coverage(self, key, covered_from, last_polled, target):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?)",
                            (key, covered_from, last_polled, target))
            self.db.commit()

    # ---- reads
    @staticmethod
    def where(f):
        c, p = [], []
        if f.get("hours"):
            c.append("ts >= ?")
            p.append(int((time.time() - float(f["hours"]) * 3600) * 1000))
        for k, col in (("profile", "profile"), ("kind", "kind"), ("grp", "grp"), ("stream", "stream")):
            if f.get(k):
                c.append(f"{col} = ?")
                p.append(f[k])
        if f.get("grp_prefix"):
            esc = f["grp_prefix"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            c.append("grp LIKE ? ESCAPE '\\'")
            p.append(esc + "%")
        if f.get("since_ms"):
            c.append("ts >= ?")
            p.append(int(f["since_ms"]))
        if f.get("until_ms"):
            c.append("ts <= ?")
            p.append(int(f["until_ms"]))
        if f.get("level"):
            lv = [l for l in f["level"].split(",") if l in LEVELS]
            if lv:
                c.append(f"level IN ({','.join('?' * len(lv))})")
                p += lv
        if f.get("etype"):
            c.append("etype = ? AND sig = ?")
            p += [f["etype"], f.get("sig", "")]
        if f.get("q"):
            c.append("(message LIKE ? OR grp LIKE ? OR etype LIKE ?)")
            p += [f"%{f['q']}%"] * 3
        if f.get("hide_muted") and f["hide_muted"] not in ("0", "false"):
            # muted = always hidden; marked fixed = hide only the lines from before it was fixed
            c.append("NOT EXISTS (SELECT 1 FROM mutes m WHERE m.etype = events.etype AND m.sig = events.sig "
                     "AND (m.profile = '' OR m.profile = events.profile) "
                     "AND (m.status = 'muted' OR events.ts <= m.at))")
        return (" WHERE " + " AND ".join(c)) if c else "", p

    def context(self, event_id, before_ms, after_ms, limit=300):
        """The line plus what the same log stream printed just before and after it."""
        with self.lock:
            r = self.db.execute(f"SELECT {COLS} FROM events WHERE id = ?", (event_id,)).fetchone()
            if not r:
                return None, []
            r = dict(r)
            rows = self.db.execute(
                f"SELECT {COLS} FROM events WHERE grp = ? AND stream = ? AND ts BETWEEN ? AND ? "
                f"ORDER BY ts, seq LIMIT ?",
                (r["group"], r["stream"], r["ts"] - before_ms, r["ts"] + after_ms, limit)).fetchall()
        return r, [dict(x) for x in rows]

    def list_events(self, f, limit=500):
        w, p = self.where(f)
        with self.lock:
            total = self.db.execute(f"SELECT COUNT(*) FROM events{w}", p).fetchone()[0]
            rows = self.db.execute(f"SELECT {COLS} FROM events{w} ORDER BY ts DESC LIMIT ?",
                                   p + [limit]).fetchall()
        return [dict(r) for r in rows], total

    def all_events(self, f):
        w, p = self.where(f)
        with self.lock:
            return [dict(r) for r in self.db.execute(
                f"SELECT {COLS} FROM events{w} ORDER BY ts LIMIT 500000", p)]

    def new_since(self, seq, levels):
        with self.lock:
            top = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            live = []
            if seq and levels:
                fresh = int((time.time() - 900) * 1000)   # only notify for lines < 15 min old
                rows = [dict(r) for r in self.db.execute(
                    f"SELECT {COLS} FROM events WHERE seq > ? AND live = 1 AND ts >= ? "
                    f"AND level IN ('error','suspect') ORDER BY ts DESC LIMIT 200", [seq, fresh])]
                mutes = self._mutes_locked()
                for r in rows:
                    m = mutes.get((r["etype"], r["sig"], r["profile"])) or mutes.get((r["etype"], r["sig"], ""))
                    if m and (m["status"] == "muted" or r["ts"] <= m["at"]):
                        continue                         # muted noise: no pop-up
                    r["regression"] = bool(m)            # marked fixed, but it's back
                    if r["level"] in levels or r["regression"]:
                        live.append(r)
                live = live[:50]
        return {"seq": top, "changed": top > seq, "live": live}

    # ---- mutes (kept forever)
    def _mutes_locked(self):
        return {(r["etype"], r["sig"], r["profile"]): dict(r) for r in self.db.execute("SELECT * FROM mutes")}

    def set_mute(self, etype, sig, profile, status, note=""):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO mutes VALUES (?,?,?,?,?,?)",
                            (etype, sig, profile or "", status, int(time.time() * 1000), note or ""))
            self.db.commit()

    def unmute(self, etype, sig, profile):
        with self.lock:
            n = self.db.execute("DELETE FROM mutes WHERE etype=? AND sig=? AND profile=?",
                                (etype, sig, profile or "")).rowcount
            self.db.commit()
        return n

    def list_mutes(self):
        """Every mute / fixed mark, with when it was last seen and whether a fixed one came back."""
        with self.lock:
            out = []
            for m in self.db.execute("SELECT * FROM mutes ORDER BY at DESC").fetchall():
                m = dict(m)
                pf, pp = ("", []) if not m["profile"] else (" AND profile = ?", [m["profile"]])
                last = self.db.execute(f"SELECT MAX(ts), COUNT(*) FROM events WHERE etype=? AND sig=?{pf} AND ts > ?",
                                       [m["etype"], m["sig"]] + pp + [m["at"]]).fetchone()
                lh = self.db.execute(f"SELECT MAX(hour), COALESCE(SUM(n),0) FROM pattern_hours WHERE etype=? AND sig=?{pf} "
                                     f"AND hour > ?", [m["etype"], m["sig"]] + pp + [m["at"] // 3_600_000]).fetchone()
                m["since_count"] = max(last[1] or 0, lh[1] or 0)
                m["last_seen"] = max(last[0] or 0, (lh[0] or 0) * 3_600_000)
                m["regression"] = m["status"] == "fixed" and m["since_count"] > 0
                out.append(m)
        return out

    # ---- hourly baseline for "new" / "spike"
    def rollup(self, since_ms, until_ms, profile=None, region=None):
        """Fold error/hidden-error counts into hourly buckets (kept beyond the 24h cache).
        Counts only ever grow (MAX), so re-running over partial data never loses anything."""
        extra, p = "", [since_ms, until_ms]
        if profile:
            extra, p = " AND profile = ? AND region = ?", p + [profile, region]
        with self.lock:
            self.db.execute(
                "INSERT INTO pattern_hours (profile, grp, etype, sig, level, hour, n) "
                "SELECT profile, grp, etype, sig, MAX(level), ts / 3600000, COUNT(*) FROM events "
                f"WHERE level IN ('error','suspect') AND ts BETWEEN ? AND ?{extra} "
                "GROUP BY profile, grp, etype, sig, ts / 3600000 "
                "ON CONFLICT(profile, grp, etype, sig, hour) DO UPDATE SET n = MAX(n, excluded.n)", p)
            self.db.commit()

    def prune_baseline(self, days):
        with self.lock:
            self.db.execute("DELETE FROM pattern_hours WHERE hour < ?",
                            (int(time.time() // 3600) - days * 24,))
            self.db.commit()

    def trends(self, profile=None):
        """Per pattern: first hour ever seen, count in the last 24h, and the 7 days before that."""
        now_h = int(time.time() // 3600)
        pf, pp = ("", []) if not profile else (" WHERE profile = ?", [profile])
        with self.lock:
            rows = self.db.execute(
                "SELECT etype, sig, MIN(hour), "
                "SUM(CASE WHEN hour > ? THEN n ELSE 0 END), "
                f"SUM(CASE WHEN hour <= ? AND hour > ? THEN n ELSE 0 END) FROM pattern_hours{pf} "
                "GROUP BY etype, sig", [now_h - 24, now_h - 24, now_h - 24 * 8] + pp).fetchall()
            oldest = self.db.execute(f"SELECT MIN(hour) FROM pattern_hours{pf}", pp).fetchone()[0]
        baseline_h = (now_h - oldest) if oldest is not None else 0
        out = {}
        for et, sg, first_h, last24, prev7 in rows:
            avg = (prev7 or 0) / 7
            new = first_h > now_h - 24 and baseline_h >= 48
            spike = (not new) and last24 >= 10 and last24 >= 3 * max(avg, 1)
            out[(et, sg)] = {"first_hour": first_h, "last24": last24 or 0, "prev7_daily_avg": round(avg, 1),
                             "is_new": new, "is_spike": spike,
                             "spike_ratio": round(last24 / max(avg, 0.5), 1) if spike else None}
        return out, baseline_h

    def annotate(self, rows, profile=None):
        """Add new/spike/mute info to summary rows."""
        tr, baseline_h = self.trends(profile)
        with self.lock:
            mutes = self._mutes_locked()
        for g in rows:
            g.update(tr.get((g["etype"], g["sig"]), {"is_new": False, "is_spike": False}))
            g["baseline_hours"] = baseline_h
            m = mutes.get((g["etype"], g["sig"], profile or "")) or mutes.get((g["etype"], g["sig"], ""))
            if not m and not profile:
                m = next((v for k, v in mutes.items() if k[0] == g["etype"] and k[1] == g["sig"]), None)
            g["mute"] = m["status"] if m else None
            g["regression"] = bool(m and m["status"] == "fixed" and g["last_seen"] > m["at"])
        return aws_local.attach_triage(self, aws_ops.attach_notes(self, rows, profile), profile)

    def pattern_totals(self, profile, since_ms, until_ms):
        """Error / hidden-error counts per pattern from the hourly baseline (kept 30 days)."""
        with self.lock:
            return {(r[0], r[1]): {"level": r[2], "n": r[3], "first": r[4] * 3_600_000, "last": r[5] * 3_600_000 + 3_599_999}
                    for r in self.db.execute(
                        "SELECT etype, sig, MAX(level), SUM(n), MIN(hour), MAX(hour) FROM pattern_hours "
                        "WHERE profile = ? AND hour >= ? AND hour <= ? GROUP BY etype, sig",
                        (profile, since_ms // 3_600_000, until_ms // 3_600_000))}

    def summary(self, f):
        w, p = self.where(f)
        with self.lock:
            main = self.db.execute(
                f"SELECT etype, sig, COUNT(*) AS count, MAX(ts) AS last_seen, "
                f"substr(message,1,2000) AS sample, kind, level, hint FROM events{w} "
                f"GROUP BY etype, sig ORDER BY count DESC LIMIT 2000", p).fetchall()
            first = {(r[0], r[1]): r[2] for r in self.db.execute(
                f"SELECT etype, sig, MIN(ts) FROM events{w} GROUP BY etype, sig", p)}
            per = {}
            for col, name in (("profile", "profiles"), ("grp", "log_groups")):
                for r in self.db.execute(
                        f"SELECT etype, sig, {col}, COUNT(*) FROM events{w} "
                        f"GROUP BY etype, sig, {col} ORDER BY COUNT(*) DESC", p):
                    per.setdefault((r[0], r[1]), {}).setdefault(name, {})[r[2]] = r[3]
        out = []
        for r in main:
            k = (r["etype"], r["sig"])
            out.append({**dict(r), "first_seen": first.get(k, r["last_seen"]),
                        "profiles": per.get(k, {}).get("profiles", {}),
                        "log_groups": per.get(k, {}).get("log_groups", {})})
        return out

    def volume(self, f):
        f = {k: v for k, v in f.items() if k != "level"}
        w, p = self.where(f)
        rows = {}
        with self.lock:
            for r in self.db.execute(
                    f"SELECT profile, grp, level, COUNT(*), MAX(ts) FROM events{w} "
                    f"GROUP BY profile, grp, level", p):
                g = rows.setdefault((r[0], r[1]), {"profile": r[0], "group": r[1], "total": 0,
                                                   "last": 0, **{l: 0 for l in LEVELS}})
                g[r[2]] = r[3]
                g["total"] += r[3]
                g["last"] = max(g["last"], r[4])
        return sorted(rows.values(), key=lambda g: -g["total"])

    # ---- infrastructure snapshots (latest per account/region) and layer package lists
    def save_infra(self, key, snap):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO infra_snap VALUES (?,?,?)",
                            (key, snap.get("at", 0), json.dumps(snap)))
            self.db.commit()

    def get_infra(self, key):
        with self.lock:
            r = self.db.execute("SELECT data FROM infra_snap WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def save_layer_pkgs(self, arn, pkgs):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO layer_pkgs VALUES (?,?)", (arn, json.dumps(pkgs)))
            self.db.commit()

    def get_layer_pkgs(self, arn):
        with self.lock:
            r = self.db.execute("SELECT data FROM layer_pkgs WHERE arn = ?", (arn,)).fetchone()
        return json.loads(r[0]) if r else None

    # ---- deploys (kept forever; tiny)
    def record_functions(self, profile, region, funcs):
        """Compare each function with what we last saw; record new code or config deploys."""
        new = []
        with self.lock:
            for f in funcs:
                last = self.db.execute(
                    "SELECT deployed_at, code_sha, label FROM deploys WHERE profile=? AND region=? "
                    "AND function=? ORDER BY deployed_at DESC LIMIT 1",
                    (profile, region, f["function"])).fetchone()
                kind = None
                if not last:
                    kind = "code"                      # first time we see it: its current deploy
                elif f["sha"] != last["code_sha"]:
                    kind = "code"
                elif f["last_modified"] - last["deployed_at"] > 5 * 60_000:
                    kind = "config"                    # settings/env changed, same code
                elif f["label"] and f["label"] != last["label"]:
                    self.db.execute("UPDATE deploys SET label=? WHERE profile=? AND region=? AND "
                                    "function=? AND deployed_at=?",
                                    (f["label"], profile, region, f["function"], last["deployed_at"]))
                if kind:
                    self.db.execute("INSERT OR IGNORE INTO deploys VALUES (?,?,?,?,?,?,?,?,?)",
                                    (profile, region, f["function"], f["log_group"], f["last_modified"],
                                     f["sha"], kind, f["label"], f["runtime"]))
                    if last:
                        new.append({**f, "kind": kind})
            self.db.commit()
        return new

    def list_deploys(self, profile=None, since_ms=0, function=None, limit=500):
        q, p = "SELECT * FROM deploys WHERE deployed_at >= ?", [since_ms]
        if profile:
            q += " AND profile = ?"
            p.append(profile)
        if function:
            q += " AND function = ?"
            p.append(function)
        with self.lock:
            return [dict(r) for r in self.db.execute(q + " ORDER BY deployed_at DESC LIMIT ?", p + [limit])]

    def level_counts(self, f):
        w, p = self.where(f)
        with self.lock:
            out = {l: 0 for l in LEVELS}
            for r in self.db.execute(f"SELECT level, COUNT(*) FROM events{w} GROUP BY level", p):
                out[r[0]] = r[1]
            w2 = (w + " AND " if w else " WHERE ") + "level = 'platform' AND message LIKE 'REPORT%'"
            out["invocations"] = self.db.execute(f"SELECT COUNT(*) FROM events{w2}", p).fetchone()[0]
        return out

    def set_status(self, key, **kw):
        with self.slock:
            self.status.setdefault(key, {}).update(kw, updated=time.time())

    def get_status(self):
        with self.slock:
            return json.loads(json.dumps(self.status))


def _ctx_code(code):
    return (rf"(?:\b(?:status(?:_?code)?|http(?:/\d(?:\.\d)?)?|code|response|error)\b\W{{0,12}}{code}\b"
            rf"|\b{code}\s+(?:client|server)\s+error|\bHTTP {code}\b)")


PARTNER_PROBLEMS = [
    ("auth", re.compile(_ctx_code("401") + r"|unauthori[sz]ed|invalid[_ ]grant|token (?:has )?expired|expired[_ ]token"
                        r"|invalid[_ ](?:access[_ ])?token|INVALID_LOGIN|authentication (?:failed|required)|invalid_client", re.I)),
    ("permission", re.compile(_ctx_code("403") + r"|forbidden|MISSING_SCOPES|insufficient (?:permissions?|scopes?)"
                              r"|not authorized to|permission denied", re.I)),
    ("rate limit", re.compile(_ctx_code("429") + r"|too many requests|rate[ _-]?limit|throttl|SECONDLY"
                              r"|concurrency limit|request limit", re.I)),
    ("server error", re.compile(_ctx_code("50[0-4]") + r"|internal server error|bad gateway|service unavailable"
                                r"|gateway time-?out", re.I)),
    ("timeout", re.compile(r"timed? ?out|ReadTimeout|ConnectTimeout|connection (?:reset|refused|aborted)"
                           r"|RemoteDisconnected", re.I)),
]


def partner_health(store, f, services, hints=None):
    """Group error / hidden-error lines by partner service and kind of API problem."""
    kw = {svc: [w.lower() for w in words] for svc, words in services.items()}
    name_kw = {svc: words + [h.lower() for h in (hints or {}).get(svc, [])] for svc, words in kw.items()}
    w, p = store.where({**f, "level": "error,suspect"})
    with store.lock:
        rows = store.db.execute(f"SELECT id, ts, profile, grp, message FROM events{w} ORDER BY ts DESC LIMIT 200000",
                                p).fetchall()
    now = time.time() * 1000
    out = {}
    for rid, ts, prof, grp, msg in rows:
        low = msg.lower()
        for prob, rx in PARTNER_PROBLEMS:
            m = rx.search(msg)
            if not m:
                continue
            # the service named closest to the problem in the message; else from the function name
            hits = []
            for svc, words in kw.items():
                pos = [j for j in (low.find(x) for x in words) if j >= 0]
                if pos:
                    hits.append((min(abs(j - m.start()) for j in pos), svc))
            if hits:
                svc = min(hits)[1]
            elif prob in ("timeout", "server error"):
                continue      # e.g. a Lambda timeout - only count these when a partner is named
            else:
                in_name = [svc for svc, words in name_kw.items() if any(x in grp.lower() for x in words)]
                svc = in_name[0] if len(in_name) == 1 else ("/".join(in_name) + "?" if in_name else "Other")
            e = out.setdefault(svc, {}).setdefault(prob, {
                "count": 0, "last_hour": 0, "profiles": {}, "functions": {}, "first": ts, "last": ts,
                "sample": msg[:1500], "sample_id": rid, "recent_profiles": set()})
            e["count"] += 1
            e["profiles"][prof] = e["profiles"].get(prof, 0) + 1
            e["functions"][grp] = e["functions"].get(grp, 0) + 1
            e["first"], e["last"] = min(e["first"], ts), max(e["last"], ts)
            if ts > now - 3_600_000:
                e["last_hour"] += 1
                e["recent_profiles"].add(prof)
            break                                   # one problem per line
    result = []
    for svc, probs in out.items():
        for prob, e in probs.items():
            e["several_clients_now"] = len(e.pop("recent_profiles")) >= 2
            e["profiles"] = dict(sorted(e["profiles"].items(), key=lambda x: -x[1]))
            e["functions"] = dict(sorted(e["functions"].items(), key=lambda x: -x[1])[:10])
            result.append({"service": svc, "problem": prob, **e})
    result.sort(key=lambda r: (not r["several_clients_now"], -r["last_hour"], -r["count"]))
    return result


def sfn_run_input(client, execution_arn, max_failures=3):
    """Inputs needed to reproduce a Step Functions failure, read from its execution history."""
    events = []
    for page in client.get_paginator("get_execution_history").paginate(
            executionArn=execution_arn, includeExecutionData=True,
            PaginationConfig={"PageSize": 1000, "MaxItems": 5000}):
        events += page.get("events", [])
    events.sort(key=lambda e: e["id"])

    def pretty(text, n=20000):
        if text is None:
            return None
        try:
            text = json.dumps(json.loads(text), indent=1)
        except (ValueError, TypeError):
            pass
        return text if len(text) <= n else text[:n] + f"\n... [{len(text) - n} more chars]"

    run_input, state, state_input, payload, failures = None, None, None, None, []
    for ev in events:
        t = ev["type"]
        if t == "ExecutionStarted":
            run_input = ev.get("executionStartedEventDetails", {}).get("input")
        elif t.endswith("StateEntered"):
            d = ev.get("stateEnteredEventDetails", {})
            state, state_input, payload = d.get("name"), d.get("input"), None
        elif t == "LambdaFunctionScheduled":
            d = ev.get("lambdaFunctionScheduledEventDetails", {})
            payload = {"resource": d.get("resource"), "payload": d.get("input")}
        elif t == "TaskScheduled":
            d = ev.get("taskScheduledEventDetails", {})
            params = d.get("parameters")
            try:
                pj = json.loads(params)
                payload = {"resource": pj.get("FunctionName") or d.get("resource"),
                           "payload": json.dumps(pj.get("Payload", pj))}
            except (ValueError, TypeError, AttributeError):
                payload = {"resource": d.get("resource"), "payload": params}
        elif (t.endswith(("Failed", "TimedOut")) and not t.startswith("Execution")) and len(failures) < max_failures:
            det = _details(ev)
            failures.append({"state": state, "event": t, "error": det.get("error"), "cause": (det.get("cause") or "")[:3000],
                             "state_input": pretty(state_input),
                             "lambda": (payload or {}).get("resource"),
                             "lambda_payload": pretty((payload or {}).get("payload"))})
    return {"execution_arn": execution_arn, "run_input": pretty(run_input), "failures": failures}


def deploy_impact(store, d, window_hours=24):
    """Errors / hidden errors for a function in equal windows before and after a deploy."""
    now = int(time.time() * 1000)
    t0, W = d["deployed_at"], int(window_hours * 3_600_000)
    after_end = min(now, t0 + W)
    base = {"profile": d["profile"], "grp": d["log_group"]}
    fb = {**base, "since_ms": t0 - W, "until_ms": t0 - 1}
    fa = {**base, "since_ms": t0, "until_ms": after_end}
    cb, ca = store.level_counts(fb), store.level_counts(fa)
    pb = {(g["etype"], g["sig"]): g for g in store.summary({**fb, "level": "error,suspect"})}
    pa = {(g["etype"], g["sig"]): g for g in store.summary({**fa, "level": "error,suspect"})}
    pats = []
    for k in set(pb) | set(pa):
        g = pa.get(k) or pb.get(k)
        nb, na = pb[k]["count"] if k in pb else 0, pa[k]["count"] if k in pa else 0
        pats.append({"etype": k[0], "sig": k[1], "level": g["level"], "before": nb, "after": na,
                     "status": "new" if not nb else "gone" if not na else "still",
                     "sample": (pa.get(k) or pb.get(k))["sample"][:1500]})
    pats.sort(key=lambda x: ({"new": 0, "still": 1, "gone": 2}[x["status"]], -(x["after"] + x["before"])))
    after_h = (after_end - t0) / 3_600_000

    def rate(c, hours):
        bad = c["error"] + c["suspect"]
        return bad / c["invocations"] if c["invocations"] else bad / max(hours, 1e-9)
    per = "invocation" if cb["invocations"] and ca["invocations"] else "hour"
    rb, ra = rate(cb, window_hours), rate(ca, after_h)
    new = sum(1 for x in pats if x["status"] == "new")
    gone = sum(1 for x in pats if x["status"] == "gone")
    eb, ea = cb["error"] + cb["suspect"], ca["error"] + ca["suspect"]
    if after_h < 0.25 and ca["invocations"] < 5:
        verdict = "too early"
    elif new and ea:
        verdict = "new errors"
    elif eb and not ea:
        verdict = "fixed"
    elif not eb and not ea:
        verdict = "clean"
    elif ra < rb * 0.5:
        verdict = "better"
    elif ra > rb * 1.5:
        verdict = "worse"
    else:
        verdict = "no change"
    return {"deploy": d, "window_hours": window_hours, "after_hours": round(after_h, 2),
            "before": cb, "after": ca, "rate_per": per, "rate_before": rb, "rate_after": ra,
            "new_patterns": new, "gone_patterns": gone, "verdict": verdict, "patterns": pats}


VERSION_DESC_RX = re.compile(r"\b(?:git|commit|sha|version|release|v)[\s:=#]*([0-9a-f]{7,40}|v?\d[\w.\-]{0,38})\b", re.I)
VERSION_ENV_KEYS = ("GIT_SHA", "GIT_COMMIT", "COMMIT_SHA", "CODE_VERSION", "APP_VERSION", "VERSION",
                    "RELEASE", "BUILD_ID", "DEPLOY_VERSION")


def infra_views(store, workers, profile=None):
    """(worker, snapshot, REPORT stats) for each account/region, optionally one profile."""
    now = int(time.time() * 1000)
    out = []
    for w in workers:
        if profile and w.profile != profile:
            continue
        snap = store.get_infra(w.key) or {}
        stats = aws_infra.report_stats(store, w.profile, w.region, now - DAY_MS)
        out.append((w, snap, stats))
    return out


def tag(rows, w):
    for r in rows:
        r["profile"], r["region"] = w.profile, w.region
    return rows


def build_digest(store, workers, cfg, profile=None):
    """Per-client morning summary: what needs attention today."""
    now = int(time.time() * 1000)
    out = []
    by_profile = {}
    for w, snap, stats in infra_views(store, workers, profile):
        by_profile.setdefault(w.profile, []).append((w, snap, stats))
    for prof, views in sorted(by_profile.items()):
        sec = []

        def add(title, items):
            if items:
                sec.append({"title": title, "items": items[:8] + ([f"... and {len(items) - 8} more"] if len(items) > 8 else [])})
        sched, fns, apis, alarms, secrets, cost = [], [], [], [], [], 0.0
        for w, snap, stats in views:
            sched += [r for r in aws_infra.schedule_health(snap, stats, now, w.covered_from)
                      if r["status"] in ("not running", "missed runs", "target missing", "last run failed")]
            fh = aws_infra.function_health(snap, stats, now)
            fns += fh
            cost += sum(f["cost_24h"] for f in fh)
            apis += [a for a in aws_infra.api_report(snap) if a["e5"]]
            alarms += [a for a in snap.get("alarms", []) if a["state"] == "ALARM"]
        add("Scheduled jobs that didn't run properly",
            [f"{r['name']} ({r['expression']}) -> {r['function'] or r['state_machine'] or r['target']}: {r['status']}. {r['detail']}"
             for r in sched])
        pats = store.annotate(store.summary({"hours": 24, "profile": prof, "level": "error,suspect", "hide_muted": "1"}), prof)
        add("Regressions (marked fixed, came back)", [f"{g['etype']}: {g['sig'][:120]} ({g['count']}x)" for g in pats if g.get("regression")])
        add("New since yesterday", [f"{g['etype']}: {g['sig'][:120]} ({g['count']}x, {', '.join(list(g['log_groups'])[:2])})"
                                    for g in pats if g.get("is_new")])
        add("Spiking", [f"{g['etype']}: {g['sig'][:120]} ({g['last24']} today vs {g['prev7_daily_avg']}/day)"
                        for g in pats if g.get("is_spike") and not g.get("below_threshold")])
        nothing = [d for w, snap, stats in views for d in aws_ops.did_nothing(store, snap, w.profile, w.region, now, EXTRA_RX)]
        add("Ran but did nothing / went quiet", [f"{aws_ops.function_from_group(d['group']) or d['group']}: {d['detail']}"
                                                 for d in nothing])
        tot_e = sum(g["count"] for g in pats if g["level"] == "error")
        tot_s = sum(g["count"] for g in pats if g["level"] == "suspect")
        add("Top errors (24h)", [f"{g['etype']}: {g['sig'][:120]} - {g['count']}x" for g in pats[:3]])
        deps = []
        for d in store.list_deploys(prof, now - DAY_MS):
            imp = deploy_impact(store, d, 24)
            deps.append(f"{d['function']} {d['kind']}{' ' + d['label'] if d['label'] else ''} at {iso(d['deployed_at'])}: {imp['verdict']}")
        add("Deploys in the last 24h", deps)
        add("Partner API problems", [f"{p['service']} {p['problem']}: {p['count']} lines ({p['last_hour']} in last hour)"
                                     + (" - several clients" if p["several_clients_now"] else "")
                                     for p in partner_health(store, {"hours": 24, "profile": prof}, cfg["partner_services"],
                                                             cfg.get("partner_function_hints"))])
        add("CloudWatch alarms firing", [f"{a['name']}: {a['reason'][:150]}" for a in alarms])
        add("API Gateway server errors", [f"{a['api']} ({a['stage']}): {a['e5']} 5xx of {a['requests']} requests" for a in apis])
        add("Functions needing attention", [f"{f['name']}: {', '.join(x for x in f['flags'] if 'runtime' not in x and 'layer' not in x)}"
                                            for f in fns if any('runtime' not in x and 'layer' not in x for x in f['flags'])])
        dep_rt = sorted({f["runtime"] for f in fns if f["runtime_status"] == "deprecated"})
        n_old = sum(1 for f in fns if f["runtime_status"] in ("deprecated", "soon"))
        n_lay = sum(1 for f in fns if f["outdated_layers"])
        if n_old or n_lay:
            add("Upgrade backlog", ([f"{n_old} functions on deprecated / soon-deprecated runtimes ({', '.join(dep_rt)})"] if n_old else [])
                + ([f"{n_lay} functions on an outdated layer version"] if n_lay else []))
        out.append({"profile": prof, "errors_24h": tot_e, "hidden_24h": tot_s, "cost_24h": round(cost, 2),
                    "ok": not sec or all(x["title"] in ("Top errors (24h)", "Upgrade backlog", "Deploys in the last 24h") for x in sec),
                    "sections": sec})
    out.sort(key=lambda d: (d["ok"], -len(d["sections"])))
    return out


def digest_text(dg):
    lines = [f"AWS daily digest - {time.strftime('%a %d %b %Y %H:%M')}", ""]
    quiet = [d["profile"] for d in dg if d["ok"] and not d["sections"]]
    for d in dg:
        if d["profile"] in quiet:
            continue
        lines.append(f"## {d['profile']} - {d['errors_24h']} errors, {d['hidden_24h']} hidden errors, ~${d['cost_24h']} Lambda cost (24h)")
        for s in d["sections"]:
            lines.append(f"* {s['title']}:")
            lines += [f"    - {i}" for i in s["items"]]
        lines.append("")
    if quiet:
        lines.append("All quiet: " + ", ".join(quiet))
    return "\n".join(lines)


EXTRA_RX = []
_clients_cache = {"at": 0, "data": None}


def ops_helpers(store, workers, cfg):
    now = int(time.time() * 1000)

    def schedules(profile):
        return [r for w, snap, st in infra_views(store, workers, profile)
                for r in aws_infra.schedule_health(snap, st, now, w.covered_from)]

    def functions(profile):
        return [r for w, snap, st in infra_views(store, workers, profile) for r in aws_infra.function_health(snap, st, now)]

    def deploys(profile, since):
        out = []
        for d in store.list_deploys(profile, since):
            out.append({**d, "verdict": deploy_impact(store, d, 24)["verdict"]})
        return out

    return {"infra": store.get_infra, "pattern_totals": store.pattern_totals, "mutes": store.list_mutes,
            "schedules": schedules, "functions": functions, "deploys": deploys,
            "partners": lambda p: partner_health(store, {"hours": 24, "profile": p}, cfg["partner_services"],
                                                 cfg.get("partner_function_hints"))}


def level_counts_window(store, profile, start, end):
    with store.lock:
        rows = store.db.execute("SELECT level, COUNT(*) FROM events WHERE profile = ? AND ts >= ? AND ts < ? "
                                "AND level IN ('error','suspect') GROUP BY level", (profile, start, end)).fetchall()
    d = dict(rows)
    return {"errors": d.get("error", 0), "hidden": d.get("suspect", 0)}


def error_windows(store, profile, data_from, now):
    """Errors / hidden errors in the last 1h, 6h, 24h, each with the window just before it.
    Uses the loaded lines when they cover the window, else the hourly counts (kept 30 days)."""
    out = {}
    for label, hrs in (("1h", 1), ("6h", 6), ("24h", 24)):
        w = hrs * 3_600_000
        cur = level_counts_window(store, profile, now - w, now)
        if now - 2 * w >= data_from:
            prev = level_counts_window(store, profile, now - 2 * w, now - w)
        else:
            pt = store.pattern_totals(profile, now - 2 * w, now - w - 1)
            prev = ({"errors": sum(v["n"] for v in pt.values() if v["level"] == "error"),
                     "hidden": sum(v["n"] for v in pt.values() if v["level"] == "suspect")} if pt else None)
        out[label] = {**cur, "prev_errors": prev["errors"] if prev else None,
                      "prev_hidden": prev["hidden"] if prev else None}
    return out


def clients_overview(store, workers, cfg, max_age=60):
    if _clients_cache["data"] is not None and time.time() - _clients_cache["at"] < max_age:
        return _clients_cache["data"]
    now = int(time.time() * 1000)
    status = store.get_status()
    profiles = sorted({w.profile for w in workers})
    cards = []
    for prof in profiles:
        views = infra_views(store, workers, prof)
        conn_err = [status.get(w.key, {}).get("error") for w, _, _ in views if not status.get(w.key, {}).get("ok")]
        sched, fns, alarms, api5, nothing = [], [], [], [], []
        for w, snap, st in views:
            sched += aws_infra.schedule_health(snap, st, now, w.covered_from)
            fns += aws_infra.function_health(snap, st, now)
            alarms += [a for a in snap.get("alarms", []) if a["state"] == "ALARM"]
            api5 += [a for a in aws_infra.api_report(snap) if a["e5"]]
            nothing += aws_ops.did_nothing(store, snap, w.profile, w.region, now, EXTRA_RX)
        pats = store.annotate(store.summary({"hours": 24, "profile": prof, "level": "error,suspect", "hide_muted": "1"}), prof)
        cur, prev = store.pattern_totals(prof, now - DAY_MS, now), store.pattern_totals(prof, now - 2 * DAY_MS, now - DAY_MS)
        rt = aws_ops.run_totals(store, prof, now - DAY_MS, now)
        hist = aws_ops.run_totals(store, prof, now - 8 * DAY_MS, now - DAY_MS)
        hist_hours = max((now - DAY_MS) // 3_600_000 - min((v["from_hour"] for v in hist.values() if v["from_hour"]),
                                                          default=(now - DAY_MS) // 3_600_000), 0)
        data_from = max((w.covered_from for w, _, _ in views), default=now)
        cov = {(w.profile, w.region): w.covered_from for w, _, _ in views}

        def impact(d, win=6):
            """Before/after for one deploy; 'before' must actually be loaded or the verdict is unknown."""
            imp = deploy_impact(store, d, win)
            loaded = cov.get((d["profile"], d["region"]), now) <= d["deployed_at"] - win * 3_600_000
            return {"function": d["function"], "at": d["deployed_at"], "label": d["label"], "kind": d["kind"],
                    "verdict": imp["verdict"] if loaded else "before not loaded", "before_loaded": loaded,
                    "before": imp["before"]["error"] + imp["before"]["suspect"],
                    "after": imp["after"]["error"] + imp["after"]["suspect"],
                    "rate_per": imp["rate_per"], "rate_before": imp["rate_before"], "rate_after": imp["rate_after"],
                    "after_hours": imp["after_hours"], "window_hours": win}
        deps = store.list_deploys(prof, now - 7 * DAY_MS)
        latest = {}
        for d in deps:                                   # newest first: keep each function's latest deploy
            latest.setdefault(d["function"], d)
        last_dep = impact(deps[0]) if deps else None
        recent = [impact(d) for d in latest.values() if d["deployed_at"] >= now - DAY_MS]
        bad_deps = [x for x in recent if x["verdict"] in ("worse", "new errors")]
        parts = {
            "connected": not conn_err, "connection_error": (conn_err or [""])[0],
            "sched_bad": [x for x in sched if x["status"] in ("not running", "missed runs", "target missing")],
            "last_failed": [x for x in sched if x["status"] == "last run failed"],
            "regressions": [g for g in pats if g.get("regression")],
            "new": [g for g in pats if g.get("is_new") and not g.get("below_threshold")],
            "spikes": [g for g in pats if g.get("is_spike")],
            "alarms": alarms, "api5xx": api5, "nothing": nothing, "bad_deploys": bad_deps,
            "partners": partner_health(store, {"hours": 6, "profile": prof}, cfg["partner_services"],
                                       cfg.get("partner_function_hints"))[:3],
            "near_limits": sum(1 for f in fns if any(("timeout" in x or "memory" in x) for x in f["flags"])),
            "deprecated": sum(1 for f in fns if f["runtime_status"] == "deprecated"),
            "errors_24h": sum(v["n"] for v in cur.values() if v["level"] == "error"),
            "errors_prev": sum(v["n"] for v in prev.values() if v["level"] == "error"),
            "hidden_24h": sum(v["n"] for v in cur.values() if v["level"] == "suspect"),
            "hidden_prev": sum(v["n"] for v in prev.values() if v["level"] == "suspect"),
            "runs_24h": sum(v["runs"] for v in rt.values()), "failed_runs_24h": sum(v["failed"] for v in rt.values()),
            "records_24h": sum(v["records"] for v in rt.values()),
            "records_usual": round(sum(v["records"] for v in hist.values()) / (hist_hours / 24)) if hist_hours >= 48 else None,
            "sched_ok": sum(1 for x in sched if x["status"] == "ok"), "sched_total": len(sched),
            "last_deploy": last_dep, "cost_30d": round(sum(f["cost_30d"] for f in fns), 2),
            "windows": error_windows(store, prof, data_from, now),
            "recent_deploys": sorted(recent, key=lambda x: -x["at"])[:6],
            "functions": len(fns), "accounts": [w.key for w, _, _ in views]}
        cards.append(aws_ops.client_card(prof, parts))
    # Most critical first: most errors in the last 24h (then last 1h, hidden errors, status as tie-breakers)
    order = {"red": 0, "amber": 1, "green": 2}
    win = lambda c, k, f: ((c.get("windows") or {}).get(k) or {}).get(f) or 0   # the numbers shown on the card
    cards.sort(key=lambda c: (-(win(c, "24h", "errors") or c["errors_24h"] or 0), -win(c, "1h", "errors"),
                              -win(c, "24h", "hidden"), order[c["status"]], -c["red"], c["profile"]))
    _clients_cache.update(at=time.time(), data=cards)
    return cards


def parse_aws_time(text):
    """Lambda LastModified, e.g. '2026-10-07T09:12:33.000+0000' (any fraction length / Z)."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?", text)
    dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    tz = m.group(3) or "Z"
    off = 0 if tz == "Z" else (1 if tz[0] == "+" else -1) * (int(tz[1:3]) * 60 + int(tz[-2:]))
    dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(minutes=off)))
    return dt + datetime.timedelta(microseconds=int((m.group(2) or "0")[:6].ljust(6, "0")))


def version_label(description, env):
    """Version from the function description ('git abc1234') or a whitelisted env var.
    Other environment variables are never read or stored (they often hold secrets)."""
    m = VERSION_DESC_RX.search(description or "")
    if m:
        return m.group(1)[:40]
    for k in VERSION_ENV_KEYS:
        if env.get(k):
            return str(env[k])[:40]
    return ""


def iso(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def events_csv(events):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "profile", "account", "region", "log_group", "log_stream", "level",
                "kind", "type", "signature", "why_suspect", "message"])
    for e in events:
        w.writerow([iso(e["ts"]), e["profile"], e["account"], e["region"], e["group"],
                    e["stream"], e["level"], e["kind"], e["etype"], e["sig"], e["hint"], e["message"]])
    return buf.getvalue()


def summary_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["count", "level", "type", "signature", "why_suspect", "profiles",
                "log_groups", "first_seen", "last_seen", "latest_sample"])
    for g in rows:
        w.writerow([
            g["count"], g["level"], g["etype"], g["sig"], g["hint"],
            "; ".join(f"{p} ({n})" for p, n in g["profiles"].items()),
            "; ".join(f"{p} ({n})" for p, n in g["log_groups"].items()),
            iso(g["first_seen"]), iso(g["last_seen"]), g["sample"],
        ])
    return buf.getvalue()


def volume_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["profile", "log_group", "total", *LEVELS, "last_event"])
    for g in rows:
        w.writerow([g["profile"], g["group"], g["total"], *[g[l] for l in LEVELS], iso(g["last"])])
    return buf.getvalue()


# --------------------------------------------------------------------------- step functions

def _ms(dt):
    return int(dt.timestamp() * 1000) if dt else 0


def _dur(ms):
    s = ms / 1000
    return f"{s:.1f}s" if s < 120 else f"{s / 60:.1f}m" if s < 7200 else f"{s / 3600:.1f}h"


def _details(ev):
    for k, v in ev.items():
        if k.endswith("EventDetails") and isinstance(v, dict):
            return v
    return {}


def _cause_text(cause):
    """Lambda causes are JSON {errorType, errorMessage, stackTrace}; make them readable."""
    if not cause:
        return "", ""
    try:
        c = json.loads(cause)
    except (ValueError, TypeError):
        return cause[:3000], ""
    if isinstance(c, dict) and ("errorMessage" in c or "errorType" in c):
        trace = c.get("stackTrace") or []
        if isinstance(trace, list):
            trace = "\n".join(str(t).rstrip() for t in trace)
        text = f"{c.get('errorType', '')}: {c.get('errorMessage', '')}".strip(": ")
        return (text + ("\n" + str(trace) if trace else ""))[:3000], c.get("errorType", "")
    return json.dumps(c, indent=1)[:3000], ""


def summarize_history(events):
    """Walk an execution history: path of states, every failure (with the state it happened in)."""
    path, failures, final, state = [], [], None, None
    for ev in sorted(events, key=lambda e: e["id"]):
        t = ev["type"]
        if t.endswith("StateEntered"):
            state = ev.get("stateEnteredEventDetails", {}).get("name")
            path.append(state)
        elif t in ("ExecutionFailed", "ExecutionTimedOut", "ExecutionAborted"):
            final = (t, _details(ev))
        elif t.endswith(("Failed", "TimedOut")) and not t.startswith("Execution"):
            d = _details(ev)
            failures.append({"state": state, "type": t, "error": d.get("error") or t,
                             "cause": d.get("cause") or ""})
    return path, failures, final


class StepFunctionsScanner:
    """Turns Step Functions executions into feed entries for one account/region."""

    def __init__(self, worker):
        self.w = worker
        self.cfg = worker.cfg
        self.client = None
        self.machines = {}          # arn -> {"name", "type"}
        self.inspected = {}         # execution arn -> True (bounded)
        self.error = None

    def connect(self, session):
        self.client = session.client(
            "stepfunctions", config=BotoConfig(retries={"max_attempts": 8, "mode": "adaptive"}))

    def refresh(self):
        try:
            machines = {}
            for page in self.client.get_paginator("list_state_machines").paginate():
                for m in page.get("stateMachines", []):
                    machines[m["stateMachineArn"]] = {"name": m["name"], "type": m.get("type", "STANDARD")}
            self.machines, self.error = machines, None
        except Exception as ex:
            self.error = str(ex).splitlines()[0][:200]

    def executions(self, arn, status, start, end, cap, by_stop=True, max_scan=1000):
        """Newest-first executions with this status whose stop (or start) time is in [start, end]."""
        out, oldest_start = [], start - self.cfg["sfn_lookback_hours"] * 3_600_000
        pages = self.client.get_paginator("list_executions").paginate(
            stateMachineArn=arn, statusFilter=status,
            PaginationConfig={"PageSize": 100, "MaxItems": max_scan})
        for page in pages:
            for ex in page.get("executions", []):
                st = _ms(ex.get("startDate"))
                if st < oldest_start:
                    return out
                t = _ms(ex.get("stopDate")) if by_stop else st
                if t >= start and (not end or t <= end):
                    out.append(ex)
                    if len(out) >= cap:
                        return out
        return out

    def history(self, ex_arn):
        events = []
        pages = self.client.get_paginator("get_execution_history").paginate(
            executionArn=ex_arn, includeExecutionData=False,
            PaginationConfig={"PageSize": 1000, "MaxItems": 3000})
        for page in pages:
            events += page.get("events", [])
        return events

    def entry(self, ex, sm, level, kind, etype, sig, hint, message, suffix):
        w = self.w
        return {"id": f"{w.key}:sfn{suffix}:{ex['executionArn']}",
                "ts": _ms(ex.get("stopDate")) or int(time.time() * 1000),
                "profile": w.profile, "account": w.account, "region": w.region,
                "group": f"stepfunctions:{sm['name']}", "stream": ex["executionArn"],
                "message": message, "level": level, "hint": hint, "etype": etype,
                "sig": normalize(sig), "kind": kind}

    def failed_entry(self, ex, sm, status):
        path, failures, final = summarize_history(self.history(ex["executionArn"]))
        fd = final[1] if final else {}
        last = failures[-1] if failures else {}
        error = fd.get("error") or last.get("error") or status
        cause, ctype = _cause_text(fd.get("cause") or last.get("cause"))
        state = last.get("state") or (path[-1] if path else "?")
        dur = _ms(ex.get("stopDate")) - _ms(ex.get("startDate"))
        lines = [f"Step Functions execution {status}: {sm['name']} / {ex['name']}",
                 f"Failed in state: {state}",
                 f"Error: {error}" + (f" ({ctype})" if ctype and ctype not in error else ""),
                 f"Cause: {cause or '(none given)'}",
                 f"Started {iso(_ms(ex.get('startDate')))} · ran {_dur(dur)}",
                 f"Path: {' → '.join(p for p in path[-15:] if p)}"]
        earlier = failures[:-1]
        if earlier:
            lines.append("Earlier failures in this run: " + "; ".join(
                f"{f['state']}: {f['error']}" for f in earlier[-5:]))
        kind = "timeout" if status == "TIMED_OUT" or "Timeout" in error else "error"
        return self.entry(ex, sm, "error", kind, error, f"{state}: {(cause or error).splitlines()[0][:150]}",
                          "", "\n".join(lines), "")

    def caught_entry(self, ex, sm):
        path, failures, _ = summarize_history(self.history(ex["executionArn"]))
        if not failures:
            return None
        counts = {}
        for f in failures:
            k = (f["state"], f["error"])
            counts.setdefault(k, [0, f["cause"]])[0] += 1
        lines = [f"Step Functions execution SUCCEEDED but had {len(failures)} failure(s) "
                 f"that were retried or caught: {sm['name']} / {ex['name']}"]
        for (state, err), (n, cause) in counts.items():
            text, _ = _cause_text(cause)
            lines.append(f"- {state}: {err}" + (f" ×{n}" if n > 1 else "") +
                         (f" — {text.splitlines()[0][:300]}" if text else ""))
        dur = _ms(ex.get("stopDate")) - _ms(ex.get("startDate"))
        lines.append(f"Started {iso(_ms(ex.get('startDate')))} · ran {_dur(dur)}")
        (state, err), _ = next(iter(counts.items()))
        hint = "caught/retried: " + ", ".join(sorted({e for _, e in counts}))[:120]
        return self.entry(ex, sm, "suspect", "suspect", err + " (caught/retried)", f"{state}: {err}",
                          hint, "\n".join(lines), "-caught")

    def stuck_entry(self, ex, sm, now):
        age = now - _ms(ex.get("startDate"))
        e = self.entry(ex, sm, "suspect", "suspect", "Long-running execution", f"{sm['name']} running long",
                       f"running {_dur(age)}",
                       f"Step Functions execution still RUNNING after {_dur(age)}: {sm['name']} / {ex['name']}\n"
                       f"Started {iso(_ms(ex.get('startDate')))}", "-stuck")
        e["ts"] = now
        return e

    def seen(self, key):
        """Remember executions already read so their history isn't fetched again every poll."""
        if key in self.inspected:
            return True
        self.inspected[key] = True
        if len(self.inspected) > 50000:
            self.inspected.pop(next(iter(self.inspected)))
        return False

    def scan(self, start, end, live, max_scan=1000, succeeded_cap=None):
        """Collect findings for [start, end]; returns list of entries."""
        succeeded_cap = succeeded_cap or self.cfg["sfn_max_succeeded_per_poll"]
        if not self.client or not self.machines:
            return []
        out, now = [], int(time.time() * 1000)
        for arn, sm in list(self.machines.items()):
            if sm["type"] != "STANDARD":
                continue                        # Express runs are only visible through their log group
            try:
                for status in ("FAILED", "TIMED_OUT", "ABORTED"):
                    for ex in self.executions(arn, status, start, end, 500, max_scan=max_scan):
                        if self.seen("f:" + ex["executionArn"]):
                            continue
                        out.append(self.failed_entry(ex, sm, status))
                if self.cfg["sfn_inspect_succeeded"]:
                    for ex in self.executions(arn, "SUCCEEDED", start, end, succeeded_cap,
                                              max_scan=max_scan):
                        if self.seen("s:" + ex["executionArn"]):
                            continue
                        e = self.caught_entry(ex, sm)
                        if e:
                            out.append(e)
                if live:
                    limit = now - self.cfg["sfn_stuck_minutes"] * 60_000
                    for ex in self.executions(arn, "RUNNING", 0, None, 100, by_stop=False):
                        if _ms(ex.get("startDate")) < limit:
                            out.append(self.stuck_entry(ex, sm, now))
                self.error = None
            except Exception as ex:
                self.error = f"{sm['name']}: {str(ex).splitlines()[0][:200]}"
                if "AccessDenied" in str(ex):
                    break
        return out


# --------------------------------------------------------------------------- worker

class Worker(threading.Thread):
    """Polls one (profile, region) pair: live tail + background history backfill."""

    def __init__(self, profile, region, cfg, store):
        super().__init__(daemon=True)
        self.profile, self.region, self.cfg, self.store = profile, region, cfg, store
        self.key = f"{profile}/{region}"
        self.cov_key = "v2|" + self.key      # v2 = all-logs capture (re-pulls history once)
        self.groups = {}
        self.groups_refreshed = 0
        self.account = "?"
        self.sampled = 0                     # times a group was too busy to pull every line
        self.sfn = StepFunctionsScanner(self) if cfg["step_functions"] else None
        now = int(time.time() * 1000)
        sc = store.get_coverage("sfn|" + self.key)
        # Step Functions keeps execution history for 90 days; start with the same window as the logs
        self.sfn_covered_from = sc["covered_from"] if sc else now - cfg["lookback_minutes"] * 60_000
        self.keep_ms = int(cfg["keep_hours"] * 3_600_000)
        ex = store.get_coverage("exp|" + self.key)
        self.hist_expires = ex["covered_from"] if ex else 0   # when an older pulled range expires
        self.last_expire_check = 0
        cov = store.get_coverage(self.cov_key)
        if cov:
            # after a long break, only catch up on the cache window, not the whole gap
            self.cursor = max(cov["last_polled"] or 0, now - self.keep_ms)
            self.covered_from = cov["covered_from"]
            self.target = cov["target"] or self.covered_from
        else:
            self.cursor = self.covered_from = self.target = now - cfg["lookback_minutes"] * 60_000
            self.save()
        self.sfn_cursor, self.sfn_last_poll = self.cursor, 0
        self.lam, self.lambda_count, self.lambda_error, self.deploys_checked = None, 0, None, 0
        self.infra = None
        sd = store.get_coverage("seed|" + self.key)
        if sd:
            self.seed_from, self.seed_target = sd["covered_from"], sd["target"]
        else:   # one-time errors-only pull of the last week, so "new" / "spike" have a baseline
            self.seed_from = (now - cfg["lookback_minutes"] * 60_000) // 3_600_000 * 3_600_000
            self.seed_target = self.seed_from - cfg["seed_baseline_days"] * DAY_MS
            store.set_coverage("seed|" + self.key, self.seed_from, 0, self.seed_target)

    def save(self):
        self.store.set_coverage(self.cov_key, self.covered_from, self.cursor, self.target)
        self.store.set_coverage("sfn|" + self.key, self.sfn_covered_from, 0, 0)
        self.store.set_coverage("exp|" + self.key, self.hist_expires, 0, 0)

    def expire(self):
        """Once a pulled range is more than keep_hours old, delete it and forget it was loaded,
        so picking that range again pulls it fresh from AWS."""
        now = int(time.time() * 1000)
        if now < self.hist_expires:
            return 0
        cutoff = now - self.keep_ms
        if min(self.covered_from, self.target, self.sfn_covered_from) < cutoff:
            self.covered_from = max(self.covered_from, cutoff)
            self.target = max(self.target, cutoff)
            self.sfn_covered_from = max(self.sfn_covered_from, cutoff)
            if self.sfn:
                self.sfn.inspected.clear()
            self.save()
        return self.store.delete_older(cutoff, self.profile, self.region)

    def sfn_target(self):
        return max(self.target, int(time.time() * 1000) - 90 * DAY_MS)

    def needs_backfill(self):
        return self.covered_from > self.target or (
            self.sfn is not None and self.sfn.client is not None and self.sfn_covered_from > self.sfn_target())

    def request_history(self, hours):
        now = int(time.time() * 1000)
        target = int(now - hours * 3_600_000)
        if target < now - self.keep_ms:
            self.hist_expires = now + self.keep_ms     # keep this pulled range for keep_hours
        if target < self.target:
            self.target = target
        self.save()

    def client(self):
        session = boto3.Session(profile_name=self.profile, region_name=self.region)
        try:
            self.account = session.client("sts").get_caller_identity()["Account"]
        except Exception:
            pass
        if self.sfn:
            self.sfn.connect(session)
        if self.cfg["track_deploys"]:
            self.lam = session.client("lambda", config=BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"}))
        return session.client(
            "logs", config=BotoConfig(retries={"max_attempts": 8, "mode": "adaptive"}))

    def refresh_groups(self, logs):
        prefixes = self.cfg["log_group_prefixes"] or [None]
        excl = self.cfg["exclude_log_group_substrings"]
        groups = {}
        pager = logs.get_paginator("describe_log_groups")
        for p in prefixes:
            kw = {"logGroupNamePrefix": p} if p else {}
            for page in pager.paginate(**kw):
                for g in page.get("logGroups", []):
                    name = g["logGroupName"]
                    if not any(s in name for s in excl):
                        groups[name] = {"created": g.get("creationTime", 0),
                                        "retention_days": g.get("retentionInDays")}
        names = sorted(groups)[: self.cfg["max_groups_per_region"]]
        self.groups = {n: groups[n] for n in names}
        if self.sfn:
            self.sfn.refresh()
        self.groups_refreshed = time.time()

    def refresh_functions(self):
        """Record Lambda deploys: a changed code fingerprint (CodeSha256) = a new deploy."""
        if not self.lam:
            return
        try:
            funcs = []
            for page in self.lam.get_paginator("list_functions").paginate():
                for f in page.get("Functions", []):
                    env = (f.get("Environment") or {}).get("Variables") or {}
                    lm = parse_aws_time(f["LastModified"])
                    funcs.append({
                        "function": f["FunctionName"],
                        "log_group": (f.get("LoggingConfig") or {}).get("LogGroup")
                        or "/aws/lambda/" + f["FunctionName"],
                        "last_modified": int(lm.timestamp() * 1000), "sha": f.get("CodeSha256", ""),
                        "label": version_label(f.get("Description", ""), env),
                        "runtime": f.get("Runtime", "") or f.get("PackageType", "")})
            for d in self.store.record_functions(self.profile, self.region, funcs):
                print(f"Deploy detected: {self.key} {d['function']} ({d['kind']}"
                      f"{', ' + d['label'] if d['label'] else ''}) at {iso(d['last_modified'])}")
            self.lambda_count, self.lambda_error = len(funcs), None
        except Exception as ex:
            self.lambda_error = str(ex).splitlines()[0][:200]
        self.deploys_checked = time.time()

    def fetch(self, logs, group, start, end, cap, pattern):
        out = []
        kw = dict(logGroupName=group, startTime=start,
                  PaginationConfig={"MaxItems": cap, "PageSize": min(cap, 10000)})
        if pattern:
            kw["filterPattern"] = pattern
        if end:
            kw["endTime"] = end
        for page in logs.get_paginator("filter_log_events").paginate(**kw):
            for e in page.get("events", []):
                out.append({
                    "id": f"{self.key}:{e['eventId']}", "ts": e["timestamp"],
                    "profile": self.profile, "account": self.account, "region": self.region,
                    "group": group, "stream": e.get("logStreamName", ""),
                    "message": e.get("message", "").rstrip(),
                })
        return out, len(out) >= cap

    def pull(self, logs, group, start, end, cap, live, want_all=True):
        """Everything if possible; if the group is too busy, make sure errors still come through."""
        n = 0
        if self.cfg["capture_all_logs"] and want_all:
            evs, hit = self.fetch(logs, group, start, end, cap, "")
            n += self.store.add_many(evs, live)
            if not hit:
                return n
            self.sampled += 1
        evs, _ = self.fetch(logs, group, start, end, cap, self.cfg["filter_pattern"])
        return n + self.store.add_many(evs, live)

    def poll(self, logs):
        start = self.cursor - 120_000          # 2-min overlap for late ingestion; deduped
        started = int(time.time() * 1000)
        new = 0
        for group in list(self.groups):
            try:
                new += self.pull(logs, group, start, None, self.cfg["live_max_events_per_group"], True)
            except logs.exceptions.ResourceNotFoundException:
                continue
            except Exception as ex:
                self.store.set_status(self.key, last_group_error=f"{group}: {ex}")
        if self.sfn and time.time() - self.sfn_last_poll >= self.cfg["sfn_poll_seconds"]:
            new += self.store.add_many(self.sfn.scan(self.sfn_cursor - 120_000, None, live=True), True)
            self.sfn_cursor, self.sfn_last_poll = started, time.time()
        self.cursor = started
        self.save()
        return new

    def backfill_step(self, logs, budget_s=20):
        deadline = time.time() + budget_s
        block = self.cfg["backfill_block_days"] * DAY_MS
        cap = self.cfg["backfill_max_events_per_group_block"]
        while self.covered_from > self.target and time.time() < deadline:
            end = self.covered_from
            start = max(self.target, end - block)
            now = time.time() * 1000
            for group, meta in list(self.groups.items()):
                if meta["created"] and meta["created"] > end + 14 * DAY_MS:
                    continue      # group didn't exist yet (AWS accepts events up to 14 days old)
                if meta["retention_days"] and end < now - meta["retention_days"] * DAY_MS:
                    continue      # already expired in CloudWatch
                try:
                    # only the last keep_hours get every line; older ranges get error-type lines only
                    # (Trace a record searches CloudWatch directly for anything older)
                    self.pull(logs, group, start, end, cap, False, want_all=end > now - self.keep_ms)
                except logs.exceptions.ResourceNotFoundException:
                    continue
                except Exception as ex:
                    self.store.set_status(self.key, last_group_error=f"{group}: {ex}")
            self.covered_from = start
            self.save()
            self.store.rollup(start, end, self.profile, self.region)
            self.report()
        if self.sfn and self.sfn.client and self.sfn_covered_from > self.sfn_target() and time.time() < deadline:
            # Failures are rare, so scan the whole requested range in one go (newest first);
            # successful runs are only inspected for the newest few hundred.
            target = self.sfn_target()
            self.store.add_many(self.sfn.scan(target, self.sfn_covered_from, live=False,
                                              max_scan=20000, succeeded_cap=200), False)
            self.sfn_covered_from = target
            self.save()
            self.report()

    def seed_step(self, logs, budget_s=15):
        """Errors-only pull of older days, folded into the hourly baseline (not kept as lines)."""
        deadline = time.time() + budget_s
        cap = self.cfg["backfill_max_events_per_group_block"]
        while self.seed_from > self.seed_target and time.time() < deadline:
            end = self.seed_from
            start = max(self.seed_target, end - DAY_MS)
            now = time.time() * 1000
            for group, meta in list(self.groups.items()):
                if meta["retention_days"] and end < now - meta["retention_days"] * DAY_MS:
                    continue
                try:
                    self.pull(logs, group, start, end, cap, False, want_all=False)
                except Exception:
                    continue
            self.store.rollup(start, end, self.profile, self.region)
            self.seed_from = start
            self.store.set_coverage("seed|" + self.key, self.seed_from, 0, self.seed_target)

    def report(self, **extra):
        self.store.set_status(
            self.key, covered_from=self.covered_from, target=self.target,
            backfilling=self.covered_from > self.target, sampled=self.sampled,
            groups=len(self.groups), account=self.account,
            state_machines=len(self.sfn.machines) if self.sfn else 0,
            lambdas=self.lambda_count, lambda_error=self.lambda_error,
            sfn_error=self.sfn.error if self.sfn else None,
            baseline_loading=self.seed_from > self.seed_target, **extra)

    def run(self):
        logs = None
        while True:
            try:
                if logs is None:
                    logs = self.client()
                if time.time() - self.groups_refreshed > self.cfg["group_refresh_minutes"] * 60:
                    self.refresh_groups(logs)
                t0 = time.time()
                new = self.poll(logs)
                self.report(ok=True, error=None, last_poll=time.time(),
                            poll_secs=round(time.time() - t0, 1), new_last_poll=new)
                if self.lam and time.time() - self.deploys_checked > self.cfg["deploy_check_seconds"]:
                    self.refresh_functions()
                if time.time() - self.last_expire_check > 300:
                    self.expire()
                    self.last_expire_check = time.time()
                if self.needs_backfill():
                    self.backfill_step(logs)
                    time.sleep(1)
                    continue
                if self.seed_from > self.seed_target:
                    self.seed_step(logs)
                    self.report()
                    time.sleep(1)
                    continue
            except Exception as ex:
                # Typical: expired SSO session -> run `aws sso login --profile X`
                logs = None
                self.store.set_status(self.key, ok=False, profile=self.profile,
                                      region=self.region, error=str(ex).splitlines()[0][:300])
            time.sleep(self.cfg["poll_seconds"])


def build_workers(cfg, store):
    base = boto3.Session()
    profiles = cfg["profiles"] or base.available_profiles or ["default"]
    profiles = [p for p in profiles if p not in cfg["exclude_profiles"]]
    pairs = []
    for p in profiles:
        regions = cfg["regions"]
        if not regions:
            try:
                regions = [boto3.Session(profile_name=p).region_name or "us-east-1"]
            except Exception:
                regions = ["us-east-1"]
        pairs += [(p, r) for r in regions]
    if store is None:
        return pairs
    return [Worker(p, r, cfg, store) for p, r in pairs]


# --------------------------------------------------------------------------- web

TRACES = aws_ops.TraceJobs()
LLM = None
AIJOBS = None
TRIAGE = None
CODE = None


def make_handler(store, workers, cfg):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype, filename=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def json(self, obj):
            self.send(200, json.dumps(obj), "application/json")

        def ai(self, path, q):
            if path == "/api/ai/status":
                st = LLM.check(force=bool(q.get("force")))
                return self.json({**{k: v for k, v in st.items() if k != "models"}, "model": LLM.model,
                                  "triaged": TRIAGE.done if TRIAGE else 0,
                                  "working_on": TRIAGE.running_now if TRIAGE else None,
                                  "last_error": TRIAGE.last_error if TRIAGE else None})
            if path == "/api/ai/triage_map":
                return self.json({f"{k[0]}|{k[1]}|{k[2]}": v for k, v in aws_local.get_triage(store).items()})
            if path == "/api/ai/job":
                return self.json(AIJOBS.get(q.get("id", "")) or {"error": "unknown job"})
            if path == "/api/ai/summarize":
                if not LLM.ready:
                    return self.json({"error": "Local model not available: " + LLM.state["detail"]})
                kind, question = q.get("kind", "lines"), q.get("question", "")
                if kind == "run":
                    r = aws_ops.get_run(store, q["profile"], q["region"], q["grp"], q["request_id"], EXTRA_RX) or {}
                    lines = [dict(l, group=q["grp"]) for l in r.get("lines_list", [])]
                elif kind == "trace":
                    j = TRACES.get(q.get("id", "")) or {"hits": []}
                    lines = sorted(j["hits"], key=lambda h: h["ts"])
                    question = question or f"Follow record {j.get('term')} through the systems: what happened to it, in order?"
                else:
                    f = {k: q.get(k) for k in ("profile", "level", "grp", "grp_prefix", "q", "hours", "etype", "sig",
                                               "since_ms", "until_ms") if q.get(k)}
                    f.setdefault("hours", "24")
                    evs, total = store.list_events(f, int(q.get("limit") or 400))
                    lines = sorted(evs, key=lambda e: e["ts"])
                    question = question or ""
                    if total > len(evs):
                        question = (question + f" (Note: showing the newest {len(evs)} of {total} matching lines.)").strip()
                if not lines:
                    return self.json({"error": "No lines matched."})
                jid = AIJOBS.start(question, aws_local.lines_text(lines, LLM.max_input_chars - 2000), kind)
                return self.json({"id": jid, "lines": len(lines),
                                  "event_ids": [l.get("id") for l in lines if l.get("level") in ("error", "suspect")][:8]})
            if path == "/api/ai/triage_now":
                if not LLM.ready:
                    return self.json({"error": "Local model not available: " + LLM.state["detail"]})
                rows = store.annotate(store.summary({"hours": 24, "profile": q.get("profile"), "level": "error,suspect",
                                                     "etype": q["etype"], "sig": q.get("sig", "")}), q.get("profile"))
                if not rows:
                    return self.json({"error": "pattern not found in the last 24h"})
                prof = q.get("profile") or next(iter(rows[0]["profiles"]))
                threading.Thread(target=TRIAGE.triage_one, args=(prof, rows[0]), daemon=True).start()
                return self.json({"ok": True, "message": "Triage started; it shows up in a minute or two."})
            self.send(404, "not found", "text/plain")

        def ops(self, path, q):
            now = int(time.time() * 1000)
            prof = q.get("profile") or None
            if path == "/api/ops/clients":
                return self.json(clients_overview(store, workers, cfg, 0 if q.get("fresh") else 60))
            if path == "/api/ops/groups":
                rows = aws_ops.lambda_groups(store, prof, now - float(q.get("hours") or 24) * 3_600_000)
                return self.json([{"profile": p, "region": r, "group": g, "function": aws_ops.function_from_group(g)}
                                  for p, r, g in sorted(rows)])
            if path == "/api/ops/runs":
                hrs = float(q.get("hours") or 24)
                return self.json(aws_ops.build_runs(store, q["profile"], q["region"], q["grp"], now - int(hrs * 3_600_000),
                                                    now, EXTRA_RX))
            if path == "/api/ops/run":
                return self.json(aws_ops.get_run(store, q["profile"], q["region"], q["grp"], q["request_id"], EXTRA_RX) or {})
            if path == "/api/ops/nothing":
                return self.json([d for w, snap, _ in infra_views(store, workers, prof)
                                  for d in aws_ops.did_nothing(store, snap, w.profile, w.region, now, EXTRA_RX)])
            if path == "/api/ops/trace/start":
                ws = [w for w in workers if not prof or w.profile == prof]
                jid = TRACES.start(store, ws, q["term"].strip(), float(q.get("days") or 7), q.get("sfn", "1") != "0",
                                   classify_level)
                return self.json({"id": jid})
            if path == "/api/ops/trace":
                j = TRACES.get(q.get("id", ""))
                if not j:
                    return self.json({"error": "unknown trace id"})
                j["timeline"] = aws_ops.trace_timeline(j)
                return self.json(j)
            if path in ("/api/ops/code", "/api/ops/code_index"):
                names = set()
                for w in workers:
                    names |= {f["name"] for f in (store.get_infra(w.key) or {}).get("functions", [])}
                names |= {aws_ops.function_from_group(g) for _, _, g in aws_ops.lambda_groups(store, since=now - DAY_MS)}
                CODE.build(sorted(n for n in names if n))
                if path == "/api/ops/code_index":
                    return self.json({"root": CODE.root, "functions": CODE.map,
                                      "unmapped": sorted(n for n in names if n and n not in CODE.map)})
                ev, _ = store.context(q.get("id", ""), 0, 0, 1)
                if not ev:
                    return self.json({"error": "event not loaded"})
                fn = aws_ops.function_from_group(ev["group"])
                return self.json({"function": fn, "mapped": CODE.map.get(fn), "frames": CODE.locate(fn, ev["message"]),
                                  "message": ev["message"]})
            if path == "/api/ops/notes":
                return self.json(aws_ops.all_notes(store))
            if path == "/api/ops/note":
                aws_ops.set_note(store, q["etype"], q.get("sig", ""), q.get("profile", ""), q.get("note", ""),
                                 q.get("ignore_below") or None)
                _clients_cache["at"] = 0
                return self.json({"ok": True})
            if path in ("/api/ops/report", "/report"):
                if not prof:
                    return self.json({"error": "profile is required"})
                r = aws_ops.client_report(store, workers, prof, float(q.get("days") or 7), ops_helpers(store, workers, cfg))
                if path == "/report":
                    return self.send(200, aws_ops.report_html(r), "text/html; charset=utf-8")
                return self.json({"report": r, "markdown": aws_ops.report_markdown(r)})
            self.send(404, "not found", "text/plain")

        def infra(self, path, q):
            now = int(time.time() * 1000)
            prof = q.get("profile") or None
            views = infra_views(store, workers, prof)
            if path == "/api/infra/status":
                return self.json([{"profile": w.profile, "region": w.region, "scanned_at": snap.get("at"),
                                   "scanning": bool(w.infra and w.infra.scanning), "errors": snap.get("errors", {}),
                                   "counts": {k: len(snap.get(k) or []) for k in
                                              ("functions", "layers", "secrets", "schedules", "apis", "alarms")}}
                                  for w, snap, _ in views])
            if path == "/api/infra/rescan":
                for w, _, _ in views:
                    if w.infra:
                        w.infra.wake.set()
                return self.json({"ok": True})
            if path == "/api/infra/schedules":
                return self.json([r for w, snap, st in views
                                  for r in tag(aws_infra.schedule_health(snap, st, now, w.covered_from), w)])
            if path == "/api/infra/functions":
                rows = [r for w, snap, st in views for r in tag(aws_infra.function_health(snap, st, now), w)]
                if q.get("function"):
                    rows = [r for r in rows if r["name"] == q["function"]]
                return self.json(rows)
            if path == "/api/infra/layers":
                return self.json([r for w, snap, _ in views for r in tag(aws_infra.layers_report(snap, store), w)])
            if path == "/api/infra/secrets":
                out = []
                for w, snap, _ in views:
                    auth = [p for p in partner_health(store, {"hours": 48, "profile": w.profile}, cfg["partner_services"],
                                                      cfg.get("partner_function_hints"))
                            if p["problem"] in ("auth", "permission")]
                    out += tag(aws_infra.secrets_report(snap, now, auth), w)
                return self.json(out)
            if path == "/api/infra/apis":
                return self.json({"apis": [r for w, snap, _ in views for r in tag(aws_infra.api_report(snap), w)],
                                  "alarms": [dict(a, profile=w.profile, region=w.region) for w, snap, _ in views
                                             for a in snap.get("alarms", [])]})
            if path == "/api/infra/cost":
                out = []
                for w, snap, st in views:
                    fh = aws_infra.function_health(snap, st, now)
                    out.append({"profile": w.profile, "region": w.region,
                                "cost_24h": round(sum(f["cost_24h"] for f in fh), 4),
                                "cost_30d": round(sum(f["cost_30d"] for f in fh), 2),
                                "top": sorted(({"name": f["name"], "cost_30d": f["cost_30d"], "invocations_24h": f["invocations_24h"],
                                                "memory": f["memory"], "avg_ms": f["avg_ms"]} for f in fh),
                                              key=lambda x: -x["cost_30d"])[:10]})
                out.sort(key=lambda r: -r["cost_30d"])
                return self.json(out)
            if path == "/api/digest":
                dg = build_digest(store, workers, cfg, prof)
                return self.json({"digest": dg, "text": digest_text(dg)})
            self.send(404, "not found", "text/plain")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            stamp = time.strftime("%Y%m%d-%H%M")
            if u.path == "/":
                return self.send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/events":
                evs, total = store.list_events(q, int(q.get("limit") or 500))
                return self.json({"events": evs, "total": total})
            if u.path == "/api/new":
                return self.json(store.new_since(int(q.get("since") or 0),
                                                 [l for l in q.get("notify", "error").split(",") if l]))
            if u.path == "/api/status":
                return self.json(store.get_status())
            if u.path == "/api/summary":
                return self.json(store.annotate(store.summary(q), q.get("profile")))
            if u.path.startswith("/api/infra/") or u.path.startswith("/api/digest"):
                return self.infra(u.path, q)
            if u.path.startswith("/api/ai/"):
                return self.ai(u.path, q)
            if u.path.startswith(("/api/ops/", "/report")):
                return self.ops(u.path, q)
            if u.path == "/api/mute":
                store.set_mute(q["etype"], q.get("sig", ""), q.get("profile", ""),
                               "fixed" if q.get("status") == "fixed" else "muted", q.get("note", ""))
                return self.json({"ok": True})
            if u.path == "/api/unmute":
                return self.json({"removed": store.unmute(q["etype"], q.get("sig", ""), q.get("profile", ""))})
            if u.path == "/api/mutes":
                return self.json(store.list_mutes())
            if u.path == "/api/partners":
                return self.json(partner_health(store, q, cfg["partner_services"], cfg.get("partner_function_hints")))
            if u.path == "/api/sfn_input":
                arn, prof, region = q.get("execution_arn"), q.get("profile"), q.get("region")
                if q.get("id"):
                    ev, _ = store.context(q["id"], 0, 0, 1)
                    if not ev or not ev["group"].startswith("stepfunctions:"):
                        return self.json({"error": "That isn't a Step Functions execution entry."})
                    arn, prof, region = ev["stream"], ev["profile"], ev["region"]
                w = next((w for w in workers if w.profile == prof and (not region or w.region == region)
                          and w.sfn and w.sfn.client), None)
                if not (arn and w):
                    return self.json({"error": "Need an execution_arn and the profile it belongs to."})
                try:
                    return self.json(sfn_run_input(w.sfn.client, arn))
                except Exception as ex:
                    return self.json({"error": str(ex).splitlines()[0][:300]})
            if u.path == "/api/context":
                ev, lines = store.context(q.get("id", ""), int(float(q.get("before") or 2) * 60000),
                                          int(float(q.get("after") or 1) * 60000))
                return self.json({"event": ev, "lines": lines})
            if u.path == "/api/deploys":
                since = int((time.time() - float(q.get("hours") or 168) * 3600) * 1000)
                win = float(q.get("window_hours") or 24)
                out = []
                for d in store.list_deploys(q.get("profile"), since, q.get("function")):
                    imp = deploy_impact(store, d, win)
                    imp.pop("patterns")
                    w = next((w for w in workers if w.profile == d["profile"] and w.region == d["region"]), None)
                    imp["before_loaded"] = bool(w and w.covered_from <= d["deployed_at"] - win * 3_600_000)
                    out.append(imp)
                return self.json(out)
            if u.path == "/api/deploy_impact":
                rows = store.list_deploys(q.get("profile"), 0, q.get("function"))
                if q.get("deployed_at"):
                    rows = [r for r in rows if r["deployed_at"] <= int(q["deployed_at"])] or rows
                if not rows:
                    return self.json({"error": "No deploys recorded for that function (yet)."})
                if len({(r["profile"], r["region"]) for r in rows}) > 1 and not q.get("profile"):
                    return self.json({"matches": sorted({r["profile"] for r in rows})})
                d, win = rows[0], float(q.get("window_hours") or 24)
                w = next((w for w in workers if w.profile == d["profile"] and w.region == d["region"]), None)
                need = d["deployed_at"] - win * 3_600_000
                loading = bool(w and w.covered_from > need)
                if loading:
                    w.request_history((time.time() * 1000 - need) / 3_600_000 + 0.1)
                imp = deploy_impact(store, d, win)
                imp["history_loading"] = loading
                imp["previous"] = [r for r in rows[1:6]]
                return self.json(imp)
            if u.path == "/api/volume":
                return self.json(store.volume(q))
            if u.path == "/api/backfill":
                hours = float(q.get("hours") or 0)
                for w in workers:
                    w.request_history(hours)
                return self.json({"ok": True})
            if u.path == "/export/events.csv":
                return self.send(200, events_csv(store.all_events(q)), "text/csv; charset=utf-8",
                                 f"aws-logs-{stamp}.csv")
            if u.path == "/export/summary.csv":
                return self.send(200, summary_csv(store.summary(q)), "text/csv; charset=utf-8",
                                 f"aws-log-patterns-{stamp}.csv")
            if u.path == "/export/volume.csv":
                return self.send(200, volume_csv(store.volume(q)), "text/csv; charset=utf-8",
                                 f"aws-log-volume-{stamp}.csv")
            if u.path == "/export/events.json":
                return self.send(200, json.dumps(store.all_events(q), indent=1),
                                 "application/json", f"aws-logs-{stamp}.json")
            self.send(404, "not found", "text/plain")

    return H


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>AWS Error Feed</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e8ee;--mute:#8a91a3;--acc:#4dabf7;
--err:#ff6b6b;--tb:#ffa94d;--to:#b197fc;--crit:#ff3b6b;--ok:#51cf66;--sus:#fcc419;--warn:#ffd8a8;--info:#5c677d;--plat:#3b4252;--mark:rgba(252,196,25,.35)}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ec;--fg:#1b1f29;--mute:#667085;--acc:#1c7ed6;--sus:#f08c00;--warn:#e8590c;--info:#adb5bd;--plat:#dee2e6;--mark:rgba(250,176,5,.35)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px}
h1{font-size:16px;margin:0 8px 0 0;display:inline}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.row+.row{margin-top:8px}
input,select,button{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 9px}
button{cursor:pointer}label{font-size:13px;color:var(--mute);display:flex;gap:4px;align-items:center}
.tabs{display:flex;gap:4px;flex-wrap:wrap}.tabs button.on{border-color:var(--acc);color:var(--acc)}
.tabs .n{font-size:11px;opacity:.75;margin-left:4px}
#status{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--mute)}
.chip.bad{border-color:var(--err);color:var(--err)}.chip.good b{color:var(--ok)}
.chip.filter{border-color:var(--acc);color:var(--acc);cursor:pointer}
#hist{display:none;margin-top:8px;font-size:12px;color:var(--mute)}
#hist .track{display:inline-block;width:160px;height:6px;border-radius:3px;background:var(--line);vertical-align:middle;margin:0 8px;overflow:hidden}
#hist .fill{height:100%;background:var(--acc);width:0}
main{padding:12px 16px;max-width:1300px;margin:0 auto}
.help{color:var(--mute);font-size:13px;margin:0 0 10px}
.ev{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--err);border-radius:8px;margin-bottom:6px;padding:7px 12px}
.ev.traceback{border-left-color:var(--tb)}.ev.timeout{border-left-color:var(--to)}.ev.critical{border-left-color:var(--crit)}
.ev.suspect{border-left-color:var(--sus)}.ev.warning{border-left-color:var(--warn)}.ev.info{border-left-color:var(--info)}.ev.platform{border-left-color:var(--plat);opacity:.8}
.ev.new{animation:flash 2s}@keyframes flash{from{background:rgba(255,107,107,.18)}}
.meta{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--mute)}
.meta b{color:var(--fg)}.meta a{color:inherit}
.etype{font-weight:600;color:var(--fg)}.lvl{text-transform:uppercase;font-size:11px;letter-spacing:.04em}
.why{color:var(--sus)}
.first{margin-top:3px;font-family:ui-monospace,Menlo,monospace;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
mark{background:var(--mark);color:inherit;border-radius:2px}
pre{display:none;margin:6px 0 0;white-space:pre-wrap;word-break:break-word;font-size:12px;max-height:500px;overflow:auto;background:var(--bg);padding:8px;border-radius:6px}
.ev.open pre{display:block}.ev.open .first{white-space:normal}
.empty{color:var(--mute);text-align:center;padding:40px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}
th{font-size:12px;color:var(--mute);font-weight:600;cursor:pointer;user-select:none;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.sig{font-family:ui-monospace,Menlo,monospace;font-size:12px;word-break:break-word}
td.small{font-size:12px;color:var(--mute)}
tr.click:hover{background:var(--bg);cursor:pointer}
.bar{display:inline-block;height:6px;border-radius:3px;background:var(--err);margin-right:6px;vertical-align:middle}
.stack{display:flex;height:8px;border-radius:4px;overflow:hidden;min-width:120px;background:var(--line)}
.stack span{display:block;height:100%}
.totals{display:flex;gap:16px;margin-bottom:10px;color:var(--mute);font-size:13px;flex-wrap:wrap}.totals b{color:var(--fg);font-size:16px}
.view{display:none}.view.on{display:block}
@media (max-width:760px){
 header{position:static;padding:8px 10px}
 .tabs{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px}
 .tabs button{flex:0 0 auto}
 #export,#pause,#notif,label[for],.row label{display:none}
 #q{width:100%;flex:1 1 100%}
 #status .chip{display:none}#status .chip.bad{display:inline-block}
 main{padding:8px 10px}.help{display:none}
 .cards{grid-template-columns:1fr}
 table{display:block;overflow-x:auto}
 .meta{gap:6px}.first{white-space:normal}
}
.badge{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.04em;padding:1px 6px;border-radius:4px;margin:2px 4px 0 0;vertical-align:middle}
.b-new{background:var(--acc);color:#fff}.b-spike{background:var(--sus);color:#000}.b-reg{background:var(--err);color:#fff}
.b-muted,.b-fixed{border:1px solid var(--line);color:var(--mute)}
.act{font-size:11px;color:var(--mute);cursor:pointer;text-decoration:underline;margin-right:8px;background:none;border:0;padding:0}
.act:hover{color:var(--acc)}
#regress{display:none;margin-top:8px;padding:6px 10px;border:1px solid var(--err);border-radius:6px;font-size:13px;color:var(--err)}
#regress a{color:inherit;cursor:pointer;text-decoration:underline}
.inputbox{margin-top:8px;font-size:12px}.inputbox h4{margin:8px 0 2px;font-size:12px;color:var(--mute)}
.inputbox pre{display:block;max-height:300px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.cc{background:var(--panel);border:1px solid var(--line);border-top:4px solid var(--ok);border-radius:10px;padding:12px 14px}
.cc.red{border-top-color:var(--err)}.cc.amber{border-top-color:var(--sus)}
.cc h3{margin:0;font-size:16px;display:flex;justify-content:space-between;align-items:center;cursor:pointer}
.pill{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;color:#fff;background:var(--ok);text-transform:uppercase}
.pill.red{background:var(--err)}.pill.amber{background:var(--sus);color:#000}
.kv{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:10px 0}.kv div{font-size:11px;color:var(--mute)}.kv b{display:block;font-size:15px;color:var(--fg)}
.up{color:var(--err)}.down{color:var(--ok)}
.cc ul{margin:6px 0 0 16px;padding:0;font-size:12.5px}.cc .foot{margin-top:8px;font-size:12px;color:var(--mute);display:flex;gap:10px;flex-wrap:wrap}
.cc .foot a{color:var(--acc);cursor:pointer}
.note{font-size:12px;color:var(--acc);font-style:italic;margin-top:2px}
.win{width:100%;border:0;margin:10px 0 2px;background:none}.win th,.win td{border:0;padding:2px 4px;font-size:12px}
.win th{color:var(--mute);font-weight:500;cursor:default}.win td{text-align:right;font-variant-numeric:tabular-nums}
.win td b{font-size:15px;color:var(--fg)}.win td .chg{display:block;font-size:10.5px;color:var(--mute)}
.win td .chg.up{color:var(--err)}.win td .chg.down{color:var(--ok)}
.ai{font-size:12px;margin-top:3px;padding:3px 8px;border-radius:6px;background:rgba(77,171,247,.10);border-left:3px solid var(--acc)}
.ai b{text-transform:uppercase;font-size:10.5px;letter-spacing:.04em}.ai .v-actionable{color:var(--err)}.ai .v-noise{color:var(--ok)}
.ai .v-external{color:var(--sus)}.ai .v-unclear{color:var(--mute)}
.aibox{margin-top:8px;padding:8px 10px;border-radius:6px;background:rgba(77,171,247,.08);font-size:13px;white-space:pre-wrap}
.deps{margin:8px 0 0;font-size:12px}.deps div{display:flex;gap:6px;align-items:baseline;padding:2px 0;border-top:1px dashed var(--line)}
.deps .fn{flex:1;font-family:ui-monospace,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.run-lines{display:none}.run-lines.on{display:table-row}.run-lines td{background:var(--bg)}
.tl{border-left:2px solid var(--line);margin-left:6px;padding-left:12px}.tl .ev{margin-bottom:4px}
</style></head><body>
<header>
 <div class="row">
  <h1>AWS Error Feed</h1>
  <div class="tabs">
   <button data-v="clients" class="on">Clients</button>
   <button data-v="errors">Errors<span class="n" id="n-error"></span></button>
   <button data-v="suspect">Hidden errors?<span class="n" id="n-suspect"></span></button>
   <button data-v="all">All logs<span class="n" id="n-all"></span></button>
   <button data-v="patterns">Patterns</button>
   <button data-v="volume">Volume</button>
   <button data-v="deploys">Deploys</button>
   <button data-v="partners">Partner APIs</button>
   <button data-v="runs">Runs</button>
   <button data-v="trace">Trace a record</button>
   <button data-v="schedules">Schedules<span class="n" id="n-sched"></span></button>
   <button data-v="inventory">Inventory</button>
   <button data-v="apis">APIs &amp; alarms</button>
   <button data-v="digest">Digest</button>
  </div>
 </div>
 <div class="row">
  <select id="fProfile"><option value="">All profiles</option></select>
  <select id="fHours" title="The last 24 hours are always on hand. Older ranges are pulled from AWS when you pick them and cleared again 24 hours later.">
   <option value="1">Last hour</option><option value="24" selected>Last 24 hours</option>
   <option value="168">Last 7 days</option><option value="720">Last month</option>
   <option value="1440">Last 2 months</option><option value="2160">Last 3 months</option>
   <option value="4320">Last 6 months</option><option value="8760">Last 12 months</option>
   <option value="">Everything loaded right now</option></select>
  <select id="fLevel" title="Which log levels to show">
   <option value="error,suspect,warning,info">All levels (no Lambda START/END/REPORT)</option>
   <option value="">All levels incl. Lambda platform lines</option>
   <option value="error,suspect">Errors + hidden errors</option>
   <option value="error">Errors</option><option value="suspect">Hidden errors?</option>
   <option value="warning">Warnings</option><option value="info">Info</option><option value="platform">Lambda platform (REPORT)</option></select>
  <input id="q" placeholder="Search…" size="22">
  <button id="pause">Pause</button>
  <button id="notif">Enable notifications</button>
  <label><input type="checkbox" id="notifySus"> also notify hidden errors</label>
  <label title="Muted patterns, and lines from before a pattern was marked fixed, are hidden unless this is ticked"><input type="checkbox" id="showMuted"> show muted</label>
  <select id="export"><option value="">Export…</option><option value="events.csv">This view's log lines (CSV)</option><option value="events.json">This view's log lines (JSON)</option><option value="summary.csv">Patterns (CSV)</option><option value="volume.csv">Volume per log group (CSV)</option></select>
  <span id="count" class="chip"></span>
  <span id="chips"></span>
 </div>
 <div id="regress"></div>
 <div id="hist"></div>
 <div id="status"></div>
 <div id="aiStatus" class="small" style="margin-top:6px"></div>
</header>
<main>
 <div id="v-clients" class="view on">
  <p class="help">Status of every client at a glance: <b style="color:var(--err)">red</b> = something is broken (jobs not running, regressions, alarms, server errors, syncs that did nothing), <b style="color:var(--sus)">amber</b> = worth a look, <b style="color:var(--ok)">green</b> = all quiet. Click a client to open its errors; "report" opens a printable health report.</p>
  <div id="clientCards" class="cards"></div></div>
 <div id="v-runs" class="view">
  <p class="help">Each Lambda run as one row: when it ran, how long, records processed, errors and outcome. Click a run to see everything it logged. Above: functions that ran but processed nothing / far less than usual, or went quiet.</p>
  <div id="nothingBox"></div>
  <div class="row" style="margin:8px 0"><select id="runGroup" style="min-width:320px"></select><label><input type="checkbox" id="runProblems"> only runs with problems</label></div>
  <table><thead><tr><th>Ended</th><th class="num">Duration</th><th class="num">Records</th><th class="num">Errors</th><th class="num">Hidden</th><th class="num">Memory</th><th>Outcome</th><th>First problem</th></tr></thead><tbody id="runBody"></tbody></table></div>
 <div id="v-trace" class="view">
  <p class="help">Find every log line, Lambda run and Step Functions execution that mentions an ID (HubSpot deal, NetSuite record, email…). Searches CloudWatch directly for that ID, so it can go back weeks without loading all logs. Use the profile picker to search one client (faster).</p>
  <div class="row" style="margin-bottom:10px"><input id="trTerm" placeholder="Record ID, e.g. 991 or jane@acme.com" size="34">
   <select id="trDays"><option value="1">last day</option><option value="7" selected>last 7 days</option><option value="14">last 14 days</option><option value="30">last 30 days</option></select>
   <label><input type="checkbox" id="trSfn" checked> include Step Functions inputs</label><button id="trGo">Trace</button><button id="trSum" class="act">🤖 summarize the trace</button><span id="trStatus" class="small"></span></div>
  <div id="trBody"></div></div>
 <div id="v-feed" class="view">
  <p class="help" id="feedHelp"></p>
  <div id="feed"></div><div id="empty" class="empty">Nothing here for this range yet.</div></div>
 <div id="v-patterns" class="view">
  <p class="help">Similar log lines grouped together (ids, numbers and timestamps ignored). <b>NEW</b> = first seen in the last 24h; <b>SPIKE</b> = 24h count well above the previous 7-day daily average; <b>REGRESSION</b> = marked fixed but back. <i>mute</i> hides known noise, <i>fixed</i> hides it until it comes back. Click a row to see those lines.</p>
  <div class="row" style="margin-bottom:8px"><select id="pFilter"><option value="">All patterns</option><option value="newspike">New &amp; spiking only</option><option value="reg">Regressions only</option></select><span id="baseNote" class="small" style="color:var(--mute)"></span></div>
  <div class="totals" id="totals"></div><table><thead><tr>
  <th data-k="count" class="num">Count</th><th data-k="etype">Type</th><th data-k="sig">Message (normalized)</th>
  <th data-k="profiles">Profiles</th><th data-k="log_groups">Log groups</th>
  <th data-k="first_seen">First seen</th><th data-k="last_seen">Last seen</th></tr></thead>
  <tbody id="sumBody"></tbody></table></div>
 <div id="v-deploys" class="view">
  <p class="help">Each Lambda deploy (code change) or settings change, with errors + hidden errors in equal windows before and after it — per invocation when Lambda REPORT lines are available. Click a row to see that function's logs since the deploy. Add <code>git &lt;commit&gt;</code> to a function's description to show its version.</p>
  <div class="row" style="margin-bottom:10px"><label>Compare windows of <select id="dWin"><option value="1">1 hour</option><option value="6">6 hours</option><option value="24" selected>24 hours</option><option value="72">3 days</option></select></label></div>
  <table><thead><tr><th>Deployed</th><th>Profile</th><th>Function</th><th>Change</th><th class="num">Calls before → after</th><th class="num">Errors+hidden before → after</th><th class="num">Rate change</th><th>Patterns</th><th>Verdict</th></tr></thead>
  <tbody id="depBody"></tbody></table></div>
 <div id="v-schedules" class="view">
  <p class="help">Every EventBridge schedule / scheduled rule, when it should have run in the last 24h (or since logs were loaded) and whether its Lambda or state machine actually ran. Catches jobs that stop silently — they never log an error. <span id="infraNote"></span></p>
  <table><thead><tr><th>Status</th><th>Profile</th><th>Schedule</th><th>Runs</th><th>Target</th><th class="num">Expected</th><th class="num">Ran</th><th>Last run</th><th>Next run</th><th>Details</th></tr></thead>
  <tbody id="schBody"></tbody></table></div>
 <div id="v-inventory" class="view">
  <p class="help">Each client's Lambdas with runtime support status, layers, timeout / memory headroom (from the last 24h of runs), throttles and estimated Lambda cost; then layers (with the packages inside each version) and secrets (metadata only — values are never read). <button class="act" id="rescan">re-scan now</button></p>
  <div class="row" style="margin-bottom:8px"><select id="invView"><option value="functions">Functions</option><option value="layers">Layers &amp; packages</option><option value="secrets">Secrets</option><option value="cost">Cost by client</option></select>
   <label><input type="checkbox" id="invFlagged"> only ones needing attention</label></div>
  <div id="invBody"></div></div>
 <div id="v-apis" class="view">
  <p class="help">API Gateway (REST and HTTP APIs) over the last 24h: requests, 4xx, 5xx and p99 latency per stage, with the Lambda behind each route — a 5xx on a webhook endpoint usually means that event was lost. Below: CloudWatch alarms that are firing.</p>
  <div id="apiBody"></div></div>
 <div id="v-digest" class="view">
  <p class="help">What needs attention today, per client: broken schedules, regressions, new / spiking errors, deploy results, partner API problems, alarms, API 5xx, functions near limits and the upgrade backlog. Ask Claude for the same with "give me today's digest".</p>
  <div id="digBody"></div></div>
 <div id="v-partners" class="view">
  <p class="help">API problems with partner services across all clients, from error and hidden-error lines: <b>auth</b> (401, expired token), <b>permission</b> (403, missing scopes), <b>rate limit</b> (429), <b>server error</b> (5xx) and <b>timeout</b>. A red flag means the same problem hit several clients in the last hour — usually the partner's side or a shared credential, not your code. Click a row for the lines.</p>
  <table><thead><tr><th>Service</th><th>Problem</th><th class="num">Lines</th><th class="num">Last hour</th><th>Clients</th><th>Functions</th><th>Last seen</th><th>Example</th></tr></thead>
  <tbody id="partBody"></tbody></table></div>
 <div id="v-volume" class="view">
  <p class="help">What's coming through each log group in the selected range. Click a row to see its logs.</p>
  <div class="totals" id="vtotals"></div><table><thead><tr>
  <th>Profile</th><th>Log group</th><th class="num">Total</th><th>Mix</th><th class="num">Errors</th><th class="num">Hidden?</th>
  <th class="num">Warn</th><th class="num">Info</th><th class="num">Platform</th><th>Last line</th></tr></thead>
  <tbody id="volBody"></tbody></table></div>
</main>
<script>
let seq=0, paused=false, tab='errors', sigFilter=null, grpFilter=null, sumRows=[], sortKey='count', sortDir=-1, busy=false, lastHeavy=0;
const openIds=new Set(), knownProfiles=new Set();
const $=id=>document.getElementById(id);
const getJ=async u=>(await fetch(u)).json();
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const enc=s=>encodeURIComponent(encodeURIComponent(s)).replace(/%/g,'$');
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v);
const consoleUrl=e=>e.group.startsWith('stepfunctions:')?`https://${e.region}.console.aws.amazon.com/states/home?region=${e.region}#/v2/executions/details/${encodeURIComponent(e.stream)}`:`https://${e.region}.console.aws.amazon.com/cloudwatch/home?region=${e.region}#logsV2:log-groups/log-group/${enc(e.group)}/log-events/${enc(e.stream)}`;
const firstLine=m=>{const l=m.split('\n').map(x=>x.trim()).filter(Boolean);
  const tb=l.findIndex(x=>/^\w*(Error|Exception)\b/.test(x)); return (tb>=0?l[tb]:l[0])||m};
const ago=ms=>{const s=(Date.now()-ms)/1000;return s<90?Math.round(s)+'s ago':s<5400?Math.round(s/60)+'m ago':s<129600?Math.round(s/3600)+'h ago':Math.round(s/86400)+'d ago'};
const day=ms=>new Date(ms).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
const store={get:k=>{try{return localStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};
const HELP={errors:'Lines logged as errors: ERROR / CRITICAL / FATAL, tracebacks, Lambda crashes and timeouts.',
  suspect:'Lines that were NOT logged as an error but look like one — e.g. a try/except that print()s “failed to…”, “could not…”, an exception name, or a 4xx/5xx status. Highlighted words show why each line was flagged.',
  all:'Every log line coming through. Use the level picker to narrow it down.'};
const levelFor=()=>tab==='errors'?'error':tab==='suspect'?'suspect':$('fLevel').value;
const params=(withSig=true)=>{const p=new URLSearchParams();
  for(const [k,id] of [['profile','fProfile'],['hours','fHours'],['q','q']]) if($(id).value)p.set(k,$(id).value);
  const lv=levelFor(); if(lv)p.set('level',lv);
  if(grpFilter)p.set('grp',grpFilter);
  if(withSig&&sigFilter){p.set('etype',sigFilter.etype);p.set('sig',sigFilter.sig)}
  if(!$('showMuted').checked)p.set('hide_muted','1');
  return p};
async function mute(etype,sig,profile,status){
  const p=new URLSearchParams({etype,sig,profile:profile||'',status}); await getJ('/api/mute?'+p); refresh(null,true); loadRegress();}
async function unmute(etype,sig,profile){
  await getJ('/api/unmute?'+new URLSearchParams({etype,sig,profile:profile||''})); refresh(null,true); loadRegress();}
let regTick=0;
async function loadRegress(){
  try{ const ms=await getJ('/api/mutes'), reg=ms.filter(m=>m.regression), el=$('regress');
    if(!reg.length){el.style.display='none';return}
    el.style.display='block';
    el.innerHTML='<b>Regression:</b> '+reg.slice(0,4).map((m,i)=>`<a data-i="${i}">${esc(m.etype)}: ${esc((m.sig||'').slice(0,60))}</a>${m.profile?' ('+esc(m.profile)+')':''} — back ${m.since_count}× since marked fixed ${ago(m.at)}`).join(' · ')+(reg.length>4?` · +${reg.length-4} more`:'');
    el.querySelectorAll('a').forEach(a=>a.onclick=()=>{const m=reg[a.dataset.i]; if(m.profile)$('fProfile').value=m.profile; setSig({etype:m.etype,sig:m.sig}); show('errors')});
  }catch(e){}
}
async function showInput(e,box){
  box.innerHTML='<div class="small">Loading run input from AWS…</div>';
  const r=await getJ('/api/sfn_input?id='+encodeURIComponent(e.id));
  if(r.error){box.innerHTML=`<div class="small">${esc(r.error)}</div>`;return}
  box.innerHTML=`<h4>Run input</h4><pre>${esc(r.run_input||'(none)')}</pre>`+r.failures.map(f=>`<h4>Failed in ${esc(f.state)} — ${esc(f.error||f.event)}${f.lambda?' · called '+esc(f.lambda):''}</h4>`+
    (f.lambda_payload?`<div class="small">Payload sent to the Lambda/task:</div><pre>${esc(f.lambda_payload)}</pre>`:'')+
    (f.state_input?`<div class="small">State input:</div><pre>${esc(f.state_input)}</pre>`:'')).join('');
}
function highlight(text,hint){
  let h=esc(text); if(!hint)return h;
  const words=hint.split(', ').map(w=>w.replace(/^HTTP /,'')).filter(Boolean).map(w=>w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));
  return words.length?h.replace(new RegExp('('+words.join('|')+')','gi'),'<mark>$1</mark>'):h;
}
function headline(e){
  if(e.group.startsWith('stepfunctions:')){const L=e.message.split('\n'), d=L.find(x=>/^(Cause: |- )/.test(x)&&!/\(none given\)/.test(x))||L.find(x=>x.startsWith('Error: '));
    return L[0]+(d?'  —  '+d.replace(/^(Cause: |- )/,''):'')}
  return e.level==='error'?firstLine(e.message):e.message.split('\n')[0];
}
function card(e,isNew){
  const d=document.createElement('div'); d.className=`ev ${e.level==='error'?e.kind:e.level}${isNew?' new':''}${openIds.has(e.id)?' open':''}`;
  d.innerHTML=`<div class="meta"><span>${new Date(e.ts).toLocaleString()}</span>
   <b>${esc(e.profile)}</b><span>${esc(e.account)} · ${esc(e.region)}</span>
   <span>${esc(e.group)}</span><span class="lvl">${esc(e.level)}</span>${e.level==='error'||e.level==='suspect'?`<span class="etype">${esc(e.etype)}</span>`:''}
   ${e.hint?`<span class="why">flagged: ${esc(e.hint)}</span>`:''}
   <a href="${consoleUrl(e)}" target="_blank">open in console ↗</a></div>
   ${e.regression?'<span class="badge b-reg">REGRESSION</span>':''}${noteFor(e,e.profile)?`<div class="note">📝 ${esc(noteFor(e,e.profile).note)}</div>`:''}${aiLine(AITRIAGE[e.etype+'|'+e.sig+'|'+e.profile])}
   <div class="first">${highlight(headline(e),e.hint)}</div><pre>${highlight(e.message,e.hint)}</pre>
   ${e.level==='error'||e.level==='suspect'?`<div><button class="act" data-a="mute" title="Hide this pattern for ${esc(e.profile)}">mute</button><button class="act" data-a="fixed" title="Hide until it happens again (then it's flagged as a regression)">mark fixed</button><button class="act" data-a="note">note</button>${/File "|\sat .*:\d+:\d+/.test(e.message)?'<button class="act" data-a="code">open code</button>':''}${e.group.startsWith('stepfunctions:')?'<button class="act" data-a="input">show run input</button>':''}</div><div class="inputbox"></div>`:''}`;
  d.querySelector('.first').onclick=()=>{d.classList.toggle('open'); d.classList.contains('open')?openIds.add(e.id):openIds.delete(e.id)};
  d.querySelectorAll('.act').forEach(b=>b.onclick=ev=>{ev.stopPropagation();
    if(b.dataset.a==='input')return showInput(e,d.querySelector('.inputbox'));
    if(b.dataset.a==='code')return showCode(e,d.querySelector('.inputbox'));
    if(b.dataset.a==='note')return editNote(e.etype,e.sig,e.profile);
    mute(e.etype,e.sig,e.profile,b.dataset.a)});
  return d;
}
async function loadFeed(newIds){
  const p=params(); p.set('limit','500');
  const r=await getJ('/api/events?'+p), f=$('feed'); f.innerHTML='';
  r.events.forEach(e=>f.appendChild(card(e,newIds&&newIds.has(e.id))));
  $('empty').style.display=r.events.length?'none':'block';
  $('count').textContent=r.total>r.events.length?`newest ${r.events.length} of ${r.total} matching`:`${r.total} matching`;
}
function renderSummary(){
  let rows=[...sumRows]; const pf=$('pFilter').value;
  if(pf==='newspike')rows=rows.filter(g=>g.is_new||g.is_spike); else if(pf==='reg')rows=rows.filter(g=>g.regression);
  const bh=sumRows.length?sumRows[0].baseline_hours:0;
  $('baseNote').textContent=bh<48?` Baseline still building (${bh}h of history so far) — NEW needs 2 days, SPIKE compares with the previous week.`:'';
  const val=(g,k)=>k==='profiles'||k==='log_groups'?Object.keys(g[k]).length:g[k];
  rows.sort((a,b)=>{const x=val(a,sortKey),y=val(b,sortKey);return (x>y?1:x<y?-1:0)*sortDir});
  const max=Math.max(1,...rows.map(g=>g.count)), total=rows.reduce((s,g)=>s+g.count,0);
  $('totals').innerHTML=`<span><b>${total}</b> lines</span><span><b>${rows.length}</b> distinct patterns</span><span><b>${new Set(rows.map(g=>g.etype)).size}</b> types</span>`;
  $('count').textContent=`${total} matching`;
  const list=o=>Object.entries(o).slice(0,4).map(([k,n])=>`${esc(k)} <span style="opacity:.6">(${n})</span>`).join('<br>')+(Object.keys(o).length>4?`<br>+${Object.keys(o).length-4} more`:'');
  const col={error:'--err',suspect:'--sus',warning:'--warn',info:'--info',platform:'--plat'};
  $('sumBody').innerHTML=rows.map(g=>`<tr class="click" data-i="${sumRows.indexOf(g)}">
    <td class="num"><span class="bar" style="width:${Math.max(3,60*g.count/max)}px;background:var(${col[g.level]||'--err'})"></span>${g.count}</td>
    <td>${g.regression?'<span class="badge b-reg">REGRESSION</span>':''}${g.is_new?'<span class="badge b-new">NEW</span>':''}${g.is_spike?`<span class="badge b-spike">SPIKE ×${g.spike_ratio}</span>`:''}${g.mute&&!g.regression?`<span class="badge b-${g.mute}">${g.mute.toUpperCase()}</span>`:''}<br>
      <b>${esc(g.etype)}</b><br><span class="small" style="color:var(--mute)">${esc(g.level)}${g.hint?` · ${esc(g.hint)}`:''}</span>
      ${g.note?`<div class="note">📝 ${esc(g.note)}${g.ignore_below?` (ignore under ${g.ignore_below}/day)`:''}</div>`:''}${aiLine(g.ai)}
      <div>${g.mute?`<button class="act" data-a="unmute">unmute</button>`:'<button class="act" data-a="muted">mute</button><button class="act" data-a="fixed">mark fixed</button>'}<button class="act" data-a="note">note</button></div></td>
    <td class="sig" title="${esc(g.sample)}">${highlight(g.sig||'—',g.hint)}</td>
    <td class="small">${list(g.profiles)}</td><td class="small">${list(g.log_groups)}</td>
    <td class="small">${new Date(g.first_seen).toLocaleString()}</td>
    <td class="small">${new Date(g.last_seen).toLocaleString()}<br>${ago(g.last_seen)}</td></tr>`).join('')
    || `<tr><td colspan="7" class="empty">Nothing in this range.</td></tr>`;
  document.querySelectorAll('#sumBody tr.click').forEach(tr=>{tr.onclick=()=>{const g=sumRows[tr.dataset.i];
    setSig({etype:g.etype,sig:g.sig}); show(g.level==='error'?'errors':g.level==='suspect'?'suspect':'all')};
    tr.querySelectorAll('.act').forEach(b=>b.onclick=ev=>{ev.stopPropagation(); const g=sumRows[tr.dataset.i], prof=$('fProfile').value;
      b.dataset.a==='note'?editNote(g.etype,g.sig,prof):b.dataset.a==='unmute'?unmute(g.etype,g.sig,prof):mute(g.etype,g.sig,prof,b.dataset.a)})});
}
async function loadVolume(){
  const p=params(false); p.delete('level');
  const rows=await getJ('/api/volume?'+p), lv=['error','suspect','warning','info','platform'];
  const col={error:'--err',suspect:'--sus',warning:'--warn',info:'--info',platform:'--plat'};
  const tot=k=>rows.reduce((s,g)=>s+g[k],0);
  $('vtotals').innerHTML=`<span><b>${tot('total')}</b> lines</span><span><b>${rows.length}</b> log groups</span><span><b>${tot('error')}</b> errors</span><span><b>${tot('suspect')}</b> hidden errors?</span>`;
  $('count').textContent=`${rows.length} log groups`;
  $('volBody').innerHTML=rows.map((g,i)=>`<tr class="click" data-g="${esc(g.group)}" data-p="${esc(g.profile)}">
    <td>${esc(g.profile)}</td><td class="sig">${esc(g.group)}</td><td class="num"><b>${g.total}</b></td>
    <td><div class="stack">${lv.map(l=>g[l]?`<span title="${l}: ${g[l]}" style="width:${100*g[l]/g.total}%;background:var(${col[l]})"></span>`:'').join('')}</div></td>
    ${lv.map(l=>`<td class="num">${g[l]||''}</td>`).join('')}<td class="small">${ago(g.last)}</td></tr>`).join('')
    || `<tr><td colspan="10" class="empty">Nothing in this range.</td></tr>`;
  document.querySelectorAll('#volBody tr.click').forEach(tr=>tr.onclick=()=>{$('fProfile').value=tr.dataset.p; setGrp(tr.dataset.g); show('all')});
}
async function counts(){
  const p=params(false); p.delete('level'); p.set('limit','0');
  const n=async lv=>{const q=new URLSearchParams(p); if(lv)q.set('level',lv); return (await getJ('/api/events?'+q)).total};
  const [e,s,a]=await Promise.all([n('error'),n('suspect'),n('error,suspect,warning,info')]);
  $('n-error').textContent=e; $('n-suspect').textContent=s; $('n-all').textContent=a;
}
async function refresh(newIds,force){
  if(busy)return; busy=true;
  try{
    const HEAVY={clients:loadClients,runs:loadRuns,trace:pollTrace,volume:loadVolume,deploys:loadDeploys,partners:loadPartners,schedules:loadSchedules,inventory:loadInventory,apis:loadApis,digest:loadDigest};
    if(tab==='patterns'||HEAVY[tab]){
      if(force||Date.now()-lastHeavy>30000){ lastHeavy=Date.now();
        tab==='patterns'?(sumRows=await getJ('/api/summary?'+params(false)),renderSummary()):await HEAVY[tab](); }
    } else await loadFeed(newIds);
    await counts();
  }catch(e){} busy=false;
}
const VERDICT={fixed:['--ok','fixed'],better:['--ok','better'],clean:['--ok','clean'],'no change':['--mute','no change'],
  'too early':['--mute','too early'],worse:['--err','worse'],'new errors':['--err','new errors']};
async function loadDeploys(){
  const p=new URLSearchParams(); if($('fProfile').value)p.set('profile',$('fProfile').value);
  p.set('hours',$('fHours').value||'8760'); p.set('window_hours',$('dWin').value);
  const rows=await getJ('/api/deploys?'+p);
  $('count').textContent=`${rows.length} deploys`;
  const fmt=(x,per)=>per==='invocation'?(100*x).toFixed(1)+'%':x.toFixed(1)+'/h';
  $('depBody').innerHTML=rows.map((r,i)=>{const d=r.deploy, v=VERDICT[r.verdict]||['--mute',r.verdict];
    const ch=r.rate_before?Math.round(100*(r.rate_after-r.rate_before)/r.rate_before):null;
    return `<tr class="click" data-i="${i}">
    <td class="small">${new Date(d.deployed_at).toLocaleString()}<br>${ago(d.deployed_at)}</td><td>${esc(d.profile)}</td>
    <td class="sig">${esc(d.function)}</td><td class="small">${esc(d.kind)}${d.label?`<br><b style="color:var(--fg)">${esc(d.label)}</b>`:''}</td>
    <td class="num">${r.before.invocations} → ${r.after.invocations}</td>
    <td class="num">${r.before.error+r.before.suspect} → ${r.after.error+r.after.suspect}${r.before_loaded?'':'<br><span class="small" title="Older history not loaded; pick a longer range or ask Claude to check this deploy">before not loaded</span>'}</td>
    <td class="num small">${fmt(r.rate_before,r.rate_per)} → ${fmt(r.rate_after,r.rate_per)}${ch===null?'':`<br>${ch>0?'+':''}${ch}%`}</td>
    <td class="small">${r.new_patterns?`<span style="color:var(--err)">${r.new_patterns} new</span> `:''}${r.gone_patterns?`<span style="color:var(--ok)">${r.gone_patterns} gone</span>`:''}</td>
    <td><span class="chip" style="border-color:var(${v[0]});color:var(${v[0]})">${v[1]}</span><br><span class="small">${r.after_hours<r.window_hours?r.after_hours.toFixed(1)+'h after so far':''}</span></td></tr>`}).join('')
    || `<tr><td colspan="9" class="empty">No deploys recorded in this range yet. Deploys are detected from now on (plus each function's latest one).</td></tr>`;
  document.querySelectorAll('#depBody tr.click').forEach(tr=>tr.onclick=()=>{const d=rows[tr.dataset.i].deploy;
    $('fProfile').value=d.profile; setGrp(d.log_group); show('all')});
}
const SCHED={'not running':'--err','missed runs':'--err','target missing':'--err','last run failed':'--sus','disabled':'--mute','not enough data':'--mute','not checked':'--mute',ok:'--ok'};
const when=ms=>ms?new Date(ms).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
const until=ms=>{if(!ms)return'';const s=(ms-Date.now())/1000;return s<5400?'in '+Math.round(s/60)+'m':s<129600?'in '+Math.round(s/3600)+'h':'in '+Math.round(s/86400)+'d'};
async function infraNote(){
  const st=await getJ('/api/infra/status'+($('fProfile').value?'?profile='+encodeURIComponent($('fProfile').value):''));
  const pend=st.filter(x=>!x.scanned_at).length, errs=st.flatMap(x=>Object.entries(x.errors).map(([k,v])=>`${x.profile}: ${k} (${v})`));
  return (pend?`<b>Scanning ${pend} account(s)…</b> `:'')+(errs.length?`<span title="${esc(errs.join('\n'))}" style="color:var(--sus)">${errs.length} sections unavailable (hover)</span>`:'');
}
async function loadSchedules(){
  const p=new URLSearchParams(); if($('fProfile').value)p.set('profile',$('fProfile').value);
  const [rows,note]=await Promise.all([getJ('/api/infra/schedules?'+p),infraNote()]); $('infraNote').innerHTML=note;
  const bad=rows.filter(r=>['not running','missed runs','target missing','last run failed'].includes(r.status)).length;
  $('n-sched').textContent=bad||''; $('count').textContent=`${rows.length} schedules, ${bad} with problems`;
  $('schBody').innerHTML=rows.map((r,i)=>`<tr class="click" data-i="${i}"><td><span class="chip" style="border-color:var(${SCHED[r.status]||'--mute'});color:var(${SCHED[r.status]||'--mute'})">${esc(r.status)}</span></td>
    <td>${esc(r.profile)}</td><td class="sig">${esc(r.name)}<br><span class="small">${r.source==='rule'?'EventBridge rule':'Scheduler'}${r.dlq?' · DLQ':''}</span></td>
    <td class="small">${esc(r.expression)}${r.tz&&r.tz!=='UTC'?'<br>'+esc(r.tz):''}</td><td class="sig small">${esc(r.function||r.state_machine||r.target||'')}</td>
    <td class="num">${r.expected??'—'}</td><td class="num">${r.actual??'—'}</td><td class="small">${when(r.last_run)}${r.last_run_status?'<br>'+esc(r.last_run_status):''}</td>
    <td class="small">${when(r.next_run)}<br>${until(r.next_run)}</td><td class="small">${esc(r.detail)}${r.missed&&r.missed.length?'<br>missed: '+r.missed.slice(-3).map(when).join(', '):''}</td></tr>`).join('')
    ||`<tr><td colspan="10" class="empty">No schedules found yet (the first scan takes a minute after start).</td></tr>`;
  document.querySelectorAll('#schBody tr.click').forEach(tr=>tr.onclick=()=>{const r=rows[tr.dataset.i]; if(!r.function&&!r.state_machine)return;
    $('fProfile').value=r.profile; setGrp(r.function?'/aws/lambda/'+r.function:'stepfunctions:'+r.state_machine); show('all')});
}
async function loadInventory(){
  const v=$('invView').value, only=$('invFlagged').checked, p=new URLSearchParams(); if($('fProfile').value)p.set('profile',$('fProfile').value);
  const RS={deprecated:'--err',soon:'--sus',ok:'--ok',unknown:'--mute',container:'--mute'};
  if(v==='functions'){
    let rows=await getJ('/api/infra/functions?'+p); if(only)rows=rows.filter(r=>r.flags.length);
    $('count').textContent=`${rows.length} functions`;
    $('invBody').innerHTML=`<table><thead><tr><th>Profile</th><th>Function</th><th>Runtime</th><th>Layers</th><th class="num">Runs 24h</th><th class="num">Avg / max</th><th class="num">Timeout used</th><th class="num">Memory used</th><th class="num">Est. $/30d</th><th>Needs attention</th></tr></thead><tbody>`+
      rows.map((r,i)=>`<tr class="click" data-i="${i}"><td>${esc(r.profile)}</td><td class="sig">${esc(r.name)}${r.secrets.length?`<br><span class="small">secrets: ${esc(r.secrets.join(', '))}</span>`:''}</td>
      <td title="${esc(r.runtime_text)}"><span style="color:var(${RS[r.runtime_status]})">${esc(r.runtime||'?')}</span><br><span class="small">${esc(r.runtime_text)}</span></td>
      <td class="small">${r.layers.map(l=>esc(l.split(':').slice(-2).join(':'))).join('<br>')||'—'}</td><td class="num">${r.invocations_24h}${r.from_logs?'':'<br><span class="small">metrics</span>'}</td>
      <td class="num small">${r.avg_ms??'—'} / ${r.max_ms??'—'} ms</td><td class="num">${r.timeout_pct??'—'}${r.timeout_pct!=null?'%':''}<br><span class="small">of ${r.timeout}s</span></td>
      <td class="num">${r.mem_pct??'—'}${r.mem_pct!=null?'%':''}<br><span class="small">${r.mem_used_mb??'—'} of ${r.memory} MB</span></td><td class="num">${r.cost_30d}</td>
      <td class="small" style="color:var(--err)">${r.flags.map(esc).join('<br>')}${r.outdated_layers.length?'<br><span style="color:var(--mute)">'+esc(r.outdated_layers.join(', '))+'</span>':''}</td></tr>`).join('')+'</tbody></table>';
    document.querySelectorAll('#invBody tr.click').forEach(tr=>tr.onclick=()=>{const r=rows[tr.dataset.i]; $('fProfile').value=r.profile; setGrp(r.log_group); show('all')});
  } else if(v==='layers'){
    let rows=await getJ('/api/infra/layers?'+p); if(only)rows=rows.filter(r=>r.outdated);
    $('count').textContent=`${rows.length} layer versions in use`;
    $('invBody').innerHTML=`<table><thead><tr><th>Profile</th><th>Layer</th><th>Version</th><th>Used by</th><th>Packages</th></tr></thead><tbody>`+
      rows.map(r=>`<tr><td>${esc(r.profile)}</td><td class="sig">${esc(r.layer)}</td><td>${r.version}${r.outdated?` <span class="badge b-spike">latest ${r.latest}</span>`:''}</td>
      <td class="small">${r.functions.map(esc).join('<br>')}</td><td class="small sig">${Object.entries(r.packages).map(([k,v])=>esc(k)+' '+esc(v)).join(', ')||esc(r.note||'—')}</td></tr>`).join('')+'</tbody></table>';
  } else if(v==='secrets'){
    let rows=await getJ('/api/infra/secrets?'+p); if(only)rows=rows.filter(r=>r.flags.length);
    $('count').textContent=`${rows.length} secrets`;
    $('invBody').innerHTML=`<table><thead><tr><th>Profile</th><th>Secret</th><th>Last changed</th><th>Last read</th><th>Rotation</th><th>Used by</th><th>Notes</th></tr></thead><tbody>`+
      rows.map(r=>`<tr><td>${esc(r.profile)}</td><td class="sig">${esc(r.name)}</td><td class="small">${when(r.last_changed)}<br>${r.last_changed?ago(r.last_changed):''}</td>
      <td class="small">${r.last_accessed?new Date(r.last_accessed).toLocaleDateString():'—'}</td><td class="small">${r.rotation?'on'+(r.next_rotation?' · next '+new Date(r.next_rotation).toLocaleDateString():''):'off'}</td>
      <td class="small">${r.functions.map(esc).join('<br>')||'<span style="color:var(--mute)">not found in env vars</span>'}</td>
      <td class="small" style="color:var(--err)">${r.flags.map(esc).join('<br>')}${r.related.length?'<br>'+r.related.map(esc).join('<br>'):''}</td></tr>`).join('')+'</tbody></table>';
  } else {
    const rows=await getJ('/api/infra/cost?'+p); const tot=rows.reduce((s,r)=>s+r.cost_30d,0);
    $('count').textContent=`~$${tot.toFixed(2)} / 30 days`;
    $('invBody').innerHTML=`<p class="small">Estimate from the last 24h of Lambda runs (billed duration × memory + requests, list prices, no free tier) × 30. Lambda compute only.</p><table><thead><tr><th>Profile</th><th class="num">Est. $/30d</th><th>Most expensive functions</th></tr></thead><tbody>`+
      rows.map(r=>`<tr><td>${esc(r.profile)}</td><td class="num"><b>${r.cost_30d.toFixed(2)}</b></td><td class="small">${r.top.filter(t=>t.cost_30d>0).slice(0,5).map(t=>`${esc(t.name)} $${t.cost_30d} (${t.invocations_24h} runs/day, ${t.memory} MB, avg ${t.avg_ms} ms)`).join('<br>')||'—'}</td></tr>`).join('')+'</tbody></table>';
  }
}
async function loadApis(){
  const p=new URLSearchParams(); if($('fProfile').value)p.set('profile',$('fProfile').value);
  const r=await getJ('/api/infra/apis?'+p); const firing=r.alarms.filter(a=>a.state==='ALARM');
  $('count').textContent=`${r.apis.length} API stages, ${firing.length} alarms firing`;
  $('apiBody').innerHTML=`<table><thead><tr><th>Profile</th><th>API</th><th>Stage</th><th class="num">Requests</th><th class="num">4xx</th><th class="num">5xx</th><th class="num">p99</th><th>Routes → Lambda</th><th>Flags</th></tr></thead><tbody>`+
    r.apis.map(a=>`<tr><td>${esc(a.profile)}</td><td class="sig">${esc(a.api)}<br><span class="small">${esc(a.kind)}</span></td><td>${esc(a.stage)}</td><td class="num">${a.requests}</td><td class="num">${a.e4}</td>
    <td class="num" style="${a.e5?'color:var(--err);font-weight:700':''}">${a.e5}</td><td class="num">${a.p99_ms!=null?a.p99_ms+' ms':'—'}</td>
    <td class="small sig">${a.routes.slice(0,6).map(x=>esc(x.route)+(x.function?' → '+esc(x.function):'')).join('<br>')}${a.routes.length>6?`<br>+${a.routes.length-6} more`:''}</td><td class="small" style="color:var(--err)">${a.flags.map(esc).join('<br>')}</td></tr>`).join('')
    +(r.apis.length?'':'<tr><td colspan="9" class="empty">No API Gateway APIs found.</td></tr>')+`</tbody></table>
    <h3 style="font-size:14px;margin:18px 0 8px">CloudWatch alarms (${firing.length} firing of ${r.alarms.length})</h3><table><thead><tr><th>State</th><th>Profile</th><th>Alarm</th><th>Metric</th><th>Since</th><th>Reason</th></tr></thead><tbody>`+
    r.alarms.sort((a,b)=>(a.state==='ALARM'?0:1)-(b.state==='ALARM'?0:1)).slice(0,200).map(a=>`<tr><td><span class="chip" style="${a.state==='ALARM'?'border-color:var(--err);color:var(--err)':''}">${esc(a.state)}</span></td><td>${esc(a.profile)}</td><td class="sig">${esc(a.name)}</td><td class="small">${esc(a.metric)} ${esc(Object.values(a.dims).join(' '))}</td><td class="small">${when(a.since)}</td><td class="small">${esc(a.reason)}</td></tr>`).join('')
    +(r.alarms.length?'':'<tr><td colspan="6" class="empty">No CloudWatch alarms.</td></tr>')+'</tbody></table>';
}
async function loadDigest(){
  const p=new URLSearchParams(); if($('fProfile').value)p.set('profile',$('fProfile').value);
  const r=await getJ('/api/digest?'+p); $('count').textContent=`${r.digest.filter(d=>!d.ok).length} clients need attention`;
  $('digBody').innerHTML=r.digest.map(d=>`<div class="ev ${d.ok?'info':'error'}" style="padding:10px 14px"><div class="meta"><b style="font-size:14px">${esc(d.profile)}</b><span>${d.errors_24h} errors · ${d.hidden_24h} hidden errors · ~$${d.cost_24h} Lambda (24h)</span>${d.ok?'<span style="color:var(--ok)">all quiet</span>':''}</div>`+
    d.sections.map(s=>`<div style="margin-top:6px"><b class="small" style="color:var(--fg)">${esc(s.title)}</b><ul style="margin:2px 0 0 18px;padding:0;font-size:13px">${s.items.map(i=>`<li>${esc(i)}</li>`).join('')}</ul></div>`).join('')+'</div>').join('')
    +`<p class="small"><button class="act" id="copyDig">copy as text</button></p>`;
  $('copyDig').onclick=()=>navigator.clipboard.writeText(r.text);
}
function chg(cur,prev){
  if(prev==null)return '<span class="chg">—</span>';
  if(!prev&&!cur)return '<span class="chg">same</span>';
  if(!prev)return `<span class="chg up">new (was 0)</span>`;
  const p=Math.round(100*(cur-prev)/prev); const cls=p>=20&&cur-prev>2?'up':p<=-20&&prev-cur>2?'down':'';
  return `<span class="chg ${cls}" title="previous period: ${prev}">${p>0?'▲ +':p<0?'▼ ':''}${p}% vs ${prev}</span>`;
}
function winTable(w){
  if(!w)return '';
  const cols=['1h','6h','24h'];
  return `<table class="win"><tr><th></th>${cols.map(k=>`<th style="text-align:right">last ${k}</th>`).join('')}</tr>
   <tr><th>Errors</th>${cols.map(k=>`<td><b>${w[k].errors}</b>${chg(w[k].errors,w[k].prev_errors)}</td>`).join('')}</tr>
   <tr><th>Hidden</th>${cols.map(k=>`<td><b>${w[k].hidden}</b>${chg(w[k].hidden,w[k].prev_hidden)}</td>`).join('')}</tr></table>`;
}
function depList(ds){
  if(!ds||!ds.length)return '';
  const V={fixed:'--ok',better:'--ok',clean:'--ok','no change':'--mute','too early':'--mute','before not loaded':'--mute',worse:'--err','new errors':'--err'};
  const r=(x,per)=>per==='invocation'?(100*x).toFixed(1)+'%/run':x.toFixed(1)+'/h';
  return `<div class="deps"><div style="border:0;color:var(--mute)">Deploys in the last 24h (errors+hidden, ${ds[0].window_hours}h before vs after)</div>`+ds.map(d=>`<div title="${esc(d.label||'')}"><span class="fn">${esc(d.function)}</span><span class="small">${ago(d.at)}</span>
    <span class="small">${d.before_loaded?`${r(d.rate_before,d.rate_per)} → ${r(d.rate_after,d.rate_per)}`:''}</span>
    <span style="color:var(${V[d.verdict]||'--mute'});font-weight:600">${esc(d.verdict)}</span></div>`).join('')+'</div>';
}
async function loadClients(){
  const cards=await getJ('/api/ops/clients'); const red=cards.filter(c=>c.status==='red').length, amb=cards.filter(c=>c.status==='amber').length;
  $('count').textContent=`${red} red · ${amb} amber · ${cards.length-red-amb} green`;
  const tr=(a,b)=>b==null?'':a>b*1.2&&a-b>2?`<span class="up">▲</span>`:a<b*0.8&&b-a>2?`<span class="down">▼</span>`:'';
  $('clientCards').innerHTML=cards.map((c,i)=>`<div class="cc ${c.status}"><h3 data-i="${i}"><span style="opacity:.55;font-weight:400;margin-right:6px">#${i+1}</span>${esc(c.profile)}<span class="pill ${c.status}">${c.status==='red'?'needs attention':c.status==='amber'?'watch':'healthy'}</span></h3>
    ${winTable(c.windows)}
    <div class="kv"><div>Runs 24h<b>${c.runs_24h}${c.failed_runs_24h?` <span class="up" style="font-size:12px">${c.failed_runs_24h} failed</span>`:''}</b></div>
     <div>Records 24h<b>${c.records_24h||'—'}${c.records_usual?` <span class="small" style="color:var(--mute)">/ ~${c.records_usual}</span>`:''}</b></div>
     <div>Schedules<b>${c.sched_total?`${c.sched_ok}/${c.sched_total} ok`:'—'}</b></div></div>
    ${depList(c.recent_deploys)}
    ${c.reasons.length?`<ul>${c.reasons.slice(0,6).map(r=>`<li>${esc(r)}</li>`).join('')}${c.reasons.length>6?`<li>+${c.reasons.length-6} more</li>`:''}</ul>`:'<div class="small" style="color:var(--ok)">Nothing needs attention.</div>'}
    <div class="foot"><span>~$${c.cost_30d}/30d</span>${c.last_deploy&&!(c.recent_deploys||[]).length?`<span>Last deploy: ${esc(c.last_deploy.function)} ${ago(c.last_deploy.at)}</span>`:''}
     <a data-go="errors" data-p="${esc(c.profile)}">errors</a><a data-go="runs" data-p="${esc(c.profile)}">runs</a><a data-go="schedules" data-p="${esc(c.profile)}">schedules</a>
     <a href="/report?profile=${encodeURIComponent(c.profile)}&days=7" target="_blank">weekly report ↗</a></div></div>`).join('')
    ||'<div class="empty">Loading clients… (first scan takes about a minute)</div>';
  document.querySelectorAll('#clientCards h3').forEach(h=>h.onclick=()=>{$('fProfile').value=cards[h.dataset.i].profile; show('errors')});
  document.querySelectorAll('#clientCards a[data-go]').forEach(a=>a.onclick=()=>{$('fProfile').value=a.dataset.p; show(a.dataset.go)});
}
let runGroups=[];
async function loadRuns(){
  const pf=$('fProfile').value, p=new URLSearchParams(); if(pf)p.set('profile',pf);
  const [groups,nothing]=await Promise.all([getJ('/api/ops/groups?'+p),getJ('/api/ops/nothing?'+p)]);
  $('nothingBox').innerHTML=nothing.length?`<div class="ev suspect" style="padding:8px 12px"><b>Possibly did nothing / went quiet</b><ul style="margin:4px 0 0 18px;padding:0;font-size:13px">${nothing.map(d=>`<li><b>${esc(d.profile)}</b> · ${esc(d.group)} — ${esc(d.kind)}: ${esc(d.detail)}</li>`).join('')}</ul></div>`:'';
  const cur=$('runGroup').value; runGroups=groups;
  $('runGroup').innerHTML=groups.map((g,i)=>`<option value="${i}">${esc(g.profile)} · ${esc(g.function||g.group)}</option>`).join('')||'<option>No Lambda runs loaded yet</option>';
  if(cur&&groups[cur])$('runGroup').value=cur;
  const g=groups[$('runGroup').value]; if(!g){$('runBody').innerHTML='';return}
  let runs=await getJ('/api/ops/runs?'+new URLSearchParams({profile:g.profile,region:g.region,grp:g.group,hours:$('fHours').value||24}));
  if($('runProblems').checked)runs=runs.filter(r=>r.outcome!=='ok');
  $('count').textContent=`${runs.length} runs`;
  const OC={ok:'--ok',timeout:'--err',failed:'--err','hidden errors':'--sus','no records':'--sus'};
  $('runBody').innerHTML=runs.slice(0,400).map((r,i)=>`<tr class="click" data-i="${i}"><td class="small">${new Date(r.end).toLocaleString()}${r.cold_start?' <span class="badge b-muted">cold</span>':''}</td>
    <td class="num">${(r.duration_ms/1000).toFixed(1)}s</td><td class="num">${r.records??'—'}</td><td class="num">${r.errors||''}</td><td class="num">${r.hidden||''}</td>
    <td class="num small">${r.mem_used}/${r.memory} MB</td><td><span class="chip" style="border-color:var(${OC[r.outcome]});color:var(${OC[r.outcome]})">${esc(r.outcome)}</span></td>
    <td class="small sig">${r.first_problem?esc(r.first_problem.line.slice(0,160)):''}</td></tr><tr class="run-lines" id="rl${i}"><td colspan="8"></td></tr>`).join('')
    ||'<tr><td colspan="8" class="empty">No runs in this range.</td></tr>';
  document.querySelectorAll('#runBody tr.click').forEach(tr=>tr.onclick=async()=>{const r=runs[tr.dataset.i], row=$('rl'+tr.dataset.i);
    if(row.classList.toggle('on')&&!row.dataset.loaded){row.dataset.loaded=1;
      const full=await getJ('/api/ops/run?'+new URLSearchParams({profile:g.profile,region:g.region,grp:g.group,request_id:r.request_id}));
      row.firstElementChild.innerHTML=`<div class="small">Request ${esc(r.request_id)} · stream ${esc(r.stream)} · <button class="act" id="sum${tr.dataset.i}">🤖 summarize this run</button></div><div id="sumbox${tr.dataset.i}"></div><pre style="display:block;max-height:420px">${(full.lines_list||[]).map(l=>esc(new Date(l.ts).toLocaleTimeString()+'  ['+l.level+']  '+l.message)).join('\n')||'(no lines captured for this run)'}</pre>`;
      $('sum'+tr.dataset.i).onclick=ev=>{ev.stopPropagation(); aiSummarize({kind:'run',profile:g.profile,region:g.region,grp:g.group,request_id:r.request_id},$('sumbox'+tr.dataset.i))}}});
}
let trJob=null,trTimer=null;
async function startTrace(){
  const term=$('trTerm').value.trim(); if(!term)return;
  const p=new URLSearchParams({term,days:$('trDays').value,sfn:$('trSfn').checked?'1':'0'}); if($('fProfile').value)p.set('profile',$('fProfile').value);
  trJob=(await getJ('/api/ops/trace/start?'+p)).id; $('trBody').innerHTML=''; pollTrace();
}
async function pollTrace(){
  clearTimeout(trTimer); if(!trJob)return;
  const j=await getJ('/api/ops/trace?id='+trJob);
  $('trStatus').textContent=j.status==='running'?`searching… ${j.accounts_done}/${j.accounts} accounts, ${j.groups_searched} log groups, ${j.hits.length} lines found`:
    `done — ${j.hits.length} lines in ${j.timeline.length} runs, ${j.sfn.length} Step Functions executions`;
  $('count').textContent=`${j.hits.length} lines`;
  const sf=j.sfn.length?`<h4 style="margin:6px 0">Step Functions executions with “${esc(j.term)}” in their ${'input/output'}</h4><table><tbody>${j.sfn.map(e=>`<tr><td class="small">${when(e.start)}</td><td>${esc(e.profile)}</td><td class="sig">${esc(e.state_machine)} / ${esc(e.execution)}</td><td><span class="chip" style="${e.status==='SUCCEEDED'?'':'border-color:var(--err);color:var(--err)'}">${esc(e.status)}</span></td><td><a target="_blank" href="https://${e.region}.console.aws.amazon.com/states/home?region=${e.region}#/v2/executions/details/${encodeURIComponent(e.arn)}">open ↗</a></td></tr>`).join('')}</tbody></table>`:'';
  $('trBody').innerHTML=sf+(j.timeline.length?`<h4 style="margin:12px 0 6px">Timeline (oldest first)</h4>`:'')+j.timeline.map(g=>`<div style="margin-bottom:10px"><div class="meta"><b>${when(g.start)}</b><span>${esc(g.profile)}</span><span class="sig">${esc(g.group)}</span><span class="lvl" style="color:var(${g.outcome==='error'?'--err':g.outcome==='ok'?'--ok':'--sus'})">${esc(g.outcome)}</span><span>${g.lines.length} line(s)</span></div>
    <div class="tl">${g.lines.map(l=>`<div class="ev ${l.level==='error'?'error':l.level}" style="padding:4px 10px"><span class="small">${new Date(l.ts).toLocaleTimeString()} · ${esc(l.level)}${l.source==='cloudwatch'?' · from CloudWatch':''}</span><div class="sig" style="font-size:12px;white-space:pre-wrap">${highlight(l.message.slice(0,1200),j.term)}</div></div>`).join('')}</div></div>`).join('')
    +(j.partial.length||j.errors.length?`<p class="small" style="color:var(--sus)">${esc([...j.partial,...j.errors].slice(0,8).join(' · '))}</p>`:'');
  if(j.status==='running')trTimer=setTimeout(pollTrace,2000);
}
let NOTES={}, AITRIAGE={};
async function loadTriage(){try{AITRIAGE=await getJ('/api/ai/triage_map')}catch(e){}}
async function loadNotes(){try{(await getJ('/api/ops/notes')).forEach(n=>NOTES[n.etype+'|'+n.sig+'|'+n.profile]=n)}catch(e){}}
const noteFor=(e,p)=>NOTES[e.etype+'|'+e.sig+'|'+(p||'')]||NOTES[e.etype+'|'+e.sig+'|'];
async function editNote(etype,sig,profile){
  const cur=NOTES[etype+'|'+sig+'|'+(profile||'')]||{}; const note=prompt('Note for this error pattern (shown here and to Claude). Leave empty to remove.',cur.note||'');
  if(note===null)return; const th=prompt('Ignore when fewer than N per day? (optional number)',cur.ignore_below||'');
  await getJ('/api/ops/note?'+new URLSearchParams({etype,sig,profile:profile||'',note,ignore_below:th||''})); NOTES={}; await loadNotes(); refresh(null,true);
}
async function showCode(e,box){
  box.innerHTML='<div class="small">Looking up the code…</div>';
  const r=await getJ('/api/ops/code?id='+encodeURIComponent(e.id));
  if(r.error){box.innerHTML=`<div class="small">${esc(r.error)}</div>`;return}
  if(!r.mapped){box.innerHTML=`<div class="small">Couldn't find the repo folder for <b>${esc(r.function||e.group)}</b>. Add it to repos.json next to the dashboard: {"${esc(r.function||'function-name')}": "C:\\path\\to\\its\\folder"}</div>`;return}
  const own=r.frames.filter(f=>f.path);
  box.innerHTML=`<div class="small">Repo: ${esc(r.mapped.repo)} (${esc(r.mapped.how)})</div>`+(own.length?own.map(f=>`<h4>${esc(f.path)} line ${f.line}${f.func?' in '+esc(f.func):''} — <a href="${f.vscode}">open in VS Code</a>${f.github?` · <a href="${f.github}" target="_blank">GitHub ↗</a>`:''}</h4><pre>${esc(f.snippet||'')}</pre>`).join(''):'<div class="small">No stack-trace lines from your own code in this error.</div>');
}
async function loadPartners(){
  const p=params(false); p.delete('level'); p.delete('hide_muted');
  const rows=await getJ('/api/partners?'+p);
  $('count').textContent=`${rows.length} partner problems`;
  const list=o=>Object.entries(o).slice(0,3).map(([k,n])=>`${esc(k)} <span style="opacity:.6">(${n})</span>`).join('<br>')+(Object.keys(o).length>3?`<br>+${Object.keys(o).length-3} more`:'');
  $('partBody').innerHTML=rows.map((r,i)=>`<tr class="click" data-i="${i}">
    <td><b>${esc(r.service)}</b>${r.several_clients_now?'<br><span class="badge b-reg">SEVERAL CLIENTS NOW</span>':''}</td><td>${esc(r.problem)}</td>
    <td class="num">${r.count}</td><td class="num">${r.last_hour||''}</td><td class="small">${list(r.profiles)}</td><td class="small">${list(r.functions)}</td>
    <td class="small">${ago(r.last)}</td><td class="sig" title="${esc(r.sample)}">${esc(r.sample.split('\n')[0].slice(0,140))}</td></tr>`).join('')
    || `<tr><td colspan="8" class="empty">No partner API problems in this range.</td></tr>`;
  document.querySelectorAll('#partBody tr.click').forEach(tr=>tr.onclick=()=>{const r=rows[tr.dataset.i];
    const prof=Object.keys(r.profiles); if(prof.length===1)$('fProfile').value=prof[0];
    const fn=Object.keys(r.functions); if(fn.length===1)setGrp(fn[0]);
    $('fLevel').value='error,suspect'; $('fLevel').dataset.touched=1; show('all')});
}
function renderChips(){
  const c=[]; if(sigFilter)c.push(`<span class="chip filter" data-x="sig">${esc(sigFilter.etype)}: ${esc((sigFilter.sig||'').slice(0,50))} ✕</span>`);
  if(grpFilter)c.push(`<span class="chip filter" data-x="grp">${esc(grpFilter)} ✕</span>`);
  $('chips').innerHTML=c.join(' ');
  document.querySelectorAll('#chips .filter').forEach(el=>el.onclick=()=>{el.dataset.x==='sig'?sigFilter=null:grpFilter=null;renderChips();refresh(null,true)});
}
function setSig(s){sigFilter=s;renderChips()}
function setGrp(g){grpFilter=g;renderChips()}
function show(v){
  tab=v; document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
  const feed=['errors','suspect','all'].includes(v);
  $('v-feed').classList.toggle('on',feed); $('v-patterns').classList.toggle('on',v==='patterns'); $('v-volume').classList.toggle('on',v==='volume'); $('v-deploys').classList.toggle('on',v==='deploys'); $('v-partners').classList.toggle('on',v==='partners');
  ['schedules','inventory','apis','digest','clients','runs','trace'].forEach(x=>$('v-'+x).classList.toggle('on',v===x));
  $('fLevel').style.display=(v==='all'||v==='patterns')?'':'none';
  if(v==='patterns'&&!$('fLevel').dataset.touched)$('fLevel').value='error,suspect';
  if(v==='all'&&!$('fLevel').dataset.touched)$('fLevel').value='error,suspect,warning,info';
  if(feed)$('feedHelp').textContent=HELP[v];
  refresh(null,true);
}
function notify(evs){
  if(!("Notification" in window)||Notification.permission!=="granted"||!evs.length)return;
  if(evs.length>3){new Notification(`${evs.length} new AWS errors`,{body:[...new Set(evs.map(e=>`${e.profile}: ${e.etype}`))].slice(0,6).join('\n')});return;}
  evs.forEach(e=>new Notification(`[${e.profile}] ${e.regression?'REGRESSION: ':e.level==='suspect'?'Hidden error? ':''}${e.etype}`,{body:(e.group.split('/').pop()+' — '+firstLine(e.message)).slice(0,200),tag:e.id}));
}
async function tick(){
  if(paused)return;
  try{
    const lv=$('notifySus').checked?'error,suspect':'error';
    const r=await getJ(`/api/new?since=${seq}&notify=${lv}`), first=seq===0; seq=r.seq;
    if(first||!r.changed)return;
    notify(r.live);
    if(r.live.length){document.title=`(${r.live.length}) AWS Error Feed`; setTimeout(()=>document.title='AWS Error Feed',8000);}
    refresh(new Set(r.live.map(e=>e.id)));
  }catch(e){}
}
function updateProfiles(){
  const sel=$('fProfile'), cur=sel.value, profs=[...knownProfiles].sort();
  if(sel.options.length===profs.length+1)return;
  sel.innerHTML='<option value="">All profiles</option>'+profs.map(p=>`<option>${esc(p)}</option>`).join(''); sel.value=cur;
}
async function aiStatus(){
  try{ const a=await getJ('/api/ai/status');
    $('aiStatus').innerHTML=a.ok?`🤖 Local model <b>${esc(a.model)}</b> ready · ${a.triaged} patterns triaged this session${a.working_on?` · now: ${esc(a.working_on)}`:''}${a.last_error?` · <span style="color:var(--sus)">${esc(a.last_error)}</span>`:''}`
      :`<span style="color:var(--mute)">🤖 Local model off — ${esc(a.detail)}</span>`;
    AI_OK=a.ok; }catch(e){}
}
let AI_OK=false;
function aiLine(t){
  if(!t)return '';
  return `<div class="ai">🤖 <b class="v-${esc(t.verdict)}">${esc(t.verdict)}</b> · ${esc(t.category)} — ${esc(t.summary)}${t.next_step?`<br><span class="small">Next: ${esc(t.next_step)}</span>`:''}<span class="small" style="opacity:.6"> (${Math.round((t.confidence||0)*100)}%, ${esc(t.model)})</span></div>`;
}
async function aiSummarize(params,box){
  if(!AI_OK){box.innerHTML='<div class="small">Local model is off (see the 🤖 line at the top).</div>';return}
  box.innerHTML='<div class="aibox">🤖 Reading the lines on your machine… (can take up to a minute)</div>';
  const r=await getJ('/api/ai/summarize?'+new URLSearchParams(params));
  if(r.error){box.innerHTML=`<div class="small">${esc(r.error)}</div>`;return}
  const poll=async()=>{const j=await getJ('/api/ai/job?id='+r.id);
    if(j.status==='running')return setTimeout(poll,2500);
    box.innerHTML=`<div class="aibox">🤖 ${esc(j.answer||j.error||'')}</div>`};
  poll();
}
let aiTick=0;
async function status(){
  if(regTick++%3===0)loadRegress();
  if(aiTick++%6===0)aiStatus();
  try{
    const s=await getJ('/api/status'), now=Date.now(), vals=Object.values(s);
    vals.forEach(v=>v.profile&&knownProfiles.add(v.profile)); updateProfiles();
    $('status').innerHTML=Object.entries(s).map(([k,v])=>{
      if(!v.ok) return `<span class="chip bad" title="${esc(v.error||'starting…')}">● ${esc(k)} — ${esc((v.error||'starting…').slice(0,90))}</span>`;
      const pct=v.backfilling?Math.round(100*(now-v.covered_from)/Math.max(1,now-v.target)):100;
      return `<span class="chip good" title="${v.groups} log groups · history loaded back to ${day(v.covered_from)}${v.sampled?` · ${v.sampled} times a log group was too busy to pull every line (errors still pulled)`:''}"><b>●</b> ${esc(k)} (${esc(v.account||'?')}) · ${v.groups} groups${v.state_machines?` · ${v.state_machines} state machines`:''}${v.lambdas?` · ${v.lambdas} lambdas`:''}${v.lambda_error?` · <span style="color:var(--sus)" title="${esc(v.lambda_error)}">deploys: ${/AccessDenied/.test(v.lambda_error)?'no permission':'error'}</span>`:''}${v.sfn_error?` · <span style="color:var(--sus)" title="${esc(v.sfn_error)}">step functions: ${/AccessDenied/.test(v.sfn_error)?'no permission':'error'}</span>`:''}${v.backfilling?` · loading ${pct}%`:''}${v.sampled?' · sampled':''}</span>`}).join('');
    const h=$('fHours').value, el=$('hist'), ok=vals.filter(v=>v.ok&&v.covered_from);
    if(h&&ok.length){
      const want=now-h*3600e3, oldest=Math.max(...ok.map(v=>v.covered_from));
      const done=ok.filter(v=>v.covered_from<=want+60e3).length;
      if(done<ok.length){
        const pct=Math.round(100*ok.reduce((s,v)=>s+Math.min(1,(now-v.covered_from)/(now-want)),0)/ok.length);
        el.style.display='block';
        el.innerHTML=`Loading history for this range<span class="track"><span class="fill" style="width:${pct}%"></span></span>${pct}% · ${done}/${ok.length} accounts done · all accounts loaded back to ${day(oldest)}`;
      } else el.style.display='none';
    } else el.style.display='none';
  }catch(e){}
}
function onRange(){ const h=$('fHours').value;
  if(h&&!['deploys','schedules','inventory','apis','digest','clients','trace'].includes(tab)) fetch('/api/backfill?hours='+h);   // these views don't need old logs
  refresh(null,true); status(); }
let qt; $('q').oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>refresh(null,true),300)};
$('fHours').onchange=onRange; $('runGroup').onchange=()=>refresh(null,true); $('runProblems').onchange=()=>refresh(null,true);
$('trGo').onclick=startTrace; $('trSum').onclick=()=>{if(trJob)aiSummarize({kind:'trace',id:trJob},$('trBody').insertBefore(document.createElement('div'),$('trBody').firstChild))};
loadTriage(); setInterval(loadTriage,60000); $('trTerm').onkeydown=e=>{if(e.key==='Enter')startTrace()}; loadNotes(); $('invView').onchange=()=>refresh(null,true); $('invFlagged').onchange=()=>refresh(null,true);
$('rescan').onclick=async()=>{await getJ('/api/infra/rescan'+($('fProfile').value?'?profile='+encodeURIComponent($('fProfile').value):'')); $('rescan').textContent='scanning… (about a minute)'}; $('showMuted').onchange=()=>refresh(null,true); $('pFilter').onchange=renderSummary; $('dWin').onchange=()=>refresh(null,true); $('fProfile').onchange=()=>refresh(null,true);
$('fLevel').onchange=()=>{$('fLevel').dataset.touched=1;refresh(null,true)};
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>show(b.dataset.v));
document.querySelectorAll('#v-patterns th[data-k]').forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortDir=sortKey===k?-sortDir:-1;sortKey=k;renderSummary()});
$('export').onchange=e=>{const f=e.target.value; if(!f)return; e.target.value='';
  const p=params(f.startsWith('events')); if(f==='summary.csv'&&!['patterns','all'].includes(tab))p.set('level',levelFor());
  location.href='/export/'+f+'?'+p};
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'Resume':'Pause'};
$('notif').onclick=()=>Notification.requestPermission().then(p=>$('notif').textContent=p==='granted'?'Notifications on':'Notifications blocked');
if(window.Notification&&Notification.permission==='granted')$('notif').textContent='Notifications on';
$('notifySus').checked=store.get('notifySus')==='1'; $('notifySus').onchange=e=>store.set('notifySus',e.target.checked?'1':'0');
// deep links, e.g. /?tab=suspect&profile=anew&hours=168&q=timeout&grp=/aws/lambda/x
(()=>{const u=new URLSearchParams(location.search);
  if(u.get('profile')){knownProfiles.add(u.get('profile'));updateProfiles();$('fProfile').value=u.get('profile')}
  if(u.has('hours')){const h=u.get('hours'); if(![...$('fHours').options].some(o=>o.value===h)){const o=document.createElement('option');o.value=h;o.textContent=`Last ${h} hours`;$('fHours').insertBefore(o,$('fHours').lastElementChild)} $('fHours').value=h}
  if(u.get('q'))$('q').value=u.get('q');
  if(u.get('level')){const sel=$('fLevel'); if(![...sel.options].some(o=>o.value===u.get('level'))){const o=document.createElement('option');o.value=u.get('level');o.textContent=u.get('level');sel.appendChild(o)} sel.value=u.get('level'); sel.dataset.touched=1}
  if(u.get('grp'))grpFilter=u.get('grp');
  if(u.get('etype'))sigFilter={etype:u.get('etype'),sig:u.get('sig')||''};
  renderChips(); show(['errors','suspect','all','patterns','volume','deploys','partners','schedules','inventory','apis','digest','clients','runs','trace'].includes(u.get('tab'))?u.get('tab'):'clients');
  if(u.get('trace')){$('trTerm').value=u.get('trace'); show('trace'); startTrace()}})();
onRange(); tick(); setInterval(tick,5000); setInterval(status,5000);
</script></body></html>"""


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help="path to config.json (optional)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--log", help="write output to this file (for running in the background with pythonw)")
    ap.add_argument("--list-profiles", action="store_true",
                    help="print the profiles/regions this app will watch, then exit")
    args = ap.parse_args()
    if args.log or sys.stdout is None:          # pythonw has no console
        f = open(args.log or os.path.join(HERE, "dashboard.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
        print(f"\n=== started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    cfg = load_config(args.config)
    if args.port:
        cfg["port"] = args.port

    if args.list_profiles:
        print("python :", sys.executable)
        print("boto3  :", boto3.__version__)
        print("config :", os.environ.get("AWS_CONFIG_FILE", os.path.expanduser("~/.aws/config")))
        print("creds  :", os.environ.get("AWS_SHARED_CREDENTIALS_FILE",
                                         os.path.expanduser("~/.aws/credentials")))
        found = boto3.Session().available_profiles
        print(f"profiles found ({len(found)}):", ", ".join(found))
        for p, r in build_workers(cfg, None):
            print(f"  will watch {p}/{r}")
        return

    db = cfg["db_file"] if os.path.isabs(cfg["db_file"]) else os.path.join(HERE, cfg["db_file"])
    store = Store(db, cfg)
    aws_ops.init_db(store)
    aws_local.init_db(store)
    global LLM, AIJOBS, TRIAGE
    LLM = aws_local.LocalLLM(cfg)
    AIJOBS = aws_local.Jobs(LLM)
    global CODE, EXTRA_RX
    EXTRA_RX[:] = [re.compile(x, re.I) for x in cfg["record_count_patterns"]]
    root = cfg["repos_root"] or os.path.dirname(HERE)
    rf = cfg["repos_file"] if os.path.isabs(cfg["repos_file"]) else os.path.join(HERE, cfg["repos_file"])
    CODE = aws_ops.CodeIndex(root, rf)

    workers = build_workers(cfg, store)
    if not workers:
        sys.exit("No AWS profiles found. Check ~/.aws/config and ~/.aws/credentials.")

    # On Windows, SO_REUSEADDR lets a second copy silently share the port with an old one,
    # so the browser may keep talking to the stale process. Refuse instead.
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(store, workers, cfg))
    except OSError:
        sys.exit(f"Port {cfg['port']} is already in use - is another copy of the feed still running? "
                 f"Stop it (Ctrl+C in its window) or use --port.")

    def pruner():
        """Drop lines from accounts no longer watched and hand freed space back to the disk."""
        keys = [w.key for w in workers]
        n = 0
        while True:
            time.sleep(300 if n else 60)      # first pass a minute after start, then every 5 minutes
            now = int(time.time() * 1000)
            store.rollup(now - 3 * 3_600_000, now)          # keep the hourly baseline current
            try:
                aws_ops.rollup_runs(store, workers, now - 3 * 3_600_000, now, EXTRA_RX)
            except Exception as ex:
                print("run rollup failed:", ex)
            n += 1
            if n % 3 == 0:
                store.delete_orphans(now - cfg["keep_hours"] * 3_600_000, keys)
                store.prune_baseline(cfg["baseline_days"])
                store.shrink()
    threading.Thread(target=pruner, daemon=True).start()
    TRIAGE = aws_local.Triage(LLM, store, workers, cfg, lambda eid: store.context(eid, 120_000, 30_000))
    TRIAGE.start()
    print("Local model:", LLM.check(force=True)["detail"])

    print(f"Watching {len(workers)} profile/region pair(s):")
    for w in workers:
        print(f"  - {w.key}")
        if cfg["infra_scan"]:
            w.infra = aws_infra.InfraScanner(w.profile, w.region, cfg, store)
            w.infra.start()
        store.set_status(w.key, ok=False, profile=w.profile, region=w.region, error="starting…")
        w.start()

    url = f"http://127.0.0.1:{cfg['port']}"
    print(f"Feed: {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
