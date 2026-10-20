"""
Health & efficiency stats for the AWS Error Feed: is the tower / local model setup actually working,
and what should be improved next?

Records (in errors.db, kept KEEP_DAYS days):
  llm_calls   - every local-model call: wait in queue, load / prompt / generation time, tokens/s,
                whether the input was cut to fit the context, whether the answer hit max_tokens,
                whether JSON parsed, errors.
  tool_calls  - every Claude connector tool call: time, output size, errors (reported by mcp_server).
  counters    - daily counters: cache hits/misses, triage verdicts, evidence downgrades.
  sys_events  - the dashboard's own error log: model errors, triage/job failures, crashed threads,
                AWS accounts losing/regaining their connection, settings at each start.

Also:
  DrainShadow - runs Drain3 (log template mining) next to the normal grouping, without changing it,
                and reports how many patterns it would merge - so you can see if switching is worth it.
  snapshot()  - all of the above for the Health tab / system_health tool, plus a "what to improve
                next" list built from the rules below.
"""
import json
import os
import re
import threading
import time
import traceback
import urllib.request

import aws_local

# ------------------------------------------------------------------ settings
KEEP_DAYS = 14                    # how long health rows are kept
EVENT_DEDUPE_MINUTES = 10         # the same event within this window just bumps a counter
SNAPSHOT_CACHE_SECONDS = 15
RUNTIME_CACHE_SECONDS = 30

# "what to improve next" thresholds
LLM_ERROR_PCT = 5                 # % failed model calls (24h)
TRUNCATED_PCT = 10                # % calls whose input didn't fit the context
CUTOFF_PCT = 10                   # % answers that hit max_tokens
JSON_FAIL_PCT = 5                 # % triage answers that weren't valid JSON
MIN_GEN_TPS = 15                  # generation tokens/second below this = something's slow
MAX_INTERACTIVE_WAIT_S = 30       # p95 time a Claude request waits behind background work
MAX_RELOADS_PER_DAY = 10          # model loaded from disk again (keep_alive too short)
UNCLEAR_PCT = 40                  # % triage verdicts "unclear"
DOWNGRADE_PCT = 25                # % triage claims not backed by the lines
MIN_TRIAGED_FOR_QUALITY = 10
TRIAGE_BACKLOG_MAX = 30
GPU_FIT_PCT = 98                  # % of the model in VRAM; below = partly on CPU (slow)
MIN_SAVED_PCT = 50
MIN_CLAUDE_TOKENS_FOR_SAVINGS = 20000
BIG_TOOL_OUTPUT_CHARS = 12000
DRAIN_GAIN_PCT = 15               # Drain3 would cut patterns by at least this -> worth switching
DRAIN_MIN_PATTERNS = 30
DB_WARN_MB = 1500

DRAIN_LEVELS = ("error", "suspect", "warning")
DAY_MS = 86_400_000


def _now_ms():
    return int(time.time() * 1000)


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def _p95(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(0.95 * len(xs)))] if xs else 0


def _day(ms):
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))


# ================================================================== Drain3 grouping
HTTP_CODE_RX = re.compile(r"\b[45]\d\d\b")


class DrainShadow:
    """Drain3 log-template mining over the normalized messages.

    mode "shadow": only measures - every distinct (profile, type, message) pattern is fed once and the
    Health tab shows how many patterns Drain3 would merge.
    mode "on": the same, and the Drain3 template becomes the stored pattern (Store calls resolve()).
    When a template gets more general, remap(old, new) moves events, baseline, mutes, notes and triage.

    Never merged: different error types, different HTTP status codes (429 vs 500), run_summary
    patterns (one per function) and very short messages."""

    def __init__(self, cfg):
        g = cfg.get("grouping") or {}
        self.mode = str(g.get("drain3", "shadow")).lower()
        self.sim = float(g.get("drain3_similarity", 0.4))
        self.min_tokens = int(g.get("drain3_min_tokens", 3))
        self.max_keys = int(g.get("drain3_max_patterns", 200000))
        self.available, self.error = False, None
        self.lock = threading.RLock()
        self.miners = {}         # miner key (profile, etype, http codes) -> TemplateMiner
        self.keys = {}           # (profile, etype, raw sig) -> [miner key, cluster id, last_ts, lines]
        self.tpl = {}            # (miner key, cluster id) -> template currently stored as the pattern
        self.cost_s, self.fed, self.seeded, self.remapped = 0.0, 0, False, 0
        if self.mode == "off":
            self.error = "off in config (grouping.drain3)"
            return
        try:
            from drain3 import TemplateMiner
            from drain3.template_miner_config import TemplateMinerConfig
            self._TM, self._Cfg = TemplateMiner, TemplateMinerConfig
            self.available = True
        except ImportError:
            self.error = "drain3 isn't installed - on the tower run: pip install drain3 (tower_tune.ps1 does it)"
            self.mode = "shadow"

    @property
    def active(self):
        return self.available and self.mode == "on"

    def _miner(self, mkey):
        m = self.miners.get(mkey)
        if m is None:
            c = self._Cfg()
            c.drain_sim_th, c.drain_depth, c.profiling_enabled = self.sim, 4, False
            c.parametrize_numeric_tokens = True
            m = self.miners[mkey] = self._TM(config=c)
        return m

    def eligible(self, etype, sig):
        return bool(sig) and not str(etype).startswith("run_summary") and len(sig.split()) >= self.min_tokens

    def resolve(self, prof, etype, sig, ts, lines=1):
        """-> (pattern to store, [(old, new) template changes to apply])."""
        if not self.available or not self.eligible(etype, sig):
            return sig, []
        k = (prof, etype, sig)
        with self.lock:
            e = self.keys.get(k)
            if e:
                e[2], e[3] = max(e[2], ts), e[3] + lines
                return self.tpl.get((e[0], e[1]), sig), []
            if len(self.keys) >= self.max_keys:      # safety valve: start over
                self.keys.clear(), self.miners.clear(), self.tpl.clear()
            mkey = (prof, etype, ",".join(sorted(set(HTTP_CODE_RX.findall(sig)))))
            t0 = time.perf_counter()
            try:
                r = self._miner(mkey).add_log_message(sig)
            except Exception as ex:
                self.error = f"drain3 failed: {ex}"[:200]
                return sig, []
            self.cost_s += time.perf_counter() - t0
            self.fed += 1
            cid = r["cluster_id"]
            self.keys[k] = [mkey, cid, ts, lines]
            new = (r.get("template_mined") or sig)[:200]
            old = self.tpl.get((mkey, cid))
            self.tpl[(mkey, cid)] = new
            changes = [(old, new)] if old and old != new else []
            return new, changes

    def feed(self, prof, etype, sig, ts, lines=1):
        self.resolve(prof, etype, sig, ts, lines)

    def feed_rows(self, rows):
        """Shadow mode: rows as built in Store.add_many (id, ts, profile, account, region, group, stream,
        kind, etype, sig, message, live, level, hint)."""
        for r in rows:
            if r[12] in DRAIN_LEVELS:
                self.feed(r[2], r[8], r[9], r[1])

    def seed(self, store, since_ms):
        """Shadow mode: learn the patterns already in the cache."""
        if not self.available:
            return
        with store.lock:
            rows = store.db.execute(
                "SELECT profile, etype, sig, MAX(ts), COUNT(*) FROM events WHERE ts >= ? AND level IN "
                "('error','suspect','warning') GROUP BY profile, etype, sig", (since_ms,)).fetchall()
        for p, et, sg, ts, n in rows:
            self.feed(p, et, sg, ts, n)
        self.seeded = True

    def migrate(self, store, remap, since_ms):
        """Mode "on", at start (before the workers run): learn every stored pattern (24h cache + hourly
        baseline), then rename each to its Drain3 template. Re-running it is cheap: templates map to
        themselves."""
        if not self.active:
            return 0
        t0 = time.time()
        with store.lock:
            rows = store.db.execute(
                "SELECT profile, etype, sig, MAX(ts), COUNT(*) FROM events WHERE ts >= ? AND level IN "
                "('error','suspect','warning') GROUP BY profile, etype, sig", (since_ms,)).fetchall()
            rows += store.db.execute(
                "SELECT profile, etype, sig, MAX(hour) * 3600000, SUM(n) FROM pattern_hours "
                "GROUP BY profile, etype, sig").fetchall()
        for p, et, sg, ts, n in rows:
            self.resolve(p, et, sg, ts or 0, n or 0)
        n = 0
        with self.lock:
            final = [(k, self.tpl.get((v[0], v[1]))) for k, v in self.keys.items()]
        for (p, et, sg), t in final:
            if t and t != sg:
                remap(p, et, sg, t)
                n += 1
        self.seeded, self.remapped = True, self.remapped + n
        print(f"Drain3 grouping: {len(final)} patterns -> {len(set(self.tpl.values()))} templates, "
              f"{n} renamed in {time.time() - t0:.0f}s", flush=True)
        return n

    def prune(self, cutoff_ms):
        with self.lock:
            for k in [k for k, v in self.keys.items() if v[2] < cutoff_ms]:
                del self.keys[k]

    def stats(self, since_ms, top=15):
        st = {"mode": self.mode, "available": self.available, "error": self.error, "seeded": self.seeded,
              "similarity": self.sim, "renamed": self.remapped}
        if not self.available:
            return st
        with self.lock:
            items = [(k, list(v)) for k, v in self.keys.items() if v[2] >= since_ms]
            clusters = {}
            for k, v in items:
                clusters.setdefault((v[0], v[1]), []).append((k, v[3]))
            merges = sorted((c for c in clusters.items() if len(c[1]) > 1), key=lambda c: -len(c[1]))[:top]
            merge_rows = [{"profile": mk[0], "template": (self.tpl.get((mk, cid)) or "")[:240],
                           "patterns": len(ks), "lines": sum(n for _, n in ks),
                           "examples": [f"{k[1]}: {k[2]}"[:160] for k, _ in sorted(ks, key=lambda x: -x[1])[:4]]}
                          for (mk, cid), ks in merges]
        per = {}
        for k, v in items:
            d = per.setdefault(k[0], {"patterns": 0, "clusters": set()})
            d["patterns"] += 1
            d["clusters"].add((v[0], v[1]))
        n_sig, n_cl = len(items), len(clusters)
        st.update(patterns=n_sig, drain_clusters=n_cl,
                  reduction_pct=round(100 * (1 - n_cl / n_sig)) if n_sig else 0,
                  avg_us=round(1e6 * self.cost_s / self.fed) if self.fed else None,
                  merge_candidates=merge_rows,
                  per_profile=sorted(({"profile": p, "patterns": d["patterns"], "clusters": len(d["clusters"]),
                                       "reduction_pct": round(100 * (1 - len(d["clusters"]) / d["patterns"]))}
                                      for p, d in per.items()), key=lambda x: -x["patterns"]))
        return st


# ================================================================== recorder
class Health:
    def __init__(self, store, cfg, db_path=None):
        self.store, self.cfg, self.db_path = store, cfg, db_path
        self.started = _now_ms()
        self._dedupe = {}
        self._snap, self._snap_at = None, 0
        self._rt, self._rt_at = {}, 0
        with store.lock:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS llm_calls(
                    at INTEGER, kind TEXT, model TEXT, prio INTEGER, ok INTEGER, err TEXT,
                    in_chars INTEGER, out_chars INTEGER, prompt_tok INTEGER, gen_tok INTEGER,
                    wait_ms INTEGER, load_ms INTEGER, prompt_ms INTEGER, gen_ms INTEGER, total_ms INTEGER,
                    num_ctx INTEGER, truncated INTEGER, cut_off INTEGER, json_ok INTEGER);
                CREATE INDEX IF NOT EXISTS ix_llm_at ON llm_calls(at);
                CREATE TABLE IF NOT EXISTS tool_calls(at INTEGER, tool TEXT, ms INTEGER, out_chars INTEGER,
                    ok INTEGER, err TEXT);
                CREATE INDEX IF NOT EXISTS ix_tool_at ON tool_calls(at);
                CREATE TABLE IF NOT EXISTS counters(day TEXT, key TEXT, n INTEGER, PRIMARY KEY(day, key));
                CREATE TABLE IF NOT EXISTS sys_events(id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER,
                    last_at INTEGER, level TEXT, source TEXT, kind TEXT, message TEXT, n INTEGER);
                CREATE INDEX IF NOT EXISTS ix_sysev_at ON sys_events(last_at);
            """)
            store.db.commit()
        self.prune()

    # ---- writes
    def _exec(self, sql, params=()):
        with self.store.lock:
            cur = self.store.db.execute(sql, params)
            self.store.db.commit()
            return cur

    def prune(self):
        cut = _now_ms() - KEEP_DAYS * DAY_MS
        for t, col in (("llm_calls", "at"), ("tool_calls", "at"), ("sys_events", "last_at")):
            self._exec(f"DELETE FROM {t} WHERE {col} < ?", (cut,))
        self._exec("DELETE FROM counters WHERE day < ?", (_day(cut),))

    def llm_call(self, r):
        try:
            self._exec("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                _now_ms(), r.get("kind"), r.get("model"), r.get("prio", 0), 1 if not r.get("err") else 0,
                r.get("err"), r.get("in_chars"), r.get("out_chars"), r.get("prompt_tok"), r.get("gen_tok"),
                r.get("wait_ms"), r.get("load_ms"), r.get("prompt_ms"), r.get("gen_ms"), r.get("total_ms"),
                r.get("num_ctx"), 1 if r.get("truncated") else 0, 1 if r.get("cut_off") else 0, r.get("json_ok")))
            if r.get("err"):
                self.event("error", "local_model", r.get("kind") or "call", r["err"])
        except Exception as ex:
            print("health: llm_call record failed:", ex)

    def tool_call(self, tool, ms, out_chars, err=None):
        try:
            self._exec("INSERT INTO tool_calls VALUES (?,?,?,?,?,?)",
                       (_now_ms(), tool[:60], int(ms or 0), int(out_chars or 0), 0 if err else 1,
                        (err or None) and str(err)[:300]))
            if err:
                self.event("error", "connector", tool[:60], err)
        except Exception as ex:
            print("health: tool_call record failed:", ex)

    def count(self, key, n=1):
        try:
            self._exec("INSERT INTO counters VALUES (?,?,?) ON CONFLICT(day, key) DO UPDATE SET n = n + excluded.n",
                       (time.strftime("%Y-%m-%d"), key[:120], int(n)))
        except Exception:
            pass

    def event(self, level, source, kind, message):
        """The dashboard's own log. Repeats within EVENT_DEDUPE_MINUTES just bump the count."""
        try:
            message = str(message)[:1000]
            now = _now_ms()
            k = (level, source, kind, message[:200])
            hit = self._dedupe.get(k)
            if hit and now - hit[1] < EVENT_DEDUPE_MINUTES * 60_000:
                self._exec("UPDATE sys_events SET n = n + 1, last_at = ? WHERE id = ?", (now, hit[0]))
                hit[1] = now
                return
            cur = self._exec("INSERT INTO sys_events(at, last_at, level, source, kind, message, n) "
                             "VALUES (?,?,?,?,?,?,1)", (now, now, level, source, kind, message))
            self._dedupe[k] = [cur.lastrowid, now]
            if len(self._dedupe) > 2000:
                self._dedupe.clear()
        except Exception as ex:
            print("health: event record failed:", ex)

    def thread_hook(self, args):
        """threading.excepthook: a background thread crashed."""
        tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))[-800:]
        self.event("error", "thread", getattr(args.thread, "name", "thread"),
                   f"{args.exc_type.__name__}: {args.exc_value}\n{tb}")

    def status_change(self, key, was_ok, now_ok, err):
        if was_ok and not now_ok:
            self.event("warning", "aws", key, f"lost connection: {err}")
        elif now_ok and was_ok is False:
            self.event("info", "aws", key, "connected again")

    # ---- reads
    def runtime(self, llm):
        """What Ollama has loaded right now: GPU share, context size, version."""
        if time.time() - self._rt_at < RUNTIME_CACHE_SECONDS:
            return self._rt
        rt = {"reachable": False}
        try:
            with urllib.request.urlopen(llm.url + "/api/version", timeout=3) as r:
                rt["version"] = json.loads(r.read()).get("version")
            with urllib.request.urlopen(llm.url + "/api/ps", timeout=3) as r:
                ps = json.loads(r.read()).get("models", [])
            rt["reachable"] = True
            rt["loaded"] = [{"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1),
                             "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1),
                             "gpu_pct": round(100 * (m.get("size_vram") or 0) / m["size"]) if m.get("size") else None,
                             "context": m.get("context_length"), "expires": m.get("expires_at")} for m in ps]
        except Exception as ex:
            rt["error"] = str(ex)[:200]
        self._rt, self._rt_at = rt, time.time()
        return rt

    def _rows(self, sql, params=()):
        with self.store.lock:
            return [dict(r) for r in self.store.db.execute(sql, params).fetchall()]

    @staticmethod
    def agg(rows):
        n = len(rows)
        if not n:
            return {"calls": 0}
        ok = [r for r in rows if r["ok"]]
        gen_tok = sum(r["gen_tok"] or 0 for r in ok)
        gen_ms = sum(r["gen_ms"] or 0 for r in ok)
        pr_tok = sum(r["prompt_tok"] or 0 for r in ok)
        pr_ms = sum(r["prompt_ms"] or 0 for r in ok)
        js = [r for r in ok if r["json_ok"] is not None]
        return {"calls": n, "errors": n - len(ok), "error_pct": _pct(n - len(ok), n),
                "avg_s": round(sum(r["total_ms"] or 0 for r in ok) / max(1, len(ok)) / 1000, 1),
                "p95_s": round(_p95([r["total_ms"] for r in ok]) / 1000, 1),
                "gen_tps": round(gen_tok / (gen_ms / 1000), 1) if gen_ms else None,
                "prompt_tps": round(pr_tok / (pr_ms / 1000)) if pr_ms else None,
                "wait_p95_s": round(_p95([r["wait_ms"] for r in rows if r["prio"] == 0]) / 1000, 1),
                "wait_bg_p95_s": round(_p95([r["wait_ms"] for r in rows if r["prio"] == 1]) / 1000, 1),
                "truncated_pct": _pct(sum(1 for r in ok if r["truncated"]), len(ok)),
                "cut_off_pct": _pct(sum(1 for r in ok if r["cut_off"]), len(ok)),
                "json_fail_pct": _pct(sum(1 for r in js if not r["json_ok"]), len(js)),
                "reloads": sum(1 for r in ok if (r["load_ms"] or 0) > 2000),
                "tokens_in": pr_tok, "tokens_out": gen_tok}

    def counters(self, since_day):
        out = {}
        for r in self._rows("SELECT day, key, n FROM counters WHERE day >= ?", (since_day,)):
            out.setdefault(r["day"], {})[r["key"]] = r["n"]
        return out

    @staticmethod
    def triage_quality(c):
        verdicts = {k.split(":", 2)[2]: v for k, v in c.items() if k.startswith("triage:verdict:")}
        n = sum(verdicts.values())
        return {"triaged": n, "verdicts": verdicts, "unclear_pct": _pct(verdicts.get("unclear", 0), n),
                "downgraded_pct": _pct(c.get("triage:downgraded", 0), n),
                "evidence_dropped_pct": _pct(c.get("triage:evidence_dropped", 0), n)}

    @staticmethod
    def cache_rate(c):
        hit = sum(v for k, v in c.items() if k.startswith("cache:hit:"))
        miss = sum(v for k, v in c.items() if k.startswith("cache:miss:"))
        return {"hits": hit, "misses": miss, "hit_pct": _pct(hit, hit + miss)}

    def snapshot(self, llm, triage=None, drain=None, keep_hours=24, force=False):
        if not force and self._snap and time.time() - self._snap_at < SNAPSHOT_CACHE_SECONDS:
            return self._snap
        now = _now_ms()
        calls = self._rows("SELECT * FROM llm_calls WHERE at >= ?", (now - 7 * DAY_MS,))
        tools = self._rows("SELECT * FROM tool_calls WHERE at >= ?", (now - 7 * DAY_MS,))
        days = [_day(now - i * DAY_MS) for i in range(6, -1, -1)]
        cnt = self.counters(days[0])
        merged = lambda dd: {k: sum(cnt.get(d, {}).get(k, 0) for d in dd)
                             for k in {k for d in dd for k in cnt.get(d, {})}}
        last24 = [r for r in calls if r["at"] >= now - DAY_MS]
        by_kind = {}
        for r in last24:
            by_kind.setdefault(r["kind"] or "?", []).append(r)
        sav = aws_local.savings(self.store, 7)
        sav_by_day = {d["day"]: d for d in sav["days"]}
        settings = self._rows("SELECT at, message FROM sys_events WHERE source='settings' ORDER BY at")

        def setting_on(day):
            end = time.mktime(time.strptime(day, "%Y-%m-%d")) * 1000 + DAY_MS
            s = [x["message"] for x in settings if x["at"] < end]
            return s[-1] if s else ""

        daily = []
        for d in days:
            rows = [r for r in calls if _day(r["at"]) == d]
            a, c, s = self.agg(rows), cnt.get(d, {}), sav_by_day.get(d, {})
            tq = self.triage_quality(c)
            daily.append({"day": d, "calls": a.get("calls", 0), "error_pct": a.get("error_pct"),
                          "gen_tps": a.get("gen_tps"), "p95_s": a.get("p95_s"), "wait_p95_s": a.get("wait_p95_s"),
                          "truncated_pct": a.get("truncated_pct"), "triaged": tq["triaged"],
                          "unclear_pct": tq["unclear_pct"], "downgraded_pct": tq["downgraded_pct"],
                          "cache_hit_pct": self.cache_rate(c)["hit_pct"],
                          "tower_read": s.get("local_read", 0), "claude_received": s.get("claude_received", 0),
                          "saved_pct": s.get("saved_pct"), "settings": setting_on(d)})

        tool_rows = []
        for name in sorted({t["tool"] for t in tools}):
            for label, since in (("24h", now - DAY_MS), ("7d", now - 7 * DAY_MS)):
                rs = [t for t in tools if t["tool"] == name and t["at"] >= since]
                if label == "24h" and not rs:
                    continue
                errs = [t for t in rs if not t["ok"]]
                tool_rows.append({"tool": name, "window": label, "calls": len(rs),
                                  "avg_ms": round(sum(t["ms"] for t in rs) / len(rs)) if rs else 0,
                                  "p95_ms": _p95([t["ms"] for t in rs]),
                                  "avg_chars": round(sum(t["out_chars"] for t in rs) / len(rs)) if rs else 0,
                                  "max_chars": max((t["out_chars"] for t in rs), default=0),
                                  "errors": len(errs), "last_error": errs[-1]["err"] if errs else None})
                break                                   # 24h if used today, else 7d

        ev = self._rows("SELECT * FROM sys_events ORDER BY last_at DESC LIMIT 80")
        ev24 = self._rows("SELECT level, SUM(n) AS n FROM sys_events WHERE last_at >= ? GROUP BY level",
                          (now - DAY_MS,))
        db_mb = None
        if self.db_path:
            db_mb = round(sum(os.path.getsize(self.db_path + s) for s in ("", "-wal")
                              if os.path.exists(self.db_path + s)) / 1e6)
        st = self.store.get_status()
        snap = {
            "at": now, "since_start_min": round((now - self.started) / 60000),
            "model": {"name": llm.model, "ready": llm.state.get("ok"), "detail": llm.state.get("detail"),
                      "num_ctx": llm.num_ctx, "max_input_chars": llm.max_input_chars,
                      "parallel": llm.gate.slots, "queue": llm.gate.state(), "runtime": self.runtime(llm)},
            "llm": {"1h": self.agg([r for r in calls if r["at"] >= now - 3_600_000]), "24h": self.agg(last24),
                    "7d": self.agg(calls)},
            "by_kind": sorted(({"kind": k, **self.agg(v)} for k, v in by_kind.items()), key=lambda x: -x["calls"]),
            "triage": {"24h": self.triage_quality(cnt.get(days[-1], {})), "7d": self.triage_quality(merged(days)),
                       "backlog": getattr(triage, "backlog", None), "done_session": getattr(triage, "done", 0),
                       "last_error": getattr(triage, "last_error", None)},
            "cache": {"24h": self.cache_rate(cnt.get(days[-1], {})), "7d": self.cache_rate(merged(days))},
            "savings": sav["total"], "daily": daily, "tools": tool_rows,
            "drain": drain.stats(now - keep_hours * 3_600_000) if drain else None,
            "events": ev, "events_24h": {r["level"]: r["n"] for r in ev24},
            "accounts_down": sorted(k for k, v in st.items() if not v.get("ok") and v.get("error") != "starting…"),
            "db_mb": db_mb,
        }
        snap["recommendations"] = recommendations(snap)
        self._snap, self._snap_at = snap, time.time()
        return snap


# ================================================================== what to improve next
def recommendations(s):
    out = []

    def add(sev, area, title, fix=""):
        out.append({"sev": sev, "area": area, "title": title, "fix": fix})

    m, rt = s["model"], s["model"]["runtime"]
    a = s["llm"]["24h"]
    if not m["ready"]:
        add("bad", "Local model", f"Local model is off: {m['detail']}", "Start Ollama on the tower / pull the model.")
    for x in rt.get("loaded") or []:
        if x.get("gpu_pct") is not None and x["gpu_pct"] < GPU_FIT_PCT:
            add("bad", "GPU", f"{x['name']} is only {x['gpu_pct']}% on the GPU ({x['vram_gb']} of {x['size_gb']} GB) "
                "- the rest runs on the CPU, which is much slower.",
                "Run tower_tune.ps1 (KV cache q8_0 + flash attention) or lower local_llm.num_ctx in config.json.")
        if x.get("name") and not x["name"].startswith(m["name"].split(":")[0]):
            add("warn", "GPU", f"Ollama also has {x['name']} loaded - it competes for the 12 GB.",
                "Set OLLAMA_MAX_LOADED_MODELS=1 (tower_tune.ps1) or stop the other model.")
        if x.get("context") and x["context"] <= 8192 and x.get("gpu_pct", 100) >= GPU_FIT_PCT:
            add("info", "Context", f"Context is {x['context']} tokens. With the q8_0 KV cache, 16k fits in the same VRAM "
                "and big reads need half the map-reduce passes.", "Run tower_tune.ps1 (sets num_ctx 16384).")
    if a.get("calls"):
        if a["error_pct"] > LLM_ERROR_PCT:
            add("bad", "Local model", f"{a['errors']} of {a['calls']} model calls failed in 24h ({a['error_pct']}%).",
                "See the system log below for the errors (timeouts -> lower triage_per_cycle or num_ctx).")
        if a["truncated_pct"] > TRUNCATED_PCT:
            add("warn", "Context", f"{a['truncated_pct']}% of model inputs didn't fit the context and were cut.",
                "Raise local_llm.num_ctx (after tower_tune.ps1) or lower max_input_chars so map-reduce splits them.")
        if a["json_fail_pct"] > JSON_FAIL_PCT:
            add("warn", "Triage", f"{a['json_fail_pct']}% of triage answers weren't valid JSON.",
                "Usually a model that ignores format=json; try another model.")
        if a.get("gen_tps") is not None and a["gen_tps"] < MIN_GEN_TPS:
            add("warn", "Speed", f"Generation runs at {a['gen_tps']} tokens/s (24h average).",
                "Check the GPU share above; close other GPU apps on the tower.")
        if a["wait_p95_s"] > MAX_INTERACTIVE_WAIT_S:
            add("warn", "Queue", f"Requests from Claude waited up to {a['wait_p95_s']}s (p95) for the GPU.",
                "Lower local_llm.triage_per_cycle, or raise triage_every_minutes.")
        if a["reloads"] > MAX_RELOADS_PER_DAY:
            add("warn", "Speed", f"The model was loaded from disk {a['reloads']} times in 24h.",
                "Set OLLAMA_KEEP_ALIVE=24h (tower_tune.ps1).")
    for k in s["by_kind"]:
        if k["calls"] >= 5 and k.get("cut_off_pct", 0) > CUTOFF_PCT:
            add("warn", "Answers", f"{k['cut_off_pct']}% of '{k['kind']}' answers hit the max length and were cut off.",
                f"Raise max_tokens for '{k['kind']}' in aws_local.py.")
    for label in ("24h", "7d"):
        q = s["triage"][label]
        if q["triaged"] >= MIN_TRIAGED_FOR_QUALITY:
            if q["unclear_pct"] > UNCLEAR_PCT:
                add("warn", "Triage", f"{q['unclear_pct']}% of triage verdicts are 'unclear' ({label}).",
                    "Try a stronger model (e.g. qwen3:30b-a3b) and compare this number for a few days.")
            if q["downgraded_pct"] > DOWNGRADE_PCT:
                add("warn", "Triage", f"{q['downgraded_pct']}% of triage claims weren't backed by the log lines ({label}).",
                    "The model is guessing causes; a stronger model or more context lines should lower this.")
            break
    bl = s["triage"]["backlog"]
    if bl is not None and bl > TRIAGE_BACKLOG_MAX:
        add("warn", "Triage", f"{bl} patterns are waiting for triage.",
            "Raise local_llm.triage_per_cycle if the GPU is idle, or mute noisy patterns.")
    sv = s["savings"]
    if sv["claude_received"] >= MIN_CLAUDE_TOKENS_FOR_SAVINGS and sv["saved_pct"] < MIN_SAVED_PCT:
        big = sorted((t for t in s["tools"] if t["avg_chars"]), key=lambda t: -t["avg_chars"] * t["calls"])[:2]
        add("warn", "Savings", f"Only ~{sv['saved_pct']}% of the reading happened on the tower (7d).",
            "Biggest senders to Claude: " + ", ".join(f"{t['tool']} (~{t['avg_chars'] // 4} tokens/call)" for t in big))
    for t in s["tools"]:
        if t["errors"]:
            add("bad", "Connector", f"{t['tool']}: {t['errors']} error(s) ({t['window']}): {t['last_error']}", "")
        elif t["calls"] >= 3 and t["avg_chars"] > BIG_TOOL_OUTPUT_CHARS:
            add("info", "Connector", f"{t['tool']} sends ~{t['avg_chars'] // 4} tokens per call to Claude.",
                "Candidate for a brief default or a tower summary.")
    d = s.get("drain") or {}
    if d and not d.get("available"):
        add("info", "Grouping", f"Drain3 shadow grouping is off: {d.get('error')}", "")
    elif d.get("mode") == "on":
        add("ok", "Grouping", f"Drain3 grouping is on: {d.get('patterns', 0)} raw patterns shown as "
            f"{d.get('drain_clusters', 0)} (-{d.get('reduction_pct', 0)}%).", "")
    elif d.get("patterns", 0) >= DRAIN_MIN_PATTERNS:
        if d["reduction_pct"] >= DRAIN_GAIN_PCT:
            add("info", "Grouping", f"Drain3 would merge {d['patterns']} patterns into {d['drain_clusters']} "
                f"(-{d['reduction_pct']}%), i.e. fewer patterns to triage and read.",
                "Check the merge candidates below; if they look right, set grouping.drain3 to \"on\" in config.json.")
        else:
            add("ok", "Grouping", f"Drain3 finds only -{d['reduction_pct']}% - the current grouping is already tight.", "")
    e = s["events_24h"].get("error", 0)
    if e:
        add("warn", "System", f"{e} internal error(s) in 24h - see the system log.", "")
    if s["accounts_down"]:
        add("bad", "AWS", f"Not connected: {', '.join(s['accounts_down'])}", "aws sso login --profile <name> on the tower.")
    if s["db_mb"] and s["db_mb"] > DB_WARN_MB:
        add("warn", "Disk", f"errors.db is {s['db_mb']} MB.", "Lower keep_hours or exclude chatty log groups.")
    if not out:
        add("ok", "All", "Nothing to improve right now - everything is within its targets.", "")
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    return sorted(out, key=lambda x: order[x["sev"]])


def text_report(s):
    """Compact text for Claude (system_health tool)."""
    k = lambda n: f"{n / 1000:.1f}k" if n and n >= 1000 else str(n or 0)
    m, a, rt = s["model"], s["llm"]["24h"], s["model"]["runtime"]
    lines = ["WHAT TO IMPROVE NEXT:"]
    lines += [f"- [{r['sev']}] {r['area']}: {r['title']}" + (f" -> {r['fix']}" if r["fix"] else "")
              for r in s["recommendations"]]
    lo = ", ".join(f"{x['name']} {x['gpu_pct']}% GPU ctx {x['context']}" for x in rt.get("loaded") or []) or "nothing loaded"
    lines += ["", f"MODEL: {m['name']} ({'ready' if m['ready'] else m['detail']}); Ollama {rt.get('version', '?')}; "
              f"loaded: {lo}; queue: {m['queue']}"]
    if a.get("calls"):
        lines.append(f"24h: {a['calls']} calls, {a['error_pct']}% failed, avg {a['avg_s']}s (p95 {a['p95_s']}s), "
                     f"{a['gen_tps']} tok/s out, {a['prompt_tps']} tok/s in, Claude-request wait p95 {a['wait_p95_s']}s, "
                     f"{a['truncated_pct']}% inputs cut, {a['reloads']} reloads")
    t = s["triage"]["7d"]
    lines.append(f"TRIAGE 7d: {t['triaged']} verdicts {t['verdicts']}, unclear {t['unclear_pct']}%, "
                 f"downgraded {t['downgraded_pct']}%, backlog {s['triage']['backlog']}")
    c = s["cache"]["7d"]
    lines.append(f"CACHE 7d: {c['hit_pct']}% hits ({c['hits']}/{c['hits'] + c['misses']})")
    sv = s["savings"]
    lines.append(f"SAVINGS 7d: tower read ~{k(sv['local_read'])} tokens, Claude received ~{k(sv['claude_received'])}, "
                 f"~{sv['saved_pct']}% saved")
    lines.append("DAILY (day: calls, tok/s, p95s, unclear%, saved%, settings):")
    for d in s["daily"]:
        if d["calls"] or d["claude_received"]:
            lines.append(f"  {d['day']}: {d['calls']}, {d['gen_tps']}, {d['p95_s']}, {d['unclear_pct']}, "
                         f"{d['saved_pct']}, {d['settings'][:80]}")
    dr = s.get("drain") or {}
    if dr.get("available"):
        lines.append(f"DRAIN3 ({dr.get('mode')}): {dr.get('patterns')} patterns -> {dr.get('drain_clusters')} clusters "
                     f"(-{dr.get('reduction_pct')}%)")
        for mc in (dr.get("merge_candidates") or [])[:5]:
            lines.append(f"  {mc['profile']}: {mc['patterns']} patterns -> '{mc['template'][:100]}'")
    errs = [e for e in s["events"] if e["level"] in ("error", "warning")][:8]
    if errs:
        lines.append("RECENT SYSTEM ERRORS:")
        lines += [f"  {time.strftime('%m-%d %H:%M', time.localtime(e['last_at'] / 1000))} [{e['level']}] "
                  f"{e['source']}/{e['kind']} x{e['n']}: {e['message'][:160]}" for e in errs]
    return "\n".join(lines)
