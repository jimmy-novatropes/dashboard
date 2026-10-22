"""
Local model (Ollama) for the AWS Error Feed: does the high-volume reading on your own machine so
Claude only gets short answers.

  - Background triage: new / spiking / busiest error patterns get a verdict (actionable, noise,
    external, unclear), a category, a 1-2 sentence summary, likely cause and next step.
  - On-demand summaries: a run, a trace, or any set of log lines -> short answer.

Everything is optional: if Ollama isn't running, or the model isn't pulled, the dashboard works
exactly as before and the status chip says so.

Setup:  install Ollama (https://ollama.com), then:  ollama pull gpt-oss:20b
"""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

VERDICTS = ("actionable", "noise", "external", "unclear")
CATEGORIES = ("code bug", "bad or missing data", "partner API auth", "partner API rate limit",
              "partner API outage", "timeout / performance", "config / permissions", "expected / handled",
              "unknown")

TRIAGE_SYSTEM = """You triage errors from AWS Lambda / Step Functions integrations that sync data between
systems like HubSpot, NetSuite, Epicor and QuickBooks. You are given one recurring error pattern with
sample log lines and what the function logged just before. Reply with ONLY a JSON object:
{"verdict": one of ["actionable","noise","external","unclear"],
 "category": one of ["code bug","bad or missing data","partner API auth","partner API rate limit",
                     "partner API outage","timeout / performance","config / permissions",
                     "expected / handled","unknown"],
 "summary": "<= 2 short sentences: what is happening",
 "likely_cause": "<= 1 sentence",
 "next_step": "<= 1 sentence, concrete",
 "evidence": "the exact text (copied from the lines) that supports the verdict",
 "confidence": number 0..1}
Rules: only claim a cause that is visible in the lines and quote it in "evidence"; if nothing in the
lines shows the cause, use "unclear". A line that only counts things (records=, missing=, skipped=)
is usually normal progress output, not an error -> "noise".
"actionable" = needs a code/config fix by the developer. "noise" = harmless / already handled
(e.g. retried successfully, informational). "external" = caused by a partner service or their data
(outage, rate limit, expired token on their side). Base everything only on the lines given; if unsure
say "unclear" with low confidence. No markdown, no extra text."""

SUMMARY_SYSTEM = """You summarise logs from AWS Lambda / Step Functions integrations for a developer.
Be brief and concrete: what happened, in order, what failed and why (if visible), and which record IDs
or functions are involved. Use at most 8 short bullet points. Quote exact error messages when useful.
Do not invent anything that is not in the lines. If asked a question, answer it first in one sentence."""


READ_SYSTEM = """You read source code, diffs, git history or documents for a developer so they don't have to
read everything themselves. Lines are numbered as "  123| code". Be brief and concrete:
- If there is a question, answer it first in 1-3 sentences.
- Then the key points: what each file/part does, the important functions/sections WITH file:line
  references (e.g. handler.py:120-185), inputs/outputs, external APIs called, and anything that
  looks wrong, risky or unfinished.
- At most 15 short bullets. Quote exact names. Never invent code, lines or behaviour that isn't shown;
  if something isn't visible, say so."""

MAP_SYSTEM = """You are reading ONE PART of a larger set of files for a developer. Lines are numbered as
"  123| code". Write compact notes for this part only: per file, what it does, its key functions/sections
with file:line references, external calls, and anything relevant to the question or that looks wrong.
At most 8 bullets per file. No introduction. Never invent anything not shown."""

REDUCE_SYSTEM = """You are given notes that were written about different parts of a set of files. Combine
them into one brief answer for the developer: answer the question first (if any) in 1-3 sentences, then
at most 15 bullets with the key points, keeping the file:line references. Remove duplicates. Never add
anything that isn't in the notes."""


# Health hooks (set by aws_error_feed to aws_health.Health methods); all optional.
HOOKS = {"event": None, "count": None, "llm_call": None}
BACKGROUND_KINDS = ("triage",)      # everything else is someone (Claude / the page) waiting for an answer
# Cloud model (Ollama Pro, e.g. gpt-oss:120b-cloud, served through the same local Ollama): does the deep work
# someone is waiting for. These kinds always stay on the local GPU (background / many tiny calls):
LOCAL_ONLY_KINDS = ("triage", "repo_map", "filemap", "other")


def _hook(name, *a):
    f = HOOKS.get(name)
    if f:
        try:
            f(*a)
        except Exception:
            pass


class Gate:
    """Lets `slots` model calls run at once (it's one GPU). A call someone is waiting for always goes
    before background triage."""

    def __init__(self, slots=1):
        self.slots = max(1, int(slots or 1))
        self.busy, self.waiting = 0, [0, 0]
        self.cv = threading.Condition()

    def acquire(self, prio):
        with self.cv:
            self.waiting[prio] += 1
            try:
                while self.busy >= self.slots or (prio == 1 and self.waiting[0] > 0):
                    self.cv.wait()
                self.busy += 1
            finally:
                self.waiting[prio] -= 1

    def release(self):
        with self.cv:
            self.busy -= 1
            self.cv.notify_all()

    def state(self):
        return {"running": self.busy, "waiting_claude": self.waiting[0], "waiting_triage": self.waiting[1]}


class LocalLLM:
    def __init__(self, cfg):
        c = cfg.get("local_llm") or {}
        self.enabled = c.get("enabled", True)
        self.url = (c.get("url") or "http://localhost:11434").rstrip("/")
        self.model = c.get("model") or "gpt-oss:20b"
        self.num_ctx = int(c.get("num_ctx") or 16384)
        self.max_input_chars = int(c.get("max_input_chars") or 40000)
        self.state = {"ok": False, "detail": "not checked yet", "checked": 0}
        self.gate = Gate(c.get("parallel") or 1)   # model calls at once (match OLLAMA_NUM_PARALLEL)
        self.last_call = {}                   # timings of the latest call (for the status line)
        self.meter = None                     # meter(kind, chars_read, chars_written) - savings counter
        cc = cfg.get("cloud_llm") or {}
        self.cloud = {"enabled": bool(cc.get("enabled")), "model": cc.get("model") or "gpt-oss:120b-cloud",
                      "local_kinds": tuple(cc.get("local_kinds") or LOCAL_ONLY_KINDS),
                      "num_ctx": int(cc.get("num_ctx") or 65536),
                      "max_input_chars": int(cc.get("max_input_chars") or 180000),
                      "daily_token_cap": int(cc.get("daily_token_cap") or 3_000_000)}
        self.cloud_gate = Gate(cc.get("parallel") or 3)   # Ollama Pro allows 3 requests at once
        self.cloud_used = {"day": "", "tokens": 0, "calls": 0, "fallbacks": 0, "last_error": None}

    # ------------------------------------------------------------ health
    def check(self, force=False):
        if not self.enabled:
            self.state = {"ok": False, "detail": "disabled in config", "checked": time.time()}
            return self.state
        if not force and time.time() - self.state["checked"] < 60:
            return self.state
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=3) as r:
                tags = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
            base = self.model.split(":")[0]
            have = self.model in tags or any(t == self.model + ":latest" or t.split(":")[0] == base and
                                             self.model.endswith(t.split(":")[-1]) for t in tags)
            self.state = {"ok": have, "checked": time.time(), "models": tags,
                          "detail": f"{self.model} ready" if have else
                          f"Ollama is running but {self.model} isn't pulled - run: ollama pull {self.model}"}
        except Exception:
            self.state = {"ok": False, "checked": time.time(),
                          "detail": f"Ollama not reachable at {self.url} - install it and run: ollama pull {self.model}"}
        return self.state

    # ------------------------------------------------------------ routing (local GPU vs cloud)
    def route(self, kind):
        """"cloud" for deep work someone waits for, "local" for background triage / tiny calls,
        or when the cloud is off or today's token cap is used up."""
        c = self.cloud
        if not c["enabled"] or kind in c["local_kinds"]:
            return "local"
        day = time.strftime("%Y-%m-%d")
        if self.cloud_used["day"] != day:
            self.cloud_used.update(day=day, tokens=0, calls=0, fallbacks=0)
        return "cloud" if self.cloud_used["tokens"] < c["daily_token_cap"] else "local"

    def model_for(self, kind):
        return self.cloud["model"] if self.route(kind) == "cloud" else self.model

    def max_input_for(self, kind):
        return self.cloud["max_input_chars"] if self.route(kind) == "cloud" else self.max_input_chars

    def chunk_for(self, kind):
        if self.route(kind) == "cloud":
            return max(4000, min(self.cloud["max_input_chars"], (self.cloud["num_ctx"] - 1800) * 3))
        return self.chunk_chars

    def cloud_status(self):
        c = self.cloud
        return {"enabled": c["enabled"], "model": c["model"], "daily_token_cap": c["daily_token_cap"],
                "local_kinds": list(c["local_kinds"]), **self.cloud_used, "queue": self.cloud_gate.state()}

    @property
    def ready(self):
        return self.check()["ok"]

    @property
    def chunk_chars(self):
        """How much text fits in one call: the model's context minus room for instructions + answer."""
        return max(4000, min(self.max_input_chars, (self.num_ctx - 1800) * 3))

    # ------------------------------------------------------------ generation
    def chat(self, system, user, as_json=False, timeout=300, max_tokens=700, kind="other"):
        if self.route(kind) == "cloud":
            c = self.cloud
            try:
                return self._chat(system, user, as_json, timeout, max_tokens, kind,
                                  c["model"], c["num_ctx"], c["max_input_chars"], self.cloud_gate, True)
            except Exception as ex:          # offline / credit used up / model missing -> local GPU
                self.cloud_used["fallbacks"] += 1
                _hook("count", "cloud:fallback")
                self.cloud_used["last_error"] = f"{type(ex).__name__}: {ex}"[:300]
                _hook("event", "warning", "cloud_model", kind, "cloud failed, used local: " + str(ex)[:250])
        return self._chat(system, user, as_json, timeout, max_tokens, kind,
                          self.model, self.num_ctx, self.max_input_chars, self.gate, False)

    def _chat(self, system, user, as_json, timeout, max_tokens, kind, model, num_ctx, max_input, gate, cloud):
        cut = len(user) > max_input
        user = user[: max_input]
        body = {"model": model, "stream": False,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "options": {"num_ctx": num_ctx, "temperature": 0.2, "num_predict": max_tokens,
                            "repeat_penalty": 1.1}}
        if as_json:
            body["format"] = "json"
        if model.startswith("gpt-oss"):
            body["think"] = "low"                 # gpt-oss: keep its reasoning short
        elif re.match(r"(qwen3|deepseek-r1)", model):
            body["think"] = False
        prio = 1 if kind in BACKGROUND_KINDS else 0
        rec = {"kind": kind, "model": model, "prio": prio, "num_ctx": num_ctx, "cloud": cloud,
               "in_chars": len(system) + len(user), "truncated": cut}
        t_wait = time.time()
        gate.acquire(prio)
        rec["wait_ms"] = int(1000 * (time.time() - t_wait))
        t0, resp, text = time.time(), {}, ""
        try:
            for attempt in range(2):
                req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        resp = json.loads(r.read())
                    text = (resp.get("message", {}).get("content") or "").strip()
                    break
                except urllib.error.HTTPError as ex:
                    detail = ex.read().decode(errors="ignore")
                    if attempt == 0 and "think" in detail and "think" in body:
                        body.pop("think")          # model doesn't support the think option
                        continue
                    if attempt == 0 and "repeat limit" in detail:
                        # the model got stuck repeating itself: one retry, a bit more random
                        body["options"].update(temperature=0.6, repeat_penalty=1.2)
                        continue
                    raise RuntimeError(f"Ollama error {ex.code}: {detail[:200]}")
        except Exception as ex:
            gate.release()
            rec.update(err=f"{type(ex).__name__}: {ex}"[:300], total_ms=int(1000 * (time.time() - t0)))
            _hook("llm_call", rec)
            raise
        gate.release()
        ns = lambda k: int((resp.get(k) or 0) / 1e6)
        rec.update(out_chars=len(text), prompt_tok=resp.get("prompt_eval_count"), gen_tok=resp.get("eval_count"),
                   load_ms=ns("load_duration"), prompt_ms=ns("prompt_eval_duration"), gen_ms=ns("eval_duration"),
                   total_ms=int(1000 * (time.time() - t0)), cut_off=resp.get("done_reason") == "length")
        if cloud:
            self.cloud_used["tokens"] += ((resp.get("prompt_eval_count") or (len(system) + len(user)) // 4)
                                          + (resp.get("eval_count") or len(text) // 4))
            self.cloud_used["calls"] += 1
        if (resp.get("prompt_eval_count") or 0) >= num_ctx - max_tokens - 64:
            rec["truncated"] = True           # Ollama silently drops what doesn't fit num_ctx
        self.last_call = {"at": time.time(), "kind": kind, "model": model,
                          "gen_tps": round(rec["gen_tok"] / (rec["gen_ms"] / 1000), 1) if rec.get("gen_ms") else None,
                          "secs": round(rec["total_ms"] / 1000, 1)}
        if self.meter:
            try:
                self.meter(kind, len(user), len(text))
            except Exception:
                pass
        if not as_json:
            _hook("llm_call", rec)
            return text
        m = re.search(r"\{.*\}", text, re.S)
        try:
            out = json.loads(m.group(0) if m else text)
            rec["json_ok"] = 1
        except ValueError:
            out = {"verdict": "unclear", "category": "unknown", "summary": text[:300], "confidence": 0.1}
            rec["json_ok"] = 0
        _hook("llm_call", rec)
        return out


# ============================================================================ storage
def init_db(store):
    with store.lock:
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS ai_triage(
                etype TEXT, sig TEXT, profile TEXT, at INTEGER, model TEXT, count INTEGER, data TEXT,
                PRIMARY KEY(etype, sig, profile));
            CREATE TABLE IF NOT EXISTS ai_cache(k TEXT PRIMARY KEY, at INTEGER, text TEXT);
            CREATE TABLE IF NOT EXISTS ai_usage(
                day TEXT, source TEXT, kind TEXT, calls INTEGER, in_chars INTEGER, out_chars INTEGER,
                PRIMARY KEY(day, source, kind));
        """)
        store.db.commit()


def get_triage(store, profile=None):
    with store.lock:
        rows = store.db.execute("SELECT etype, sig, profile, at, model, count, data FROM ai_triage").fetchall()
    out = {}
    for et, sg, pf, at, model, cnt, data in rows:
        if profile and pf != profile:
            continue
        out[(et, sg, pf)] = {**json.loads(data), "at": at, "model": model, "count_at_triage": cnt}
    return out


# Claims a verdict must be able to point at in the lines; otherwise it's downgraded to "unclear".
_CLAIM_RX = {
    "partner API rate limit": re.compile(r"\b429\b|rate.?limit|too many requests|throttl", re.I),
    "partner API auth": re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|expired|invalid.?(token|key|grant)|auth", re.I),
    "partner API outage": re.compile(r"\b5\d\d\b|unavailable|timed? ?out|timeout|connection|refused|bad gateway", re.I),
    "timeout / performance": re.compile(r"timed? ?out|timeout|duration|slow|memory", re.I),
    "config / permissions": re.compile(r"denied|permission|not authorized|accessdenied|config|missing (env|key|setting)", re.I),
}


def check_evidence(res, text):
    """Downgrade a verdict whose category isn't supported by anything in the lines, and drop a quoted
    'evidence' that doesn't actually appear there."""
    ev = (res.get("evidence") or "").strip()
    if ev and ev[:60].lower() not in text.lower():
        res["evidence"] = ""
        res["note"] = "quoted evidence not found in the lines"
    rx = _CLAIM_RX.get(res.get("category"))
    if rx and not rx.search(text):
        res.update(verdict="unclear", confidence=min(res.get("confidence") or 0, 0.3),
                   note=f"downgraded: nothing in the lines shows {res['category']}")
    return res


def attach_triage(store, rows, profile=None):
    tr = get_triage(store)
    for g in rows:
        t = None
        if profile:
            t = tr.get((g["etype"], g["sig"], profile))
        else:
            for (et, sg, pf), v in tr.items():
                if et == g["etype"] and sg == g["sig"] and (pf in g.get("profiles", {}) or not g.get("profiles")):
                    t = v
                    break
        if t and "evidence" not in t:          # triaged before evidence checks existed: check it now
            t = check_evidence(dict(t), (g.get("sample") or "") + "\n" + str(g.get("sig") or ""))
        g["ai"] = t
    return rows


# ============================================================================ background triage
class Triage(threading.Thread):
    """Every few minutes: triage the patterns that matter most and don't have a fresh triage."""

    def __init__(self, llm, store, workers, cfg, context_fn):
        super().__init__(daemon=True)
        self.llm, self.store, self.workers, self.cfg = llm, store, workers, cfg
        self.context_fn = context_fn            # (event_id) -> (event, lines)
        self.done = 0
        self.last_error = None
        self.running_now = None
        self.backlog = None                     # patterns waiting for triage (last cycle)
        self.prefs = None                       # project priority: starred first, asleep skipped

    def candidates(self):
        """(profile, pattern) pairs: regressions, new, spiking, then the busiest - untriaged first."""
        tr = get_triage(self.store)
        out = []
        for prof in sorted({w.profile for w in self.workers}):
            if self.prefs and self.prefs.asleep(prof):
                continue
            rank = self.prefs.rank(prof) if self.prefs else 1
            rows = self.store.annotate(self.store.summary({"hours": 24, "profile": prof, "level": "error,suspect",
                                                           "hide_muted": "1"}), prof)
            for i, g in enumerate(rows[:40]):
                if g.get("below_threshold"):
                    continue
                prio = (0 if g.get("regression") else 1 if g.get("is_new") else 2 if g.get("is_spike") else 3 + i)
                t = tr.get((g["etype"], g["sig"], prof))
                every_run = g.get("likely_format") and not (g.get("is_spike") or g.get("is_new") or g.get("regression"))
                max_age = 7 * DAY_MS if every_run else DAY_MS     # chronic, unchanged: re-read weekly, not daily
                stale = (not t or time.time() * 1000 - t["at"] > max_age or
                         (g["count"] > 3 * max(t["count_at_triage"] or 1, 1) and g["count"] - (t["count_at_triage"] or 0) > 20))
                if stale and (prio < 3 or i < self.cfg.get("local_llm", {}).get("triage_top_n", 8)):
                    out.append(((rank, prio), prof, g))
        out.sort(key=lambda x: x[0])
        return out

    def triage_one(self, prof, g):
        ev_rows, _ = self.store.list_events({"profile": prof, "etype": g["etype"], "sig": g["sig"], "hours": 24}, 3)
        ctx_txt = ""
        if ev_rows:
            ev, lines = self.context_fn(ev_rows[0]["id"])
            if lines:
                ctx_txt = "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(l['ts'] / 1000))} [{l['level']}] "
                                    f"{l['message'][:600]}" for l in lines[-40:])
        samples = "\n---\n".join(e["message"][:2500] for e in ev_rows)
        user = (f"Client/account: {prof}\nError type: {g['etype']}\nLevel: {g['level']} "
                f"({'logged as an error' if g['level'] == 'error' else 'not logged as an error but looks like one'})\n"
                f"Normalized message: {g['sig']}\nOccurrences last 24h: {g['count']}"
                + (f" (usually ~{g.get('prev7_daily_avg')}/day)" if g.get("prev7_daily_avg") is not None else "")
                + f"\nLog groups: {', '.join(list(g['log_groups'])[:5])}\n"
                + (f"Team note: {g['note']}\n" if g.get("note") else "")
                + f"\nSample lines:\n{samples}\n\nWhat the same log stream printed around the latest one:\n{ctx_txt}")
        res = self.llm.chat(TRIAGE_SYSTEM, user, as_json=True, timeout=600, max_tokens=500, kind="triage")
        res = {"verdict": res.get("verdict") if res.get("verdict") in VERDICTS else "unclear",
               "category": res.get("category") if res.get("category") in CATEGORIES else "unknown",
               "summary": str(res.get("summary", ""))[:400], "likely_cause": str(res.get("likely_cause", ""))[:300],
               "next_step": str(res.get("next_step", ""))[:300],
               "confidence": max(0.0, min(1.0, float(res.get("confidence") or 0))),
               "evidence": str(res.get("evidence", ""))[:300]}
        res = check_evidence(res, samples + "\n" + ctx_txt)
        _hook("count", "triage:verdict:" + res["verdict"])
        note = res.get("note") or ""
        if note.startswith("downgraded"):
            _hook("count", "triage:downgraded")
        elif note.startswith("quoted evidence"):
            _hook("count", "triage:evidence_dropped")
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO ai_triage VALUES (?,?,?,?,?,?,?)",
                                  (g["etype"], g["sig"], prof, int(time.time() * 1000), self.llm.model, g["count"],
                                   json.dumps(res)))
            self.store.db.commit()
        return res

    def run(self):
        time.sleep(90)
        while True:
            try:
                if self.llm.ready:
                    cands = self.candidates()
                    self.backlog = len(cands)
                    for _, prof, g in cands[: self.cfg.get("local_llm", {}).get("triage_per_cycle", 6)]:
                        self.running_now = f"{prof}: {g['etype']}"
                        self.triage_one(prof, g)
                        self.done += 1
                    self.last_error = None
            except Exception as ex:
                self.last_error = str(ex)[:300]
                _hook("event", "error", "triage", self.running_now or "cycle", f"{type(ex).__name__}: {ex}")
            self.running_now = None
            time.sleep(self.cfg.get("local_llm", {}).get("triage_every_minutes", 5) * 60)


# ============================================================================ on-demand jobs
class PCache:
    """Answers kept in errors.db, so a restart doesn't re-read the same files / logs (30 days)."""

    def __init__(self, store, days=30):
        self.store, self.mem = store, {}
        with store.lock:
            store.db.execute("DELETE FROM ai_cache WHERE at < ?", (int(time.time()) - days * 86400,))
            store.db.commit()

    def get(self, k):
        if k in self.mem:
            return self.mem[k]
        with self.store.lock:
            r = self.store.db.execute("SELECT text FROM ai_cache WHERE k = ?", (k,)).fetchone()
        if r:
            self.mem[k] = r[0]
            return r[0]
        return None

    def __contains__(self, k):
        return self.get(k) is not None

    def __getitem__(self, k):
        v = self.get(k)
        if v is None:
            raise KeyError(k)
        return v

    def __setitem__(self, k, v):
        self.mem[k] = v
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO ai_cache VALUES (?,?,?)", (k, int(time.time()), v))
            self.store.db.commit()


# Single-shot templates (laptop-side tools choose one by name)
TEMPLATES = {
    "edit": ("""You edit code for a developer. You get part of a file with line numbers ("  12| code") and an
instruction that applies to lines START-END. Return ONLY the complete new text for lines START-END:
no line numbers, no markdown fences, no explanation. Keep the existing indentation, style and names.
Change only what the instruction asks. If it can't be done safely from what you see, return exactly
one line: CANNOT: <short reason>""", 1600),
    "commit": ("""Write a git commit message for this diff. First line: a summary of at most 72 characters in
the imperative mood. Then a blank line and up to 6 short bullet points of what changed (and why, if
visible). Plain text, no markdown headers, nothing invented.""", 400),
    "fix": ("""You get an error brief from an AWS Lambda / Step Functions integration: the error, what the
function logged just before it, the code around the failing line, recent runs and team notes.
Reply with: 1) Likely root cause (1-2 sentences). 2) Where: file:line(s) to change. 3) Suggested
fix (a few lines of code if obvious). 4) How to verify. Base everything on the brief and quote the
line that shows the cause; never assume rate limits, auth or outages unless a line shows them (429,
401/403, 5xx, timeout). A line that only counts things (records=, missing=) is normal progress output.
If the evidence doesn't support a conclusion, say "uncertain" and what to check next. Max 12 lines.""", 700),
    "narrative": ("""Write a short, client-friendly executive summary (3-5 sentences) of this integration health
report: overall status, what went wrong (if anything) and what was done or is planned. Plain
language, no jargon, nothing that isn't in the report.""", 400),
    "run": ("""Summarise this command output (tests / build / script run) for a developer: overall result first
(passed/failed, counts), then each failure: test or step name, file:line and the key error or
assertion message. Ignore passing noise. At most 12 bullets.""", 700),
    "filemap": ("""Describe what this file does in ONE line of at most 20 words. No preamble, no markdown.""", 60),
    "ask": ("""You are the on-call assistant for Open Flow's AWS integrations dashboard (CloudWatch Logs + Step
Functions across client AWS accounts; HubSpot / NetSuite / other syncs in Lambda). You get the question
and the dashboard's data (client status, error patterns with counts and triage, latest examples, the
lines logged just before, code around failing lines, runs, deploys, tasks). Answer ONLY from that data.
Be concrete: client, function, counts, times, exact error messages, file:line. Say what changed
(new / spiking / chronic). Mark guesses as "likely". Never assume rate limits, auth or outages unless a
line shows them (429, 401/403, 5xx, timeout). If the data doesn't answer it, say what's missing and
which tool would get it (get_log_lines, get_line_context, trace_record, local_read...).
Format: short bullets, max 250 words. Last line: "Evidence:" + up to 5 event_ids or file:line you used.""", 900),
    "brief": ("""Write the developer's morning brief from this dashboard snapshot of all client integrations.
Sections: "Needs action" (red clients: what is broken, since when, likely cause, first step),
"Watch" (amber: only what is new or worse), "Quiet" (one line listing the rest),
"Tasks" (proposed from yesterday's mail/meetings, overdue). Concrete: function names, counts,
error messages. Nothing that isn't in the data. Plain text bullets, max 300 words.""", 900),
}


class Jobs:
    """Summaries can take a while on a local GPU, so they run as jobs; callers poll."""

    def __init__(self, llm, store=None):
        self.llm, self.jobs, self.lock = llm, {}, threading.Lock()
        self.cache = PCache(store) if store is not None else {}

    def start_prompt(self, template, text, question="", kind=None):
        """One call with a named template (edit / commit / fix / narrative / run / filemap)."""
        system, max_tokens = TEMPLATES[template]
        kind = kind or template
        text = text[: self.llm.chunk_for(kind) - 400]
        key = hashlib.sha1((self.llm.model_for(kind) + "tpl:" + template + question + text).encode()).hexdigest()
        jid = uuid.uuid4().hex[:10]
        if key in self.cache:
            _hook("count", "cache:hit:" + kind)
            self.jobs[jid] = {"id": jid, "status": "done", "answer": self.cache[key], "cached": True}
            return jid
        _hook("count", "cache:miss:" + kind)
        job = {"id": jid, "status": "running", "answer": None, "started": time.time()}
        with self.lock:
            self.jobs[jid] = job

        def work():
            try:
                q = f"{question}\n\n" if question else ""
                ans = self.llm.chat(system, q + text, timeout=900, max_tokens=max_tokens, kind=kind)
                job["answer"], job["status"] = ans, "done"
                self.cache[key] = ans
            except Exception as ex:
                job["status"], job["error"] = "error", str(ex)[:300]
                _hook("event", "error", "job", job.get("kind") or "job", f"{type(ex).__name__}: {ex}")
        threading.Thread(target=work, daemon=True).start()
        return jid

    def filemap_known(self, items):
        """{name: purpose} for files whose content hash was already described."""
        out = {}
        for it in items:
            v = self.cache.get("fm:" + self.llm.model + ":" + str(it.get("sha")))
            if v:
                out[it["name"]] = v
        return out

    def start_filemap(self, files):
        """Describe each file in one line (cached by content hash). Background; poll with get()."""
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "status": "running", "answer": None, "started": time.time(), "parts": len(files),
               "done_parts": 0, "purposes": {}}
        with self.lock:
            self.jobs[jid] = job
        system, max_tokens = TEMPLATES["filemap"]

        def work():
            try:
                for f in files:
                    k = "fm:" + self.llm.model + ":" + str(f.get("sha"))
                    v = self.cache.get(k)
                    _hook("count", ("cache:hit:" if v else "cache:miss:") + "repo_map")
                    if not v:
                        txt = _numbered((f.get("text") or "")[: self.llm.chunk_chars - 600])
                        v = self.llm.chat(system, f"File: {f.get('name')}\n\n{txt}", timeout=600,
                                          max_tokens=max_tokens, kind="repo_map").strip().splitlines()[0][:200] \
                            if (f.get("text") or "").strip() else "(empty)"
                        self.cache[k] = v
                    job["purposes"][f.get("name")] = v
                    job["done_parts"] += 1
                job["status"] = "done"
            except Exception as ex:
                job["status"], job["error"] = "error", str(ex)[:300]
                _hook("event", "error", "job", job.get("kind") or "job", f"{type(ex).__name__}: {ex}")
        threading.Thread(target=work, daemon=True).start()
        return jid

    def start(self, question, text, kind="summary"):
        key = hashlib.sha1((self.llm.model_for(kind) + kind + question + text).encode()).hexdigest()
        if key in self.cache:
            _hook("count", "cache:hit:" + kind)
            jid = uuid.uuid4().hex[:10]
            self.jobs[jid] = {"id": jid, "status": "done", "answer": self.cache[key], "cached": True}
            return jid
        _hook("count", "cache:miss:" + kind)
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "status": "running", "answer": None, "started": time.time()}
        with self.lock:
            self.jobs[jid] = job
            if len(self.jobs) > 50:
                for k in list(self.jobs)[:-50]:
                    self.jobs.pop(k, None)

        def work():
            try:
                q = f"Question: {question}\n\n" if question else ""
                ans = self.llm.chat(SUMMARY_SYSTEM, q + "Log lines:\n" + text, timeout=900, max_tokens=900,
                                    kind=kind)
                job["answer"], job["status"] = ans, "done"
                self.cache[key] = ans
            except Exception as ex:
                job["status"], job["error"] = "error", str(ex)[:300]
                _hook("event", "error", "job", job.get("kind") or "job", f"{type(ex).__name__}: {ex}")
        threading.Thread(target=work, daemon=True).start()
        return jid

    def get(self, jid):
        j = self.jobs.get(jid)
        return dict(j) if j else None

    def start_docs(self, question, docs, kind="read"):
        """Read a set of documents [{name, text}] - one call if they fit, otherwise notes per part
        (map) combined into one answer (reduce). Runs in the background; poll with get()."""
        key = hashlib.sha1((self.llm.model_for(kind) + kind + question + json.dumps(docs, sort_keys=True)).encode()).hexdigest()
        jid = uuid.uuid4().hex[:10]
        if key in self.cache:
            _hook("count", "cache:hit:" + kind)
            self.jobs[jid] = {"id": jid, "status": "done", "answer": self.cache[key], "cached": True}
            return jid
        _hook("count", "cache:miss:" + kind)
        budget = self.llm.chunk_for(kind) - 600
        parts = _pack_docs(docs, budget)
        job = {"id": jid, "status": "running", "answer": None, "started": time.time(),
               "parts": len(parts), "done_parts": 0}
        with self.lock:
            self.jobs[jid] = job

        def work():
            try:
                q = f"Question: {question}\n\n" if question else ""
                if len(parts) == 1:
                    ans = self.llm.chat(READ_SYSTEM, q + parts[0], timeout=900, max_tokens=900, kind=kind)
                else:
                    notes = []
                    for p in parts:
                        notes.append(self.llm.chat(MAP_SYSTEM, q + p, timeout=900, max_tokens=600, kind=kind))
                        job["done_parts"] += 1
                    while len("\n\n".join(notes)) > budget and len(notes) > 1:     # reduce in groups
                        groups, cur = [], []
                        for n in notes:
                            if cur and len("\n\n".join(cur + [n])) > budget:
                                groups.append(cur)
                                cur = []
                            cur.append(n[:budget])
                        groups.append(cur)
                        notes = [self.llm.chat(REDUCE_SYSTEM, q + "Notes:\n\n" + "\n\n---\n\n".join(g),
                                               timeout=900, max_tokens=700, kind=kind) for g in groups]
                    ans = self.llm.chat(REDUCE_SYSTEM, q + "Notes:\n\n" + "\n\n---\n\n".join(notes),
                                        timeout=900, max_tokens=900, kind=kind)
                job["answer"], job["status"] = ans, "done"
                self.cache[key] = ans
            except Exception as ex:
                job["status"], job["error"] = "error", str(ex)[:300]
                _hook("event", "error", "job", job.get("kind") or "job", f"{type(ex).__name__}: {ex}")
        threading.Thread(target=work, daemon=True).start()
        return jid


def _numbered(text, start=1):
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.splitlines(), start))


def _pack_docs(docs, budget):
    """Numbered text for each document, split by lines where a file is too big, packed into parts."""
    pieces = []
    for d in docs:
        lines = (d.get("text") or "").splitlines()
        name = d.get("name") or "text"
        start, cur, size = 1, [], 0
        for i, line in enumerate(lines, 1):
            line = line[:1000]
            if cur and size + len(line) + 8 > budget - 200:
                pieces.append(f"=== {name} (lines {start}-{i - 1} of {len(lines)}) ===\n" + _numbered("\n".join(cur), start))
                start, cur, size = i, [], 0
            cur.append(line)
            size += len(line) + 8
        if cur or not lines:
            label = f"lines {start}-{len(lines)} of {len(lines)}" if start > 1 else f"{len(lines)} lines"
            pieces.append(f"=== {name} ({label}) ===\n" + _numbered("\n".join(cur), start))
    parts, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) + 2 > budget:
            parts.append(cur)
            cur = ""
        cur = (cur + "\n\n" + p) if cur else p
    if cur:
        parts.append(cur)
    return parts or ["(empty)"]


# ============================================================================ savings counter
# Tokens are estimated as characters / 4 (close enough for English + log text).
CHARS_PER_TOKEN = 4


def record_usage(store, source, kind, in_chars, out_chars, calls=1):
    """source 'local' = the local model read in_chars and wrote out_chars;
    source 'claude' = a connector tool returned out_chars to Claude (in_chars unused)."""
    day = time.strftime("%Y-%m-%d")
    with store.lock:
        store.db.execute(
            "INSERT INTO ai_usage VALUES (?,?,?,?,?,?) ON CONFLICT(day, source, kind) DO UPDATE SET "
            "calls=calls+excluded.calls, in_chars=in_chars+excluded.in_chars, out_chars=out_chars+excluded.out_chars",
            (day, source, kind[:60], calls, int(in_chars), int(out_chars)))
        store.db.commit()


def savings(store, days=7):
    """Per day: what the local model read (that Claude didn't have to), what it handed on,
    and everything the connector returned to Claude."""
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
    with store.lock:
        rows = store.db.execute("SELECT day, source, kind, calls, in_chars, out_chars FROM ai_usage "
                                "WHERE day >= ? ORDER BY day", (since,)).fetchall()
    tok = lambda c: round(c / CHARS_PER_TOKEN)
    per = {}
    for day, src, kind, calls, i, o in rows:
        d = per.setdefault(day, {"day": day, "summaries": 0, "triaged": 0, "local_read": 0, "local_wrote": 0,
                                 "claude_received": 0, "claude_calls": 0, "tools": {}})
        if src == "local":
            d["local_read"] += tok(i)
            d["local_wrote"] += tok(o)
            d["summaries" if kind != "triage" else "triaged"] += calls
        else:
            d["claude_received"] += tok(o)
            d["claude_calls"] += calls
            d["tools"][kind] = d["tools"].get(kind, 0) + tok(o)
    out = []
    for d in per.values():
        # avoided = what the local model read minus the short answer it passed on
        d["avoided"] = max(0, d["local_read"] - d["local_wrote"])
        total = d["avoided"] + d["claude_received"]
        d["saved_pct"] = round(100 * d["avoided"] / total) if total else 0
        d["tools"] = dict(sorted(d["tools"].items(), key=lambda kv: -kv[1])[:8])
        out.append(d)
    tot = {k: sum(d[k] for d in out) for k in ("summaries", "triaged", "local_read", "local_wrote",
                                                 "claude_received", "claude_calls", "avoided")}
    t = tot["avoided"] + tot["claude_received"]
    tot["saved_pct"] = round(100 * tot["avoided"] / t) if t else 0
    return {"days": out[::-1], "total": tot, "chars_per_token": CHARS_PER_TOKEN}


def lines_text(lines, max_chars):
    out, n = [], 0
    for l in lines:
        s = (f"{time.strftime('%m-%d %H:%M:%S', time.localtime(l['ts'] / 1000))} [{l.get('level', '')}] "
             f"{l.get('group', '')} | {l['message'][:1500]}")
        if n + len(s) > max_chars:
            out.append(f"... ({len(lines) - len(out)} more lines not shown)")
            break
        out.append(s)
        n += len(s)
    return "\n".join(out)
