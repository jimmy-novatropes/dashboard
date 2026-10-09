"""
Operations features for the AWS Error Feed (imported by aws_error_feed.py):

  - Runs:            group a Lambda's log lines into individual runs (one row per invocation)
  - Did-nothing:     runs that processed 0 / far fewer records than usual, log groups gone quiet
  - Trace a record:  find every line / run / Step Functions execution mentioning an ID, searching
                     CloudWatch directly for that ID (days or weeks back) without loading all logs
  - Code links:      map Lambda functions to repo folders, turn stack-trace lines into file links
  - Notes:           notes on error patterns ("known rate limit, ignore under 50/day")
  - Client report:   weekly per-client health report (HTML to print / save as PDF, or markdown)
  - Clients overview: red / amber / green status per client with the reasons
"""
import bisect
import html
import json
import os
import re
import statistics
import threading
import time
import uuid

import boto3
from botocore.config import Config as BotoConfig

import aws_infra

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
BC = BotoConfig(retries={"max_attempts": 6, "mode": "adaptive"})


def init_db(store):
    with store.lock:
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS notes(
                etype TEXT, sig TEXT, profile TEXT, note TEXT, ignore_below INTEGER, at INTEGER,
                PRIMARY KEY(etype, sig, profile));
            CREATE TABLE IF NOT EXISTS run_hours(
                profile TEXT, region TEXT, grp TEXT, hour INTEGER, runs INTEGER, failed INTEGER,
                records INTEGER, counted INTEGER, zero_runs INTEGER,
                PRIMARY KEY(profile, region, grp, hour));
        """)
        store.db.commit()


# =========================================================================== record counts
_NOUNS = (r"(?:records?|rows?|items?|deals?|contacts?|compan(?:y|ies)|orders?|products?|objects?|entries|entry|"
          r"results?|tickets?|invoices?|customers?|leads?|line[ _-]?items?|quotes?|opportunit(?:y|ies)|accounts?|"
          r"associations?|notes?|tasks?|calls?|propert(?:y|ies)|services?|productions?|events?|messages?|files?|"
          r"users?|patients?|doctors?|agents?|members?|transactions?|payments?|jobs?|batch(?:es)?|pages?)")
_VERBS = (r"(?:processed|processing|synced|syncing|synchronized|updated|updating|created|creating|inserted|"
          r"upserted|fetched|fetching|retrieved|found|got|pulled|pushed|sent|imported|exported|loaded|received|"
          r"returned|wrote|written|deleted|migrated|matched|handled|total|changed|modified|queued)")
COUNT_RX = [
    re.compile(rf"\b{_VERBS}\s*:?\s*(\d[\d,]*)\s+(?:[\w-]+\s+){{0,2}}{_NOUNS}\b", re.I),
    re.compile(rf"\b(\d[\d,]*)\s+(?:[\w-]+\s+){{0,2}}{_NOUNS}\s+(?:were\s+|was\s+|have\s+been\s+|to\s+be\s+)?{_VERBS}\b", re.I),
    re.compile(rf"\b{_NOUNS}\s+{_VERBS}\s*[:=]\s*(\d[\d,]*)", re.I),
    re.compile(r"[\"'](?:count|total|processed|records|num_records|record_count|records_processed|synced|"
               r"updated|created|total_records)[\"']\s*:\s*(\d+)", re.I),
]


def extract_count(msg, extra=()):
    """Biggest 'N records' style number in a line, or None."""
    best = None
    for rx in list(extra) + COUNT_RX:
        for m in rx.finditer(msg):
            try:
                n = int(m.group(1).replace(",", ""))
            except (ValueError, IndexError):
                continue
            if n < 10_000_000:
                best = n if best is None else max(best, n)
    return best


# =========================================================================== runs
def _lines(store, profile, region, grp, since, until):
    with store.lock:
        return store.db.execute(
            "SELECT id, ts, stream, level, etype, sig, message, hint FROM events "
            "WHERE profile = ? AND region = ? AND grp = ? AND ts BETWEEN ? AND ? ORDER BY stream, ts, seq",
            (profile, region, grp, since, until)).fetchall()


def build_runs(store, profile, region, grp, since, until, extra_rx=(), with_lines=False):
    """One entry per Lambda invocation, from the REPORT line (end time + duration) of each run.
    Lines from the same log stream inside that time window belong to that run."""
    rows = _lines(store, profile, region, grp, since - 16 * 60_000, until)
    by_stream = {}
    for r in rows:
        by_stream.setdefault(r[2], []).append(r)
    runs = []
    for stream, lines in by_stream.items():
        reps = []
        for r in lines:
            if r[3] == "platform" and r[6].startswith("REPORT"):
                m = aws_infra.REPORT_RX.match(r[6])
                if m:
                    st = aws_infra.STATUS_RX.search(m.group(7) or "")
                    dur = float(m.group(2))
                    reps.append({"request_id": m.group(1), "end": r[1], "start": int(r[1] - dur - 1500),
                                 "duration_ms": round(dur), "memory": int(m.group(4)), "mem_used": int(m.group(5)),
                                 "cold_start": bool(m.group(6)), "status": st.group(1).lower() if st else "success",
                                 "stream": stream, "report_id": r[0]})
        if not reps:
            continue
        ends = [x["end"] for x in reps]
        for x in reps:
            x.update(errors=0, hidden=0, warnings=0, lines=0, records=None, first_problem=None, _lines=[])
        for r in lines:
            if r[3] == "platform":
                continue
            i = bisect.bisect_left(ends, r[1])            # first run ending at/after this line
            if i < len(reps) and reps[i]["start"] <= r[1] <= reps[i]["end"] + 500:
                run = reps[i]
                run["lines"] += 1
                if r[3] == "error":
                    run["errors"] += 1
                elif r[3] == "suspect":
                    run["hidden"] += 1
                elif r[3] == "warning":
                    run["warnings"] += 1
                if r[3] in ("error", "suspect") and not run["first_problem"]:
                    run["first_problem"] = {"etype": r[4], "line": r[6].splitlines()[0][:300], "event_id": r[0]}
                if r[3] not in ("error", "suspect"):
                    n = extract_count(r[6], extra_rx)
                    if n is not None:
                        run["records"] = n if run["records"] is None else max(run["records"], n)
                if with_lines:
                    run["_lines"].append({"id": r[0], "ts": r[1], "level": r[3], "etype": r[4], "message": r[6],
                                          "hint": r[7]})
        runs += reps
    out = []
    for x in runs:
        if not (since <= x["end"] <= until):
            continue
        x["outcome"] = ("timeout" if x["status"] == "timeout" else
                        "failed" if x["status"] not in ("success",) or x["errors"] else
                        "hidden errors" if x["hidden"] else
                        "no records" if x["records"] == 0 else "ok")
        if with_lines:
            x["lines_list"] = x.pop("_lines")
        else:
            x.pop("_lines")
        out.append(x)
    out.sort(key=lambda x: x["end"], reverse=True)
    return out


def lambda_groups(store, profile=None, since=None):
    """Log groups that have Lambda REPORT lines (i.e. Lambdas we can build runs for)."""
    q, p = ("SELECT DISTINCT profile, region, grp FROM events WHERE level = 'platform' AND "
            "message LIKE 'REPORT%' AND ts >= ?"), [since or 0]
    if profile:
        q += " AND profile = ?"
        p.append(profile)
    with store.lock:
        return [tuple(r) for r in store.db.execute(q, p).fetchall()]


def get_run(store, profile, region, grp, request_id, extra_rx=()):
    with store.lock:
        r = store.db.execute("SELECT ts FROM events WHERE profile = ? AND grp = ? AND level = 'platform' "
                             "AND message LIKE ?", (profile, grp, f"REPORT RequestId: {request_id}%")).fetchone()
    if not r:
        return None
    runs = build_runs(store, profile, region, grp, r[0] - 1000, r[0] + 1000, extra_rx, with_lines=True)
    return next((x for x in runs if x["request_id"] == request_id), None)


def rollup_runs(store, workers, since, until, extra_rx=()):
    """Hourly per-function run / record counts, kept 30 days (baseline for 'did nothing')."""
    for profile, region, grp in lambda_groups(store, since=since):
        agg = {}
        for x in build_runs(store, profile, region, grp, since, until, extra_rx):
            h = x["end"] // HOUR_MS
            a = agg.setdefault(h, [0, 0, 0, 0, 0])
            a[0] += 1
            a[1] += x["outcome"] in ("failed", "timeout")
            if x["records"] is not None:
                a[2] += x["records"]
                a[3] += 1
                a[4] += x["records"] == 0
        with store.lock:
            for h, a in agg.items():
                store.db.execute(
                    "INSERT INTO run_hours VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(profile, region, grp, hour) "
                    "DO UPDATE SET runs = MAX(runs, excluded.runs), failed = MAX(failed, excluded.failed), "
                    "records = MAX(records, excluded.records), counted = MAX(counted, excluded.counted), "
                    "zero_runs = MAX(zero_runs, excluded.zero_runs)", (profile, region, grp, h, *a))
            store.db.execute("DELETE FROM run_hours WHERE hour < ?", (int(time.time() // 3600) - 31 * 24,))
            store.db.commit()


def run_totals(store, profile, since, until, grp=None):
    q = ("SELECT grp, SUM(runs), SUM(failed), SUM(records), SUM(counted), SUM(zero_runs), MIN(hour) "
         "FROM run_hours WHERE profile = ? AND hour >= ? AND hour < ?")
    p = [profile, since // HOUR_MS, until // HOUR_MS + 1]
    if grp:
        q += " AND grp = ?"
        p.append(grp)
    with store.lock:
        return {r[0]: {"runs": r[1] or 0, "failed": r[2] or 0, "records": r[3] or 0, "counted": r[4] or 0,
                       "zero_runs": r[5] or 0, "from_hour": r[6]}
                for r in store.db.execute(q + " GROUP BY grp", p)}


# =========================================================================== did nothing
def did_nothing(store, snap, profile, region, now, extra_rx=()):
    """Runs that processed nothing / much less than usual, and log groups that went quiet."""
    out = []
    for prof, reg, grp in lambda_groups(store, profile, now - DAY_MS):
        if reg != region:
            continue
        runs = [x for x in build_runs(store, prof, reg, grp, now - DAY_MS, now, extra_rx) if x["records"] is not None]
        if len(runs) < 3:
            continue
        recs = [x["records"] for x in runs]            # newest first
        older = recs[3:] or recs
        pos = [r for r in older if r > 0]
        median = statistics.median(pos) if pos else 0
        zero_streak = 0
        for r in recs:
            if r == 0:
                zero_streak += 1
            else:
                break
        base = {"profile": prof, "region": reg, "group": grp, "runs_checked": len(runs), "usual": median,
                "latest": recs[:5]}
        if zero_streak >= 2 and median > 0:
            out.append({**base, "kind": "zero records",
                        "detail": f"Last {zero_streak} runs processed 0 records (usually ~{median:g})."})
        elif median >= 5 and statistics.mean(recs[:3]) < 0.2 * median:
            out.append({**base, "kind": "low records",
                        "detail": f"Last 3 runs averaged {statistics.mean(recs[:3]):.0f} records vs usual ~{median:g}."})
        # compare today's total with the hourly baseline once a few days exist
        hist = run_totals(store, prof, now - 8 * DAY_MS, now - DAY_MS, grp).get(grp)
        if hist and hist["from_hour"] and now // HOUR_MS - hist["from_hour"] >= 72 and hist["counted"]:
            days = min(7, (now - DAY_MS) / HOUR_MS - hist["from_hour"]) / 24
            avg = hist["records"] / max(days, 1)
            today = sum(recs)
            if avg >= 20 and today < 0.3 * avg and not any(o["group"] == grp for o in out):
                out.append({**base, "kind": "low records",
                            "detail": f"{today} records in the last 24h vs ~{avg:.0f}/day over the past week."})
    for grp, v in (snap.get("log_volume") or {}).items():
        if v["base24"] >= 50 and v["last24"] == 0:
            out.append({"profile": profile, "region": region, "group": grp, "kind": "silent",
                        "detail": f"No log lines for 24h; usually ~{v['base24']:.0f}/day."
                                  + (f" Last activity {time.strftime('%Y-%m-%d %H:%M', time.localtime(v['last_active'] / 1000))}."
                                     if v.get("last_active") else ""),
                        "usual": v["base24"], "latest": [0]})
        elif v["base6"] >= 30 and v["last6"] < 0.2 * v["base6"]:
            out.append({"profile": profile, "region": region, "group": grp, "kind": "quiet",
                        "detail": f"{v['last6']} log lines in the last 6h vs ~{v['base6']:.0f} usually at this time.",
                        "usual": v["base6"], "latest": [v["last6"]]})
    return out


# =========================================================================== trace a record
class TraceJobs:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, store, workers, term, days=7, include_sfn=True, classify=None):
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "term": term, "days": days, "status": "running", "started": int(time.time() * 1000),
               "accounts": len(workers), "accounts_done": 0, "groups_searched": 0, "hits": [], "sfn": [],
               "errors": [], "partial": []}
        with self.lock:
            self.jobs[jid] = job
            for old in sorted(self.jobs)[:-20]:
                if old != jid and self.jobs[old]["status"] == "done":
                    self.jobs.pop(old, None)
        word = re.compile(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", re.I)
        # 1) what's already loaded
        with store.lock:
            rows = store.db.execute(
                "SELECT id, ts, profile, region, grp, stream, level, etype, message FROM events WHERE message LIKE ?"
                + (" AND profile IN (%s)" % ",".join("?" * len(workers)) if workers else ""),
                [f"%{term}%"] + [w.profile for w in workers]).fetchall()
        seen = set()
        for r in rows:
            if word.search(r[8]):
                seen.add(r[0])
                job["hits"].append({"id": r[0], "ts": r[1], "profile": r[2], "region": r[3], "group": r[4],
                                    "stream": r[5], "level": r[6], "etype": r[7], "message": r[8][:3000],
                                    "source": "loaded"})

        # 2) CloudWatch directly + 3) Step Functions inputs, one thread per account
        def per_account(w):
            try:
                self._search_account(w, job, word, days, include_sfn, seen, classify)
            except Exception as ex:
                job["errors"].append(f"{w.key}: {aws_infra._err(ex)}")
            finally:
                with self.lock:
                    job["accounts_done"] += 1
                    if job["accounts_done"] >= job["accounts"]:
                        job["status"] = "done"
                        job["hits"].sort(key=lambda h: h["ts"])
                        job["sfn"].sort(key=lambda e: e["start"] or 0)
        if not workers:
            job["status"] = "done"
        for w in workers:
            threading.Thread(target=per_account, args=(w,), daemon=True).start()
        return jid

    def _search_account(self, w, job, word, days, include_sfn, seen, classify):
        s = boto3.Session(profile_name=w.profile, region_name=w.region)
        logs = s.client("logs", config=BC)
        now = int(time.time() * 1000)
        start = now - int(days * DAY_MS)
        deadline = time.time() + 150
        pattern = '"' + job["term"].replace('"', "") + '"'
        for grp in list(w.groups):
            if time.time() > deadline:
                job["partial"].append(f"{w.key}: stopped after time limit; not every log group searched")
                break
            kw = dict(logGroupName=grp, startTime=start, filterPattern=pattern, limit=500)
            found, pages = 0, 0
            try:
                while pages < 60 and found < 300 and time.time() < deadline:
                    r = logs.filter_log_events(**kw)
                    pages += 1
                    for e in r.get("events", []):
                        eid = f"{w.key}:{e['eventId']}"
                        if eid in seen or not word.search(e.get("message", "")):
                            continue
                        seen.add(eid)
                        msg = e.get("message", "").rstrip()
                        lvl = classify(msg)[0] if classify else "info"
                        job["hits"].append({"id": eid, "ts": e["timestamp"], "profile": w.profile, "region": w.region,
                                            "group": grp, "stream": e.get("logStreamName", ""), "level": lvl,
                                            "etype": "", "message": msg[:3000], "source": "cloudwatch"})
                        found += 1
                    if not r.get("nextToken"):
                        break
                    kw["nextToken"] = r["nextToken"]
            except Exception as ex:
                if "ResourceNotFound" not in str(ex):
                    job["errors"].append(f"{w.key} {grp}: {aws_infra._err(ex)}")
            job["groups_searched"] += 1
        if not include_sfn:
            return
        try:
            sfn = s.client("stepfunctions", config=BC)
            sdl = time.time() + 60
            for page in sfn.get_paginator("list_state_machines").paginate():
                for sm in page.get("stateMachines", []):
                    if sm.get("type", "STANDARD") != "STANDARD":
                        continue
                    n = 0
                    for ep in sfn.get_paginator("list_executions").paginate(
                            stateMachineArn=sm["stateMachineArn"], PaginationConfig={"PageSize": 100, "MaxItems": 300}):
                        for ex in ep.get("executions", []):
                            st = int(ex["startDate"].timestamp() * 1000)
                            if st < start or time.time() > sdl:
                                break
                            n += 1
                            d = sfn.describe_execution(executionArn=ex["executionArn"])
                            if word.search(d.get("input") or "") or word.search(d.get("output") or ""):
                                job["sfn"].append({"profile": w.profile, "region": w.region, "state_machine": sm["name"],
                                                   "execution": ex["name"], "arn": ex["executionArn"], "status": ex["status"],
                                                   "start": st, "stop": int(ex["stopDate"].timestamp() * 1000) if ex.get("stopDate") else None,
                                                   "in_input": bool(word.search(d.get("input") or ""))})
                    if time.time() > sdl:
                        job["partial"].append(f"{w.key}: Step Functions search stopped after time limit")
                        return
        except Exception as ex:
            job["errors"].append(f"{w.key} step functions: {aws_infra._err(ex)}")

    def get(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            return json.loads(json.dumps(j)) if j else None


def trace_timeline(job):
    """Group hits into runs: same log stream, lines within 2 minutes of each other."""
    groups, cur = [], {}
    for h in sorted(job["hits"], key=lambda h: (h["group"], h["stream"], h["ts"])):
        k = (h["group"], h["stream"])
        g = cur.get(k)
        if g and h["ts"] - g["end"] <= 120_000:
            g["lines"].append(h)
            g["end"] = h["ts"]
        else:
            g = {"group": h["group"], "stream": h["stream"], "profile": h["profile"], "region": h["region"],
                 "start": h["ts"], "end": h["ts"], "lines": [h]}
            cur[k] = g
            groups.append(g)
    for g in groups:
        lv = [l["level"] for l in g["lines"]]
        g["outcome"] = "error" if "error" in lv else "hidden errors" if "suspect" in lv else "ok"
    groups.sort(key=lambda g: g["start"])
    return groups


# =========================================================================== code links
PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([\w<>.]+))?')
JS_FRAME = re.compile(r"at (?:([\w.<>$]+) )?\(?((?:/var/task|/opt)/[^:()\s]+):(\d+):\d+\)?")
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".idea", ".vscode", "dist", "build",
             "site-packages", ".aws-sam", ".serverless", "cdk.out", ".terraform", "env"}


def frames(msg):
    out = []
    for m in PY_FRAME.finditer(msg):
        out.append({"file": m.group(1), "line": int(m.group(2)), "func": m.group(3) or ""})
    for m in JS_FRAME.finditer(msg):
        out.append({"file": m.group(2), "line": int(m.group(3)), "func": m.group(1) or ""})
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


class CodeIndex:
    """Where each Lambda's code lives on this computer: function name -> repo folder.
    Found automatically under `repos_root` (folders named like the function, or deploy files that
    mention it), with manual overrides in repos.json: {"function-name": "C:\\\\path\\\\to\\\\folder"}."""

    def __init__(self, root, overrides_file):
        self.root, self.overrides_file = root, overrides_file
        self.map, self.built, self.lock = {}, 0, threading.Lock()

    def _overrides(self):
        try:
            with open(self.overrides_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def build(self, function_names, profiles=()):
        with self.lock:
            if time.time() - self.built < 600 and set(function_names) <= set(self.map):
                return self.map
            ov = self._overrides()
            want = {_norm(f): f for f in function_names}
            found = {}
            repos = []
            if self.root and os.path.isdir(self.root):
                repos = [os.path.join(self.root, d) for d in os.listdir(self.root)
                         if os.path.isdir(os.path.join(self.root, d)) and d not in SKIP_DIRS and not d.startswith(".")]
            for repo in repos:
                for dirpath, dirs, files in os.walk(repo):
                    depth = dirpath[len(repo):].count(os.sep)
                    dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")] if depth < 5 else []
                    base = _norm(os.path.basename(dirpath))
                    if base in want and want[base] not in found:
                        found[want[base]] = {"repo": repo, "dir": dirpath, "how": "folder name"}
                    for fn in files:
                        if not fn.lower().endswith((".yml", ".yaml", ".json", ".tf", ".toml", ".cfg")) or fn == "package-lock.json":
                            continue
                        p = os.path.join(dirpath, fn)
                        try:
                            if os.path.getsize(p) > 300_000:
                                continue
                            text = open(p, encoding="utf-8", errors="ignore").read()
                        except OSError:
                            continue
                        for nf, f in want.items():
                            if f not in found and re.search(r"(?<![\w-])" + re.escape(f) + r"(?![\w-])", text):
                                found[f] = {"repo": repo, "dir": dirpath, "how": f"mentioned in {fn}"}
            for f, path in ov.items():
                found[f] = {"repo": path, "dir": path, "how": "repos.json"}
            self.map, self.built = found, time.time()
            return found

    def resolve(self, function, file, line):
        loc = self.map.get(function)
        if not loc or re.search(r"(site-packages|/lib/python|/var/lang/|/var/runtime/|node_modules)", file):
            return None
        rel = re.sub(r"^(/var/task/|/opt/python/|/opt/nodejs/|/opt/)", "", file).lstrip("/")
        for base in (loc["dir"], loc["repo"]):
            p = os.path.normpath(os.path.join(base, rel))
            if os.path.isfile(p):
                return p
        tail = rel.replace("/", os.sep)
        best = None
        for dirpath, dirs, files in os.walk(loc["repo"]):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if os.path.basename(rel) in files:
                p = os.path.join(dirpath, os.path.basename(rel))
                if p.endswith(tail):
                    return p
                best = best or p
        return best

    @staticmethod
    def snippet(path, line, ctx=6):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return None
        a, b = max(1, line - ctx), min(len(lines), line + ctx)
        return "".join(f"{'>>' if i == line else '  '} {i:5d}  {lines[i - 1]}" for i in range(a, b + 1))

    @staticmethod
    def github_url(repo, path, line):
        try:
            cfgtxt = open(os.path.join(repo, ".git", "config"), encoding="utf-8").read()
            m = re.search(r'url\s*=\s*(?:git@github\.com:|https://github\.com/)([^\s]+?)(?:\.git)?\s', cfgtxt + "\n")
            head = open(os.path.join(repo, ".git", "HEAD"), encoding="utf-8").read().strip()
            branch = head.split("refs/heads/")[-1] if "refs/heads/" in head else "main"
            if m:
                rel = os.path.relpath(path, repo).replace(os.sep, "/")
                return f"https://github.com/{m.group(1)}/blob/{branch}/{rel}#L{line}"
        except OSError:
            pass
        return None

    def locate(self, function, message):
        out = []
        for fr in frames(message):
            p = self.resolve(function, fr["file"], fr["line"])
            item = {**fr, "path": p}
            if p:
                loc = self.map[function]
                item["vscode"] = "vscode://file/" + p.replace("\\", "/") + f":{fr['line']}"
                item["github"] = self.github_url(loc["repo"], p, fr["line"])
                item["snippet"] = self.snippet(p, fr["line"])
                item["repo"] = loc["repo"]
            out.append(item)
        return out


def function_from_group(grp):
    m = re.match(r"/aws/lambda/(.+)", grp or "")
    return m.group(1) if m else None


# =========================================================================== notes
def set_note(store, etype, sig, profile, note, ignore_below=None):
    with store.lock:
        if note or ignore_below:
            store.db.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?)",
                             (etype, sig, profile or "", note or "", int(ignore_below) if ignore_below else None,
                              int(time.time() * 1000)))
        else:
            store.db.execute("DELETE FROM notes WHERE etype=? AND sig=? AND profile=?", (etype, sig, profile or ""))
        store.db.commit()


def all_notes(store):
    with store.lock:
        return [dict(zip(("etype", "sig", "profile", "note", "ignore_below", "at"), r))
                for r in store.db.execute("SELECT * FROM notes ORDER BY at DESC")]


def attach_notes(store, rows, profile=None):
    notes = {(n["etype"], n["sig"], n["profile"]): n for n in all_notes(store)}
    for g in rows:
        n = notes.get((g["etype"], g["sig"], profile or "")) or notes.get((g["etype"], g["sig"], ""))
        if not n and not profile:
            n = next((v for k, v in notes.items() if k[0] == g["etype"] and k[1] == g["sig"]), None)
        g["note"] = n["note"] if n else None
        g["ignore_below"] = n["ignore_below"] if n else None
        daily = g.get("last24") if g.get("last24") is not None else g.get("count", 0)
        g["below_threshold"] = bool(n and n["ignore_below"] and daily < n["ignore_below"])
    return rows


# =========================================================================== client report
def _metric_sum(cw, specs, start, end, period=86400):
    """specs: {id: (namespace, metric, dims)} -> {id: total}"""
    import datetime as _dt
    q = [{"Id": k, "ReturnData": True, "MetricStat": {"Metric": {"Namespace": ns, "MetricName": mn, "Dimensions": dims},
                                                      "Period": period, "Stat": "Sum"}} for k, (ns, mn, dims) in specs.items()]
    out = {}
    for i in range(0, len(q), 450):
        kw = dict(MetricDataQueries=q[i:i + 450], StartTime=_dt.datetime.fromtimestamp(start / 1000, _dt.timezone.utc),
                  EndTime=_dt.datetime.fromtimestamp(end / 1000, _dt.timezone.utc))
        while True:
            r = cw.get_metric_data(**kw)
            for m in r.get("MetricDataResults", []):
                out[m["Id"]] = out.get(m["Id"], 0) + sum(m.get("Values", []))
            if not r.get("NextToken"):
                break
            kw["NextToken"] = r["NextToken"]
    return out


def client_report(store, workers, profile, days, helpers):
    """helpers: dict of callables from the main module (summary/annotate/deploys/partner/etc.)."""
    now = int(time.time() * 1000)
    start = now - int(days * DAY_MS)
    ws = [w for w in workers if w.profile == profile]
    rep = {"profile": profile, "days": days, "from": start, "to": now, "generated": now,
           "lambda": {"invocations": 0, "errors": 0}, "sfn": {"started": 0, "succeeded": 0, "failed": 0},
           "notes": [], "functions": 0, "state_machines": 0}
    # ---- activity from CloudWatch metrics (covers the whole period, no logs needed)
    for w in ws:
        snap = helpers["infra"](w.key) or {}
        fns = snap.get("functions", [])
        rep["functions"] += len(fns)
        try:
            s = boto3.Session(profile_name=w.profile, region_name=w.region)
            cw = s.client("cloudwatch", config=BC)
            spec = {}
            for i, f in enumerate(fns):
                d = [{"Name": "FunctionName", "Value": f["name"]}]
                spec[f"i{i}"] = ("AWS/Lambda", "Invocations", d)
                spec[f"e{i}"] = ("AWS/Lambda", "Errors", d)
            try:
                sms = [m for p in s.client("stepfunctions", config=BC).get_paginator("list_state_machines").paginate()
                       for m in p.get("stateMachines", [])]
            except Exception:
                sms = []
            rep["state_machines"] += len(sms)
            for i, m in enumerate(sms):
                d = [{"Name": "StateMachineArn", "Value": m["stateMachineArn"]}]
                for k, mn in (("ss", "ExecutionsStarted"), ("su", "ExecutionsSucceeded"), ("sf", "ExecutionsFailed"),
                              ("st", "ExecutionsTimedOut")):
                    spec[f"{k}{i}"] = ("AWS/States", mn, d)
            res = _metric_sum(cw, spec, start, now) if spec else {}
            per_fn = []
            for i, f in enumerate(fns):
                inv, err = res.get(f"i{i}", 0), res.get(f"e{i}", 0)
                rep["lambda"]["invocations"] += inv
                rep["lambda"]["errors"] += err
                if inv:
                    per_fn.append({"name": f["name"], "invocations": int(inv), "errors": int(err)})
            rep.setdefault("per_function", []).extend(per_fn)
            for i in range(len(sms)):
                rep["sfn"]["started"] += res.get(f"ss{i}", 0)
                rep["sfn"]["succeeded"] += res.get(f"su{i}", 0)
                rep["sfn"]["failed"] += res.get(f"sf{i}", 0) + res.get(f"st{i}", 0)
        except Exception as ex:
            rep["notes"].append(f"Activity metrics unavailable for {w.region}: {aws_infra._err(ex)}")
    L, S = rep["lambda"], rep["sfn"]
    L["success_rate"] = round(100 * (1 - L["errors"] / L["invocations"]), 2) if L["invocations"] else None
    S["success_rate"] = round(100 * S["succeeded"] / (S["succeeded"] + S["failed"]), 2) if (S["succeeded"] + S["failed"]) else None
    rep["per_function"] = sorted(rep.get("per_function", []), key=lambda x: (-x["errors"], -x["invocations"]))[:15]
    # ---- records processed (from run history kept by the dashboard)
    tot = run_totals(store, profile, start, now)
    rep["records"] = sum(v["records"] for v in tot.values())
    rep["records_from"] = min((v["from_hour"] for v in tot.values() if v["from_hour"]), default=None)
    rep["records_from"] = rep["records_from"] * HOUR_MS if rep["records_from"] else None
    # ---- issues: this period vs the one before (hourly pattern counts, kept 30 days)
    cur, prev = helpers["pattern_totals"](profile, start, now), helpers["pattern_totals"](profile, start - (now - start), start)
    issues = []
    for k, v in cur.items():
        issues.append({"etype": k[0], "sig": k[1], "level": v["level"], "count": v["n"], "prev": prev.get(k, {}).get("n", 0),
                       "first_seen": v["first"], "last_seen": v["last"]})
    issues.sort(key=lambda x: -x["count"])
    rep["issues_total"] = sum(i["count"] for i in issues if i["level"] == "error")
    rep["hidden_total"] = sum(i["count"] for i in issues if i["level"] == "suspect")
    rep["issues_prev_total"] = sum(v["n"] for v in prev.values())
    mutes = {(m["etype"], m["sig"]): m for m in helpers["mutes"]() if m["profile"] in ("", profile)}
    notes = {(n["etype"], n["sig"]): n for n in all_notes(store) if n["profile"] in ("", profile)}
    recent_cut = now - DAY_MS
    rep["resolved"] = [{"etype": i["etype"], "sig": i["sig"], "count": i["count"],
                        "fixed_at": mutes[(i["etype"], i["sig"])]["at"]} for i in issues
                       if (i["etype"], i["sig"]) in mutes and mutes[(i["etype"], i["sig"])]["status"] == "fixed"
                       and not mutes[(i["etype"], i["sig"])]["regression"]]
    rep["gone"] = [i for i in issues if i["last_seen"] < recent_cut and (i["etype"], i["sig"]) not in mutes][:10]
    rep["open"] = [dict(i, note=(notes.get((i["etype"], i["sig"])) or {}).get("note")) for i in issues
                   if i["last_seen"] >= recent_cut and (i["etype"], i["sig"]) not in mutes][:15]
    rep["new_this_period"] = [i for i in issues if i["prev"] == 0 and i["first_seen"] >= start][:10]
    # ---- changes, schedules, partner problems, upgrades
    rep["deploys"] = helpers["deploys"](profile, start)
    rep["schedules"] = helpers["schedules"](profile)
    rep["partner"] = helpers["partners"](profile)
    fh = helpers["functions"](profile)
    rep["upgrades"] = [{"name": f["name"], "runtime": f["runtime"], "text": f["runtime_text"]}
                       for f in fh if f["runtime_status"] in ("deprecated", "soon")]
    rep["outdated_layers"] = [{"name": f["name"], "layers": f["outdated_layers"]} for f in fh if f["outdated_layers"]]
    rep["cost_30d"] = round(sum(f["cost_30d"] for f in fh), 2)
    # ---- overall status
    sched_bad = [s for s in rep["schedules"] if s["status"] in ("not running", "missed runs", "target missing")]
    sr = [x for x in (L["success_rate"], S["success_rate"]) if x is not None]
    worst = min(sr) if sr else None
    rep["status"] = ("attention" if sched_bad or (worst is not None and worst < 95) else
                     "watch" if rep["open"] or (worst is not None and worst < 99.5) else "healthy")
    return rep


def _fmt_n(x):
    return f"{int(x):,}" if x is not None else "-"


def _d(ms):
    return time.strftime("%b %d", time.localtime(ms / 1000)) if ms else "-"


def report_markdown(r):
    st = {"healthy": "Healthy", "watch": "Healthy, with items to watch", "attention": "Needs attention"}[r["status"]]
    L, S = r["lambda"], r["sfn"]
    out = [f"# Integration health report - {r['profile']}",
           f"{_d(r['from'])} - {_d(r['to'])} {time.strftime('%Y', time.localtime(r['to'] / 1000))}  |  Overall: **{st}**", "",
           "## Summary"]
    if L["invocations"]:
        out.append(f"- Lambda runs: {_fmt_n(L['invocations'])} with {_fmt_n(L['errors'])} failures "
                   f"(success rate {L['success_rate']}%) across {r['functions']} functions")
    if S["started"] or S["succeeded"]:
        out.append(f"- Workflow runs (Step Functions): {_fmt_n(S['succeeded'] + S['failed'])} finished, "
                   f"{_fmt_n(S['failed'])} failed (success rate {S['success_rate']}%)")
    if r["records"]:
        out.append(f"- Records processed: {_fmt_n(r['records'])}"
                   + (f" (tracked since {_d(r['records_from'])})" if r["records_from"] and r["records_from"] > r["from"] else ""))
    out.append(f"- Errors logged: {_fmt_n(r['issues_total'])} ({_fmt_n(r['hidden_total'])} handled warnings) vs "
               f"{_fmt_n(r['issues_prev_total'])} the period before")
    out.append(f"- Changes deployed: {len(r['deploys'])}")
    if r["resolved"] or r["gone"]:
        out += ["", "## Issues resolved"]
        out += [f"- {x['etype']}: {x['sig'][:110]} (fixed {_d(x['fixed_at'])})" for x in r["resolved"]]
        out += [f"- {x['etype']}: {x['sig'][:110]} - not seen since {_d(x['last_seen'])}" for x in r["gone"]]
    if r["open"]:
        out += ["", "## Open items"]
        out += [f"- {x['etype']}: {x['sig'][:110]} - {x['count']} occurrences" + (f" ({x['note']})" if x.get("note") else "")
                for x in r["open"][:10]]
    bad = [s for s in r["schedules"] if s["status"] not in ("ok", "disabled", "not checked", "not enough data")]
    if bad:
        out += ["", "## Scheduled jobs"]
        out += [f"- {s['name']}: {s['status']} - {s['detail']}" for s in bad]
    if r["deploys"]:
        out += ["", "## Changes deployed"]
        out += [f"- {_d(d['deployed_at'])}: {d['function']}{' (' + d['label'] + ')' if d.get('label') else ''} - {d['verdict']}"
                for d in r["deploys"][:15]]
    if r["partner"]:
        out += ["", "## Third-party service issues (last 24h)"]
        out += [f"- {p['service']}: {p['problem']} ({p['count']} occurrences)" for p in r["partner"][:8]]
    if r["upgrades"] or r["outdated_layers"]:
        out += ["", "## Recommended maintenance"]
        out += [f"- {u['name']}: runtime {u['runtime']} - {u['text']}" for u in r["upgrades"][:10]]
        out += [f"- {o['name']}: shared library layer update available ({', '.join(o['layers'])})" for o in r["outdated_layers"][:10]]
    if r["notes"]:
        out += ["", "_" + " ".join(r["notes"]) + "_"]
    return "\n".join(out)


def report_html(r):
    md = report_markdown(r)
    color = {"healthy": "#2b8a3e", "watch": "#e67700", "attention": "#c92a2a"}[r["status"]]
    body = []
    for line in md.splitlines():
        t = html.escape(line)
        t = re.sub(r"\*\*(.+?)\*\*", rf'<b style="color:{color}">\1</b>', t)
        if line.startswith("# "):
            body.append(f"<h1>{t[2:]}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{t[3:]}</h2>")
        elif line.startswith("- "):
            body.append(f"<li>{t[2:]}</li>")
        elif line.startswith("_"):
            body.append(f"<p class=note>{t.strip('_')}</p>")
        elif line:
            body.append(f"<p class=sub>{t}</p>")
    h, out, inlist = [], [], False
    for b in body:
        if b.startswith("<li>") and not inlist:
            out.append("<ul>")
            inlist = True
        if not b.startswith("<li>") and inlist:
            out.append("</ul>")
            inlist = False
        out.append(b)
    if inlist:
        out.append("</ul>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Health report - {html.escape(r['profile'])}</title>
<style>body{{font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b1f29;max-width:820px;margin:32px auto;padding:0 24px}}
h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:16px;margin:22px 0 6px;border-bottom:1px solid #e3e6ec;padding-bottom:4px}}
.sub{{color:#667085;margin:0 0 10px}}ul{{margin:4px 0 0 18px;padding:0}}li{{margin:3px 0}}.note{{color:#667085;font-size:12px;margin-top:24px}}
.bar{{display:flex;gap:10px;margin:0 0 18px}}.bar button{{font:inherit;padding:6px 12px;border:1px solid #d0d5dd;border-radius:6px;background:#fff;cursor:pointer}}
@media print{{.bar{{display:none}}body{{margin:0}}}}</style></head><body>
<div class="bar"><button onclick="print()">Print / Save as PDF</button><button onclick="navigator.clipboard.writeText(document.getElementById('md').textContent)">Copy as text</button></div>
{''.join(out)}<pre id="md" style="display:none">{html.escape(md)}</pre></body></html>"""


# =========================================================================== clients overview
def _ago(ms):
    m = (time.time() * 1000 - ms) / 60000
    return f"{m:.0f}m ago" if m < 90 else f"{m / 60:.0f}h ago" if m < 2160 else f"{m / 1440:.0f}d ago"


def client_card(profile, parts):
    """parts: precomputed pieces for one client -> card with status + reasons."""
    red, amber = [], []
    if not parts["connected"]:
        red.append(f"Can't connect to AWS: {parts['connection_error']}")
    for s in parts["sched_bad"]:
        red.append(f"Schedule {s['name']}: {s['status']}")
    for g in parts["regressions"]:
        red.append(f"Regression: {g['etype']}")
    for a in parts["alarms"]:
        red.append(f"Alarm firing: {a['name']}")
    for a in parts["api5xx"]:
        red.append(f"API {a['api']} ({a['stage']}): {a['e5']} server errors")
    for d in parts["nothing"]:
        (red if d["kind"] in ("zero records", "silent") else amber).append(
            f"{(function_from_group(d['group']) or d['group'])}: {d['kind']}")
    for d in parts["bad_deploys"]:
        red.append(f"Deploy of {d['function']} ({_ago(d['at'])}): {d['verdict']}")
    new_err = [g for g in parts["new"] if g["level"] == "error"]
    if new_err:
        red.append(f"{len(new_err)} new error type(s) today")
    spikes = [g for g in parts["spikes"] if not g.get("below_threshold")]
    if spikes:
        (red if any(g["level"] == "error" for g in spikes) else amber).append(f"{len(spikes)} spiking error type(s)")
    if [g for g in parts["new"] if g["level"] == "suspect"]:
        amber.append("New hidden errors today")
    for p in parts["partners"]:
        amber.append(f"{p['service']} {p['problem']}" + (" (several clients)" if p["several_clients_now"] else ""))
    if parts["last_failed"]:
        amber.append(f"{len(parts['last_failed'])} scheduled job(s) failed their last run")
    if parts["near_limits"]:
        amber.append(f"{parts['near_limits']} function(s) near timeout / memory limit")
    if parts["deprecated"]:
        amber.append(f"{parts['deprecated']} function(s) on deprecated runtimes")
    status = "red" if red else "amber" if amber else "green"
    return {"profile": profile, "status": status, "reasons": red + amber, "red": len(red), "amber": len(amber),
            **{k: parts[k] for k in ("errors_24h", "errors_prev", "hidden_24h", "hidden_prev", "runs_24h", "failed_runs_24h",
                                     "records_24h", "records_usual", "sched_ok", "sched_total", "last_deploy", "cost_30d",
                                     "functions", "accounts", "connected", "windows", "recent_deploys")}}
