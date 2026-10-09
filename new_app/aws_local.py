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
 "confidence": number 0..1}
Rules: "actionable" = needs a code/config fix by the developer. "noise" = harmless / already handled
(e.g. retried successfully, informational). "external" = caused by a partner service or their data
(outage, rate limit, expired token on their side). Base everything only on the lines given; if unsure
say "unclear" with low confidence. No markdown, no extra text."""

SUMMARY_SYSTEM = """You summarise logs from AWS Lambda / Step Functions integrations for a developer.
Be brief and concrete: what happened, in order, what failed and why (if visible), and which record IDs
or functions are involved. Use at most 8 short bullet points. Quote exact error messages when useful.
Do not invent anything that is not in the lines. If asked a question, answer it first in one sentence."""


class LocalLLM:
    def __init__(self, cfg):
        c = cfg.get("local_llm") or {}
        self.enabled = c.get("enabled", True)
        self.url = (c.get("url") or "http://localhost:11434").rstrip("/")
        self.model = c.get("model") or "gpt-oss:20b"
        self.num_ctx = int(c.get("num_ctx") or 16384)
        self.max_input_chars = int(c.get("max_input_chars") or 40000)
        self.state = {"ok": False, "detail": "not checked yet", "checked": 0}
        self.lock = threading.Lock()          # one generation at a time (it's one GPU)

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

    @property
    def ready(self):
        return self.check()["ok"]

    # ------------------------------------------------------------ generation
    def chat(self, system, user, as_json=False, timeout=300, max_tokens=700):
        body = {"model": self.model, "stream": False,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user[: self.max_input_chars]}],
                "options": {"num_ctx": self.num_ctx, "temperature": 0.2, "num_predict": max_tokens}}
        if as_json:
            body["format"] = "json"
        if self.model.startswith("gpt-oss"):
            body["think"] = "low"                 # gpt-oss: keep its reasoning short
        elif re.match(r"(qwen3|deepseek-r1)", self.model):
            body["think"] = False
        with self.lock:
            for attempt in range(2):
                req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        msg = json.loads(r.read()).get("message", {})
                    text = (msg.get("content") or "").strip()
                    break
                except urllib.error.HTTPError as ex:
                    detail = ex.read().decode(errors="ignore")
                    if attempt == 0 and "think" in detail and "think" in body:
                        body.pop("think")          # model doesn't support the think option
                        continue
                    raise RuntimeError(f"Ollama error {ex.code}: {detail[:200]}")
        if not as_json:
            return text
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0) if m else text)
        except ValueError:
            return {"verdict": "unclear", "category": "unknown", "summary": text[:300], "confidence": 0.1}


# ============================================================================ storage
def init_db(store):
    with store.lock:
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS ai_triage(
                etype TEXT, sig TEXT, profile TEXT, at INTEGER, model TEXT, count INTEGER, data TEXT,
                PRIMARY KEY(etype, sig, profile));
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

    def candidates(self):
        """(profile, pattern) pairs: regressions, new, spiking, then the busiest - untriaged first."""
        tr = get_triage(self.store)
        out = []
        for prof in sorted({w.profile for w in self.workers}):
            rows = self.store.annotate(self.store.summary({"hours": 24, "profile": prof, "level": "error,suspect",
                                                           "hide_muted": "1"}), prof)
            for i, g in enumerate(rows[:40]):
                if g.get("below_threshold"):
                    continue
                prio = (0 if g.get("regression") else 1 if g.get("is_new") else 2 if g.get("is_spike") else 3 + i)
                t = tr.get((g["etype"], g["sig"], prof))
                stale = (not t or time.time() * 1000 - t["at"] > DAY_MS or
                         (g["count"] > 3 * max(t["count_at_triage"] or 1, 1) and g["count"] - (t["count_at_triage"] or 0) > 20))
                if stale and (prio < 3 or i < self.cfg.get("local_llm", {}).get("triage_top_n", 8)):
                    out.append((prio, prof, g))
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
        res = self.llm.chat(TRIAGE_SYSTEM, user, as_json=True, timeout=600, max_tokens=500)
        res = {"verdict": res.get("verdict") if res.get("verdict") in VERDICTS else "unclear",
               "category": res.get("category") if res.get("category") in CATEGORIES else "unknown",
               "summary": str(res.get("summary", ""))[:400], "likely_cause": str(res.get("likely_cause", ""))[:300],
               "next_step": str(res.get("next_step", ""))[:300],
               "confidence": max(0.0, min(1.0, float(res.get("confidence") or 0)))}
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
                    for _, prof, g in self.candidates()[: self.cfg.get("local_llm", {}).get("triage_per_cycle", 6)]:
                        self.running_now = f"{prof}: {g['etype']}"
                        self.triage_one(prof, g)
                        self.done += 1
                    self.last_error = None
            except Exception as ex:
                self.last_error = str(ex)[:300]
            self.running_now = None
            time.sleep(self.cfg.get("local_llm", {}).get("triage_every_minutes", 5) * 60)


# ============================================================================ on-demand jobs
class Jobs:
    """Summaries can take a while on a local GPU, so they run as jobs; callers poll."""

    def __init__(self, llm):
        self.llm, self.jobs, self.cache, self.lock = llm, {}, {}, threading.Lock()

    def start(self, question, text, kind="summary"):
        key = hashlib.sha1((self.llm.model + kind + question + text).encode()).hexdigest()
        if key in self.cache:
            jid = uuid.uuid4().hex[:10]
            self.jobs[jid] = {"id": jid, "status": "done", "answer": self.cache[key], "cached": True}
            return jid
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
                ans = self.llm.chat(SUMMARY_SYSTEM, q + "Log lines:\n" + text, timeout=900, max_tokens=900)
                job["answer"], job["status"] = ans, "done"
                self.cache[key] = ans
            except Exception as ex:
                job["status"], job["error"] = "error", str(ex)[:300]
        threading.Thread(target=work, daemon=True).start()
        return jid

    def get(self, jid):
        j = self.jobs.get(jid)
        return dict(j) if j else None


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
