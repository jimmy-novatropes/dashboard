"""Quality loop for the tower, after Anthropic's "How the Claude Code team does code review" (Oct 2026):

- Triage outcomes: score every verdict by what happened next (muted / task / fixed / ignored), so we know how
  often "actionable" was right; those outcomes (plus your own labels) form a test set that the triage is
  re-scored on (eval) whenever the model or prompt changes.
- Code review: several finder passes over a diff + verifier votes on each finding; only confirmed findings,
  labeled Important / Nit / Pre-existing with file:line, plus a risk tier and how to revert. Max 3 rounds.
- Bug-class sweep: after a fix, look for the same weakness in every repo (nightly + on demand).
- Bad-deploy alert: a deploy that made errors worse becomes a high-priority task with the rollback steps.

All model calls go through LocalLLM.chat, i.e. the cloud model when it's on (local GPU as fallback)."""

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import aws_local

DAY_MS = 86_400_000

# ------------------------------------------------------------------------------------------ settings
OUTCOME_DAYS = 30                 # triage verdicts scored over this many days
IGNORED_AFTER_DAYS = 7            # "actionable", nothing done, still happening this long after first seen -> ignored
EVAL_MAX_CASES = 40               # labeled patterns per eval run
EVAL_WEEKDAY, EVAL_HOUR = 6, 3    # weekly eval: Sunday 03:xx tower time (weekday 0 = Monday)
REVIEW_VERIFIERS = 3              # verifier votes per review finding
REVIEW_MIN_VOTES = 2              # votes needed to keep a finding
REVIEW_MAX_NITS = 3               # nits shown at most
REVIEW_MAX_ROUNDS = 3             # review rounds on the same files before "rethink the approach"
REVIEW_MAX_DIFF_CHARS = 120_000
SWEEP_HOUR = 2                    # nightly bug-class sweep: 02:xx tower time
SWEEP_SEEDS_PER_NIGHT = 5         # fixed / confirmed patterns swept per night
SWEEP_RESWEEP_DAYS = 14           # don't sweep the same pattern again within this
SWEEP_MAX_HITS = 30               # code locations checked per sweep
SWEEP_BATCH = 6                   # snippets per model call
SWEEP_MAX_FILE_KB = 400
SWEEP_EXT = (".py", ".js", ".ts", ".mjs", ".cjs", ".java", ".cs", ".rb", ".go", ".php")
SWEEP_SKIP_DIRS = {"graphify-out", "_graphify", ".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", ".serverless",
                   ".idea", ".vscode", "site-packages", "coverage", ".pytest_cache", "vendor", "dashboard"}
ALERT_EVERY_S = 600               # bad-deploy check interval
ALERT_WINDOW_H = 6                # before/after window per deploy
ALERT_VERDICTS = ("worse", "new errors")

# ------------------------------------------------------------------------------------------ prompts
REVIEW_FOCUS = {
    "correctness": "Logic bugs: wrong conditions, off-by-one, wrong field / property names, None / undefined "
                   "handling, wrong types, broken pagination or loops, async/await mistakes, exceptions that "
                   "escape or are raised in the wrong place.",
    "integration": "Partner API and data safety: HubSpot / NetSuite / Epicor / QuickBooks / Shopify calls without "
                   "retry or backoff on 429 / 5xx, missing pagination, writes or deletes that could hit the wrong "
                   "or too many records, duplicates when a run repeats (not idempotent), work that can exceed the "
                   "Lambda timeout, errors swallowed so a run looks green but did nothing.",
    "security": "Secrets or tokens in code or logs, logging personal data, unsafe input into queries or commands, "
                "permissions / IAM / serverless config widened, config changes that break a deploy.",
}
REVIEW_SYSTEM = """You review a code change (unified diff + the developer's intent) for ONE risk area:
{focus}
Report only real problems a careful senior developer would block or fix. No style, naming, formatting, or
"add tests / comments / logging" remarks. Every finding must point at a line of the diff (file + the NEW line
number shown on the left) and quote that code. pre_existing=true when the problem is in unchanged context lines
(not introduced by this change). Reply with ONLY a JSON object:
{{"findings": [{{"file": "path", "line": 123, "severity": "important" or "nit", "pre_existing": false,
  "title": "<= 12 words", "why": "<= 2 sentences: what goes wrong and when", "quote": "exact code from the diff"}}]}}
Use an empty list when there is nothing. No markdown."""

FINDING_VERIFY_SYSTEM = """You check ONE finding from an automated code review against the diff. Be skeptical:
real=true only if the problem clearly exists in this code and would cause wrong behaviour, damaged data, a failed
run or a security issue. Reply with ONLY a JSON object: {"real": true or false, "reason": "<= 1 sentence"}"""

SWEEP_PLAN_SYSTEM = """A recurring production error in one integration was traced to a weakness in its code.
Name the general bug class and give up to 3 Python regular expressions that find code lines where the SAME
weakness could exist in other repos (e.g. the API call that needs a retry, the field access that needs a guard).
They must be specific (match well under 1 line in 200 of normal code). Reply with ONLY a JSON object:
{"bug_class": "<= 15 words", "what_good_looks_like": "<= 1 sentence", "regexes": ["..."]}"""

SWEEP_CHECK_SYSTEM = """You check code snippets for one known bug class: {bug_class}
Good code: {good}
For each snippet decide whether it has the same weakness (the protection is missing in the code shown).
Reply with ONLY a JSON object: {{"results": [{{"id": "S1", "same_bug": true or false, "why": "<= 1 sentence"}}]}}"""

DEPLOY_SYSTEM = """A deploy of an AWS Lambda made its errors worse. From the error patterns that are new or grew
after the deploy, say in at most 2 sentences what most likely broke. Only what the lines show. Plain text."""

# risk tier: the change decides it (not the author)
HIGH_RISK = [
    (re.compile(r"\.(delete|archive|merge|purge|remove)\w*\s*\(|\bDELETE\s+FROM\b|['\"]DELETE['\"]|batch/archive", re.I),
     "deletes / archives / merges records"),
    (re.compile(r"serverless\.ya?ml|template\.ya?ml|\.github/workflows|\biam\b|Policy|Role\b|requirements\.txt|package\.json", re.I),
     "deploy / permissions / dependencies"),
    (re.compile(r"secret|token|password|api[_-]?key|authorization|oauth", re.I), "auth / secrets"),
    (re.compile(r"payment|invoice|price|amount|billing|refund", re.I), "money fields"),
]
LOW_RISK_FILE = re.compile(r"(\.md|\.txt|\.rst)$|(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$|\.(spec|test)\.[jt]s$", re.I)
TIER_ADVICE = {
    "low": "docs / tests only: fine once checks pass.",
    "medium": "read the diff, run the tests (local_run), deploy when you can watch the next runs.",
    "high": "read every line yourself, try it on one record / a sandbox first, deploy when you can watch it, "
            "and get a second pair of eyes if you can.",
}


def init_db(store):
    with store.lock:
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS triage_labels(
                etype TEXT, sig TEXT, profile TEXT, label TEXT, source TEXT, at INTEGER, note TEXT,
                PRIMARY KEY(etype, sig, profile));
            CREATE TABLE IF NOT EXISTS triage_evals(at INTEGER PRIMARY KEY, target TEXT, model TEXT, cases INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS reviews(
                id TEXT PRIMARY KEY, at INTEGER, repo TEXT, files TEXT, risk TEXT, found INTEGER, kept INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS sweeps(
                etype TEXT, sig TEXT, profile TEXT, at INTEGER, bug_class TEXT, hits INTEGER, confirmed INTEGER, data TEXT,
                PRIMARY KEY(etype, sig, profile));
            CREATE TABLE IF NOT EXISTS deploy_alerts(
                profile TEXT, function TEXT, deployed_at INTEGER, at INTEGER, verdict TEXT, task_id INTEGER,
                PRIMARY KEY(profile, function, deployed_at));
        """)
        store.db.commit()


def _now():
    return int(time.time() * 1000)


def _day(ms):
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


def number_diff(diff):
    """Unified diff with the NEW line number in front of every kept / added line (so findings can cite file:line)."""
    out, new = [], None
    for line in diff.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if m:
            new = int(m.group(1))
            out.append(line)
        elif line.startswith(("+++", "---", "diff ", "index ")) or new is None:
            out.append(line)
        elif line.startswith("-"):
            out.append(f"     | {line}")
        else:
            out.append(f"{new:5d}| {line}")
            new += 1
    return "\n".join(out)


def _similar(a, b):
    wa = {w for w in re.findall(r"[a-z0-9]{4,}", a.lower())}
    wb = {w for w in re.findall(r"[a-z0-9]{4,}", b.lower())}
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.34


def risk_tier(diff):
    files = re.findall(r"^\+\+\+ b/(.+)$", diff, re.M) or re.findall(r"^diff --git a/\S+ b/(\S+)", diff, re.M)
    changed = "\n".join(l for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
    reasons = [why for rx, why in HIGH_RISK if rx.search(changed) or any(rx.search(f) for f in files)]
    if reasons:
        return "high", reasons
    if files and all(LOW_RISK_FILE.search(f) for f in files):
        return "low", ["docs / tests only"]
    return "medium", ["ordinary code change"]


def revert_steps(files):
    fl = " ".join(files[:6]) or "<files>"
    return ("Not committed: `git checkout -- " + fl + "` (or `git stash`). Committed: `git revert <commit>`. "
            "Deployed: redeploy the previous commit the usual way, then check the deploy verdict / did_my_fix_work.")


class Quality(threading.Thread):
    def __init__(self, llm, store, tasks, cfg, here, jobs, deploy_check=None, event=None):
        super().__init__(daemon=True, name="quality")
        self.llm, self.store, self.tasks, self.cfg, self.jobs = llm, store, tasks, cfg, jobs
        self.deploy_check = deploy_check            # (deploy row, window_hours) -> impact dict or None
        self.event = event or (lambda *a: None)
        qc = cfg.get("quality") or {}
        self.enabled = qc.get("enabled", True)
        self.repos_root = qc.get("repos_root") or cfg.get("repos_root") or os.path.dirname(os.path.dirname(here))
        self.rounds = {}
        self.last = {"alert": 0.0, "sweep_day": "", "eval_day": ""}
        self.busy = {"eval": False, "sweep": False}
        self.last_error = None

    # ---------------------------------------------------------------- helpers
    def _q(self, sql, args=()):
        with self.store.lock:
            return self.store.db.execute(sql, args).fetchall()

    def _w(self, sql, args=()):
        with self.store.lock:
            self.store.db.execute(sql, args)
            self.store.db.commit()

    def _ask(self, system, user, kind, max_tokens=900, as_json=True):
        try:
            return self.llm.chat(system, user, as_json=as_json, timeout=600, max_tokens=max_tokens, kind=kind)
        except Exception as ex:
            self.event("warning", "quality", kind, f"{type(ex).__name__}: {ex}"[:250])
            return {} if as_json else ""

    def _job(self, kind, fn, *args):
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "status": "running", "answer": None, "started": time.time(), "kind": kind}
        with self.jobs.lock:
            self.jobs.jobs[jid] = job

        def work():
            try:
                job["answer"] = fn(*args)
                job["status"] = "done"
            except Exception as ex:
                job["status"], job["error"] = "error", f"{type(ex).__name__}: {ex}"[:300]
                self.event("error", "quality", kind, job["error"])
        threading.Thread(target=work, daemon=True).start()
        return jid

    # ---------------------------------------------------------------- 1. triage outcomes
    def outcomes(self, days=OUTCOME_DAYS):
        now = _now()
        since = now - days * DAY_MS
        hour_now = now // 3_600_000
        tri = self._q("SELECT etype, sig, profile, at, model, data FROM ai_triage WHERE at >= ?", (since,))
        mutes = {(e, s, p): st for e, s, p, st in self._q("SELECT etype, sig, profile, status FROM mutes")}
        labels = {(e, s, p): l for e, s, p, l in self._q("SELECT etype, sig, profile, label FROM triage_labels")}
        linked = {}
        if self.tasks:
            for it in self.tasks.list(status="all", limit=5000):
                ref = (it.get("error_ref") or "").strip()
                if " | " in ref:
                    e, s = ref.split(" | ", 1)
                    linked.setdefault((e.strip(), s.strip()[:200]), it)
        recent, groups, first = {}, {}, {}
        for p, g, e, s, n, h0 in self._q("SELECT profile, grp, etype, sig, SUM(CASE WHEN hour >= ? THEN n ELSE 0 END), "
                                         "MIN(hour) FROM pattern_hours GROUP BY profile, grp, etype, sig", (hour_now - 24,)):
            k = (e, s, p)
            recent[k] = recent.get(k, 0) + (n or 0)
            groups.setdefault(k, set()).add(g)
            first[k] = min(first.get(k, h0), h0)
        deploys = self._q("SELECT profile, log_group, deployed_at FROM deploys WHERE deployed_at >= ?", (since - 7 * DAY_MS,))
        rows = []
        for e, s, p, at, model, data in tri:
            k = (e, s, p)
            try:
                d = json.loads(data)
            except ValueError:
                continue
            verdict = d.get("first_verdict") if d.get("verified") == "disputed" else d.get("verdict")
            final = d.get("verdict")
            it = linked.get((e, s[:200]))
            if k in labels:
                out = "labeled " + labels[k]
            elif mutes.get(k) == "fixed":
                out = "fixed"
            elif k in mutes:
                out = "muted"
            elif it:
                out = "fixed" if it["status"] == "done" else "task"
            elif recent.get(k, 0) == 0 and k in recent:
                grps = groups.get(k, set())
                # stopped after a deploy of its function: a hint, not proof (partner issues stop on their own too)
                out = "gone after deploy" if any(dp == p and lg in grps and dat >= at - DAY_MS
                                                 for dp, lg, dat in deploys) else "stopped"
            elif final == "actionable" and k in first and hour_now - first[k] > IGNORED_AFTER_DAYS * 24:
                out = "ignored"
            else:
                out = "open"
            rows.append({"etype": e, "sig": s, "profile": p, "verdict": final, "first_verdict": verdict,
                         "verified": d.get("verified"), "category": d.get("category"), "model": model,
                         "outcome": out, "at": at})
        return rows

    @staticmethod
    def label_of(r):
        """Binary truth from an outcome: 'actionable' / 'not' / None (unknown)."""
        o = r["outcome"]
        if o.startswith("labeled "):
            return "actionable" if o.endswith("actionable") else "not"
        if o in ("task", "fixed"):
            return "actionable"
        if o == "muted":
            return "not"
        return None

    def score(self, rows):
        by = {}
        for r in rows:
            by.setdefault(r["verdict"], Counter())[r["outcome"].split(" ")[0] if r["outcome"].startswith("labeled") else r["outcome"]] += 1
        act = [r for r in rows if r["verdict"] == "actionable"]
        known = [r for r in act if self.label_of(r)]
        right = sum(1 for r in known if self.label_of(r) == "actionable")
        notact = [r for r in rows if r["verdict"] in ("noise", "external")]
        missed = sum(1 for r in notact if self.label_of(r) == "actionable")
        ver = Counter(r["verified"] for r in rows if r.get("verified"))
        disputed_right = sum(1 for r in rows if r.get("verified") == "disputed" and self.label_of(r) == "not")
        disputed_wrong = sum(1 for r in rows if r.get("verified") == "disputed" and self.label_of(r) == "actionable")
        return {"patterns": len(rows), "by_verdict": {k: dict(v) for k, v in by.items()},
                "actionable": len(act), "actionable_known": len(known), "actionable_right": right,
                "precision_pct": round(100 * right / len(known), 1) if known else None,
                "ignored": sum(1 for r in act if r["outcome"] == "ignored"),
                "not_actionable": len(notact), "missed": missed, "verify": dict(ver),
                "verify_disputed_right": disputed_right, "verify_disputed_wrong": disputed_wrong}

    def set_label(self, profile, etype, sig, label, note="", source="you"):
        label = (label or "").strip().lower()
        if label not in aws_local.VERDICTS + ("clear",):
            raise ValueError("label must be actionable | noise | external | unclear | clear")
        if label == "clear":
            self._w("DELETE FROM triage_labels WHERE etype=? AND sig=? AND profile=?", (etype, sig, profile))
            return {"cleared": True}
        self._w("INSERT OR REPLACE INTO triage_labels VALUES (?,?,?,?,?,?,?)",
                (etype, sig, profile, label, source, _now(), (note or "")[:300]))
        return {"ok": True, "label": label}

    # ---------------------------------------------------------------- 2. eval (labeled set, re-scored)
    def eval_cases(self):
        inputs = {(e, s, p): (inp, at) for e, s, p, at, inp in
                  self._q("SELECT etype, sig, profile, at, input FROM triage_inputs")}
        cases = []
        for r in self.outcomes(days=3650):
            lab = self.label_of(r)
            k = (r["etype"], r["sig"], r["profile"])
            if lab and k in inputs:
                cases.append({"key": k, "label": lab, "input": inputs[k][0]})
        cases.sort(key=lambda c: c["label"] != "actionable")       # keep both classes when capped
        act = [c for c in cases if c["label"] == "actionable"]
        nott = [c for c in cases if c["label"] == "not"]
        half = EVAL_MAX_CASES // 2
        return act[:max(half, EVAL_MAX_CASES - len(nott))] + nott[:max(half, EVAL_MAX_CASES - len(act))]

    def run_eval(self, target="cloud", verify=True):
        if self.busy["eval"]:
            return "An eval is already running."
        self.busy["eval"] = True
        try:
            cases = self.eval_cases()
            if not cases:
                return ("No labeled patterns yet: label some with triage_feedback, or mute noise / add tasks from "
                        "errors in the dashboard - those become the test set.")
            llm = self.llm
            if target == "local":
                ask = lambda system, u, kind, mt: llm._chat(system, u, True, 600, mt, "eval", llm.model, llm.num_ctx,
                                                            llm.max_input_chars, llm.gate, False)
            else:
                ask = lambda system, u, kind, mt: llm.chat(system, u, as_json=True, timeout=600, max_tokens=mt, kind="eval")
            models, res = Counter(), []

            def one(c):
                try:
                    j = aws_local.judge(llm, c["input"], c["input"], verify=verify, ask=ask)
                    models[j.pop("_model", "?")] += 1
                    return c, j["verdict"]
                except Exception as ex:
                    return c, "error:" + type(ex).__name__
            with ThreadPoolExecutor(3 if target != "local" else 1) as ex:
                res = list(ex.map(one, cases))
            tp = sum(1 for c, v in res if c["label"] == "actionable" and v == "actionable")
            fp = sum(1 for c, v in res if c["label"] == "not" and v == "actionable")
            fn = sum(1 for c, v in res if c["label"] == "actionable" and v != "actionable")
            tn = sum(1 for c, v in res if c["label"] == "not" and v != "actionable")
            unclear = sum(1 for _, v in res if v == "unclear")
            errors = sum(1 for _, v in res if v.startswith("error:"))
            data = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "unclear": unclear, "errors": errors, "verify": verify,
                    "accuracy_pct": round(100 * (tp + tn) / len(res), 1),
                    "precision_pct": round(100 * tp / (tp + fp), 1) if tp + fp else None,
                    "recall_pct": round(100 * tp / (tp + fn), 1) if tp + fn else None,
                    "misses": [f"{c['key'][2]}: {c['key'][0]} -> {v}" for c, v in res if c["label"] == "actionable" and v != "actionable"][:8],
                    "false_alarms": [f"{c['key'][2]}: {c['key'][0]}" for c, v in res if c["label"] == "not" and v == "actionable"][:8]}
            model = ", ".join(f"{m} x{n}" for m, n in models.most_common())
            self._w("INSERT OR REPLACE INTO triage_evals VALUES (?,?,?,?,?)", (_now(), target, model, len(res), json.dumps(data)))
            self.event("info", "quality", "eval", f"{target} ({model}): accuracy {data['accuracy_pct']}% on {len(res)} "
                                                  f"labeled patterns, false alarms {fp}, misses {fn}")
            return self.eval_text(1)
        finally:
            self.busy["eval"] = False

    def eval_text(self, n=3):
        rows = self._q("SELECT at, target, model, cases, data FROM triage_evals ORDER BY at DESC LIMIT ?", (n,))
        if not rows:
            return "EVAL: never run (triage_eval, or weekly on Sundays)."
        out = []
        for at, target, model, cases, data in rows:
            d = json.loads(data)
            out.append(f"EVAL {_day(at)} {target} [{model}]: accuracy {d['accuracy_pct']}% of {cases}, precision "
                       f"{d['precision_pct']}%, recall {d['recall_pct']}% (false alarms {d['fp']}, misses {d['fn']}, "
                       f"unclear {d['unclear']}, errors {d['errors']}, verifier {'on' if d.get('verify', True) else 'off'})")
            if d.get("misses"):
                out.append("  missed: " + "; ".join(d["misses"][:4]))
            if d.get("false_alarms"):
                out.append("  false alarms: " + "; ".join(d["false_alarms"][:4]))
        return "\n".join(out)

    # ---------------------------------------------------------------- 3. code review
    def start_review(self, diff, intent="", repo="", files=""):
        return self._job("review", self.review, diff, intent, repo, files)

    def review(self, diff, intent="", repo="", files=""):
        diff = diff[:REVIEW_MAX_DIFF_CHARS]
        flist = [f for f in (files.split(",") if isinstance(files, str) else files or []) if f.strip()] or \
            re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)
        rkey = hashlib.sha1((repo + "|" + ",".join(sorted(f.strip() for f in flist))).encode()).hexdigest()[:12]
        self.rounds[rkey] = self.rounds.get(rkey, 0) + 1
        rnd = self.rounds[rkey]
        user = (f"Repo: {repo or '?'}\nIntent: {intent or '(not given)'}\n\nDiff (NEW line numbers on the left):\n"
                + number_diff(diff))
        with ThreadPoolExecutor(3) as ex:
            got = list(ex.map(lambda f: (f, self._ask(REVIEW_SYSTEM.format(focus=REVIEW_FOCUS[f]), user, "review", 1500)),
                              REVIEW_FOCUS))
        found = []
        for focus, ans in got:
            for x in (ans.get("findings") if isinstance(ans, dict) else None) or []:
                if not isinstance(x, dict) or not x.get("title"):
                    continue
                try:
                    line = int(x.get("line") or 0)
                except (TypeError, ValueError):
                    line = 0
                dup = next((y for y in found if y["file"] == str(x.get("file", "")) and abs(y["line"] - line) <= 2
                            and _similar(y["title"], str(x["title"]))), None)
                if dup:
                    dup["also"] = sorted(set(dup.get("also", [])) | {focus})
                    continue
                found.append({"file": str(x.get("file", ""))[:200], "line": line, "focus": focus,
                              "severity": "nit" if str(x.get("severity", "")).lower() == "nit" else "important",
                              "pre_existing": bool(x.get("pre_existing")), "title": str(x["title"])[:120],
                              "why": str(x.get("why", ""))[:400], "quote": str(x.get("quote", ""))[:300]})

        def votes(f):
            q = (user[:60000] + "\n\nFinding to check:\n" +
                 json.dumps({k: f[k] for k in ("file", "line", "title", "why", "quote")}))
            n = 0
            for _ in range(REVIEW_VERIFIERS):
                v = self._ask(FINDING_VERIFY_SYSTEM, q, "review_verify", 200)
                n += 1 if (v.get("real") is True or str(v.get("real")).lower() == "true") else 0
            return n
        with ThreadPoolExecutor(3) as ex:
            vs = list(ex.map(votes, found))
        kept = []
        for f, n in zip(found, vs):
            f["votes"] = n
            if n >= REVIEW_MIN_VOTES:
                kept.append(f)
        kept.sort(key=lambda f: (f["severity"] != "important", f["pre_existing"], -f["votes"]))
        imp = [f for f in kept if f["severity"] == "important"]
        nits = [f for f in kept if f["severity"] == "nit"][:REVIEW_MAX_NITS]
        tier, why = risk_tier(diff)
        new_imp = sum(1 for f in imp if not f["pre_existing"])
        out = [f"{new_imp} important, {len(nits)} nit, {sum(1 for f in imp if f['pre_existing'])} pre-existing "
               f"(cloud review: {len(REVIEW_FOCUS)} finder passes, {REVIEW_VERIFIERS} verifier votes each; "
               f"{len(found)} raised, {len(found) - len(kept)} dropped as unconfirmed).",
               f"Risk: {tier.upper()} ({', '.join(why)}) - {TIER_ADVICE[tier]}"]
        for title, group in (("Important", imp), ("Nit", nits)):
            if group:
                out.append(f"\n{title}:")
                for i, f in enumerate(group, 1):
                    out += [f"{i}. {f['file']}:{f['line']} - {f['title']} [{'pre-existing' if f['pre_existing'] else 'new'}, "
                            f"{f['focus']}, {f['votes']}/{REVIEW_VERIFIERS} votes]",
                            f"   {f['why']}"] + ([f"   `{f['quote']}`"] if f["quote"] else [])
        out.append("\nHow to revert: " + revert_steps(flist))
        if rnd >= REVIEW_MAX_ROUNDS:
            out.append(f"Round {rnd} on these files (cap {REVIEW_MAX_ROUNDS}): stop iterating - rethink the approach "
                       "or fix the whole bug class at once instead of another round.")
        elif rnd > 1:
            out.append(f"Round {rnd} of {REVIEW_MAX_ROUNDS} on these files; next time review only what changed.")
        rid = uuid.uuid4().hex[:10]
        self._w("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?,?)",
                (rid, _now(), repo, ",".join(flist)[:1000], tier, len(found), len(kept), json.dumps(kept)[:50000]))
        return "\n".join(out)

    # ---------------------------------------------------------------- 4. bug-class sweep
    def sweep_seeds(self):
        done = {(e, s, p): at for e, s, p, at in self._q("SELECT etype, sig, profile, at FROM sweeps")}
        cutoff = _now() - SWEEP_RESWEEP_DAYS * DAY_MS
        rows = [r for r in self.outcomes(days=14)
                if (r["outcome"] in ("fixed", "task") or (r["verdict"] == "actionable" and
                                                           (r.get("verified") == "confirmed" or r["outcome"] == "gone after deploy")))
                and r.get("category") not in ("partner API outage", "bad or missing data", "expected / handled")
                and self.label_of(r) != "not"
                and done.get((r["etype"], r["sig"], r["profile"]), 0) < cutoff]
        rows.sort(key=lambda r: {"fixed": 0, "task": 1}.get(r["outcome"], 2))
        return rows

    def start_sweep(self, profile, etype, sig):
        return self._job("sweep", self.sweep_one, profile, etype, sig)

    def sweep_one(self, profile, etype, sig):
        tri = aws_local.get_triage(self.store, profile).get((etype, sig, profile)) or {}
        inp = self._q("SELECT input FROM triage_inputs WHERE etype=? AND sig=? AND profile=?", (etype, sig, profile))
        sample = inp[0][0][:4000] if inp else sig
        plan = self._ask(SWEEP_PLAN_SYSTEM, f"Client: {profile}\nError type: {etype}\nMessage: {sig}\n"
                         f"Triage: {json.dumps({k: tri.get(k) for k in ('category', 'summary', 'likely_cause', 'next_step')})}\n\n"
                         f"Lines:\n{sample}", "sweep", 500)
        rxs = []
        for x in (plan.get("regexes") or [])[:3]:
            try:
                if 3 <= len(str(x)) <= 300:
                    rxs.append(re.compile(str(x)))
            except re.error:
                pass
        bug, good = str(plan.get("bug_class") or "?")[:200], str(plan.get("what_good_looks_like") or "")[:300]
        hits = self._grep(rxs) if rxs else []
        confirmed = []
        for i in range(0, len(hits), SWEEP_BATCH):
            batch = hits[i:i + SWEEP_BATCH]
            txt = "\n\n".join(f"### S{j + 1} {h['file']}:{h['line']}\n{h['snippet']}" for j, h in enumerate(batch))
            ans = self._ask(SWEEP_CHECK_SYSTEM.format(bug_class=bug, good=good), txt, "sweep", 900)
            for r in (ans.get("results") if isinstance(ans, dict) else None) or []:
                try:
                    h = batch[int(str(r.get("id", "S0")).lstrip("Ss")) - 1]
                except (ValueError, IndexError):
                    continue
                if r.get("same_bug") is True or str(r.get("same_bug")).lower() == "true":
                    confirmed.append({"file": h["file"], "line": h["line"], "why": str(r.get("why", ""))[:200]})
        data = {"bug_class": bug, "good": good, "regexes": [r.pattern for r in rxs], "hits": len(hits),
                "confirmed": confirmed[:40], "root": self.repos_root}
        self._w("INSERT OR REPLACE INTO sweeps VALUES (?,?,?,?,?,?,?,?)",
                (etype, sig, profile, _now(), bug, len(hits), len(confirmed), json.dumps(data)))
        if confirmed:
            self.event("info", "quality", "sweep", f"{profile} {etype}: '{bug}' also likely in {len(confirmed)} place(s)")
        return self._sweep_text(profile, etype, sig, data)

    @staticmethod
    def _sweep_text(profile, etype, sig, d):
        out = [f"Sweep from {profile} / {etype}: {sig[:120]}",
               f"Bug class: {d['bug_class']}" + (f" (good: {d['good']})" if d.get("good") else ""),
               f"Searched {d.get('root')} with {len(d['regexes'])} pattern(s): {d['hits']} candidate line(s), "
               f"{len(d['confirmed'])} look like the same bug:"]
        out += [f"- {c['file']}:{c['line']} - {c['why']}" for c in d["confirmed"]] or ["- none"]
        return "\n".join(out)

    def _grep(self, rxs):
        hits, per_file = [], Counter()
        root = self.repos_root
        if not os.path.isdir(root):
            return hits
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in SWEEP_SKIP_DIRS and not d.startswith(".")]
            for fn in fns:
                if not fn.endswith(SWEEP_EXT):
                    continue
                path = os.path.join(dp, fn)
                try:
                    if os.path.getsize(path) > SWEEP_MAX_FILE_KB * 1024:
                        continue
                    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
                except OSError:
                    continue
                rel = os.path.relpath(path, root).replace("\\", "/")
                for i, line in enumerate(lines):
                    if len(line) > 400 or not any(rx.search(line) for rx in rxs):
                        continue
                    if per_file[rel] >= 2:
                        break
                    per_file[rel] += 1
                    lo, hi = max(0, i - 6), min(len(lines), i + 7)
                    hits.append({"file": rel, "line": i + 1,
                                 "snippet": "\n".join(f"{n + 1:5d}| {lines[n][:200]}" for n in range(lo, hi))})
                    if len(hits) >= SWEEP_MAX_HITS:
                        return hits
        return hits

    def sweeps_text(self, n=5):
        rows = self._q("SELECT profile, etype, sig, at, data FROM sweeps ORDER BY at DESC LIMIT ?", (n,))
        if not rows:
            return "SWEEPS: none yet (nightly at 02:xx, or sweep_bug_class)."
        return "\n\n".join(f"[{_day(at)}] " + self._sweep_text(p, e, s, json.loads(d)) for p, e, s, at, d in rows)

    def nightly_sweep(self):
        if self.busy["sweep"]:
            return
        self.busy["sweep"] = True
        try:
            for r in self.sweep_seeds()[:SWEEP_SEEDS_PER_NIGHT]:
                self.sweep_one(r["profile"], r["etype"], r["sig"])
        finally:
            self.busy["sweep"] = False

    # ---------------------------------------------------------------- 5. bad-deploy alert
    def check_deploys(self):
        if not self.deploy_check:
            return 0
        now = _now()
        seen = {(p, f, d) for p, f, d in self._q("SELECT profile, function, deployed_at FROM deploy_alerts")}
        latest = {}
        for d in self.store.list_deploys(None, now - DAY_MS):
            latest.setdefault((d["profile"], d["function"]), d)
        n = 0
        for (prof, fn), d in latest.items():
            if (prof, fn, d["deployed_at"]) in seen:
                continue
            imp = self.deploy_check(d, ALERT_WINDOW_H)
            if not imp or imp.get("verdict") not in ALERT_VERDICTS:
                continue
            b = imp["before"]["error"] + imp["before"]["suspect"]
            a = imp["after"]["error"] + imp["after"]["suspect"]
            pats = [p for p in imp.get("patterns") or [] if p["status"] == "new" or p["after"] > p["before"]][:4]
            cause = ""
            if pats:
                cause = self._ask(DEPLOY_SYSTEM, "\n\n".join(
                    f"[{p['status']}] {p['etype']}: {p['sig'][:300]} (before {p['before']}, after {p['after']})\n{p['sample'][:1200]}"
                    for p in pats), "deploy_alert", 200, as_json=False) or ""
            prev = [x for x in self.store.list_deploys(prof, 0, fn, limit=3) if x["deployed_at"] < d["deployed_at"]]
            pv = prev[0] if prev else None
            rollback = ("Roll back: redeploy the version before this one"
                        + (f" (deployed {_day(pv['deployed_at'])}" + (f", code {pv['code_sha'][:12]}" if pv.get("code_sha") else "")
                           + (f", {pv['label']}" if pv.get("label") else "") + ")" if pv else "")
                        + " - in the repo, `git log` to find the commit before this deploy, check it out and deploy it the "
                          "usual way; then watch the deploy verdict / did_my_fix_work. If it's a quick fix instead, "
                          "use fix_brief first.")
            reason = (f"Deploy at {_day(d['deployed_at'])}: errors + hidden {b} in {ALERT_WINDOW_H}h before -> {a} after "
                      f"({imp['verdict']}).")
            item = {"title": f"Check deploy of {fn}: {imp['verdict']}", "client": prof, "source": "error",
                    "priority": "high", "external_id": f"deploy:{prof}:{fn}:{d['deployed_at']}",
                    "request": (cause.strip() + " " if cause else "") + "New / growing: " +
                               ("; ".join(f"{p['etype']} ({p['before']}->{p['after']})" for p in pats) or "-"),
                    "reason": reason, "notes": rollback}
            if pats:
                item["error_ref"] = f"{pats[0]['etype']} | {pats[0]['sig']}"[:500]
            tid = None
            if self.tasks:
                try:
                    r = self.tasks.add([item])
                    tid = (r.get("added") or r.get("updated") or r.get("unchanged") or [None])[0]
                except Exception as ex:
                    self.event("warning", "quality", "deploy_alert", f"task not added: {ex}"[:200])
            self.event("error", "deploy", fn, f"{prof}: {reason} {('Likely: ' + cause.strip()[:200]) if cause else ''}")
            self._w("INSERT OR REPLACE INTO deploy_alerts VALUES (?,?,?,?,?,?)",
                    (prof, fn, d["deployed_at"], now, imp["verdict"], tid))
            n += 1
        return n

    # ---------------------------------------------------------------- report
    def text(self):
        s = self.score(self.outcomes())
        out = [f"QUALITY (triage outcomes {OUTCOME_DAYS}d, {s['patterns']} patterns): "
               + "; ".join(f"{v} -> " + ", ".join(f"{k} {n}" for k, n in sorted(c.items(), key=lambda x: -x[1]))
                           for v, c in s["by_verdict"].items())]
        out.append(f"  'actionable' right {s['precision_pct'] if s['precision_pct'] is not None else '?'}% "
                   f"({s['actionable_right']}/{s['actionable_known']} with a known outcome), ignored {s['ignored']} "
                   f"(still happening after {IGNORED_AFTER_DAYS}d, nothing done); noise/external that needed work: "
                   f"{s['missed']}/{s['not_actionable']}")
        if s["verify"]:
            out.append(f"  verifier: {', '.join(f'{k} {n}' for k, n in s['verify'].items())}; disputed and later "
                       f"confirmed noise {s['verify_disputed_right']}, disputed but needed work {s['verify_disputed_wrong']}")
        out.append(self.eval_text(1))
        since = _now() - 7 * DAY_MS
        rv = self._q("SELECT COUNT(*), SUM(found), SUM(kept), SUM(risk='high') FROM reviews WHERE at >= ?", (since,))[0]
        out.append(f"REVIEWS 7d: {rv[0] or 0} diff review(s), {rv[1] or 0} findings raised, {rv[2] or 0} kept after "
                   f"votes, {rv[3] or 0} high-risk")
        sw = self._q("SELECT COUNT(*), SUM(hits), SUM(confirmed) FROM sweeps WHERE at >= ?", (since,))[0]
        out.append(f"SWEEPS 7d: {sw[0] or 0} bug class(es) swept, {sw[1] or 0} candidate lines, {sw[2] or 0} same-bug "
                   f"locations (sweep_results)")
        al = self._q("SELECT profile, function, verdict, at, task_id FROM deploy_alerts WHERE at >= ? ORDER BY at DESC", (since,))
        out.append(f"DEPLOY ALERTS 7d: {len(al)}" + "".join(f"\n  {_day(at)} {p} {f}: {v} (task #{t})" for p, f, v, at, t in al[:5]))
        if self.last_error:
            out.append(f"  last error: {self.last_error}")
        return "\n".join(out)

    def api(self, path, q):
        if path == "/api/quality/text":
            return {"text": self.text()}
        if path == "/api/quality/label":
            try:
                return self.set_label(q.get("profile", ""), q.get("etype", ""), q.get("sig", ""), q.get("label", ""), q.get("note", ""))
            except ValueError as ex:
                return {"error": str(ex)}
        if path == "/api/quality/outcomes":
            rows = self.outcomes(int(float(q.get("days") or OUTCOME_DAYS)))
            if q.get("profile"):
                rows = [r for r in rows if r["profile"] == q["profile"]]
            return {"score": self.score(rows), "rows": rows[:300]}
        if path == "/api/quality/eval":
            target = "local" if q.get("target") == "local" else "cloud"
            return {"id": self._job("eval", self.run_eval, target, q.get("verify", "1") != "0")}
        if path == "/api/quality/sweep":
            if not (q.get("profile") and q.get("etype")):
                return {"error": "Need profile, etype and sig (from list_error_patterns)."}
            return {"id": self.start_sweep(q["profile"], q["etype"], q.get("sig", ""))}
        if path == "/api/quality/sweeps":
            return {"text": self.sweeps_text(int(q.get("n") or 5))}
        if path == "/api/quality/deploys":
            return {"alerted": self.check_deploys()}
        return {"error": "unknown"}

    # ---------------------------------------------------------------- loop
    def run(self):
        time.sleep(120)
        while True:
            try:
                if self.enabled:
                    if time.time() - self.last["alert"] >= ALERT_EVERY_S:
                        self.last["alert"] = time.time()
                        self.check_deploys()
                    lt = time.localtime()
                    day = time.strftime("%Y-%m-%d", lt)
                    if lt.tm_hour == SWEEP_HOUR and self.last["sweep_day"] != day:
                        self.last["sweep_day"] = day
                        self.nightly_sweep()
                    if lt.tm_wday == EVAL_WEEKDAY and lt.tm_hour == EVAL_HOUR and self.last["eval_day"] != day:
                        self.last["eval_day"] = day
                        self.run_eval("cloud")
                self.last_error = None
            except Exception as ex:
                self.last_error = f"{type(ex).__name__}: {ex}"[:300]
                self.event("error", "quality", "loop", self.last_error)
            time.sleep(60)
