"""
Web research on the tower: the cloud model plans search queries, Ollama's web search finds pages, Ollama's web
fetch reads them, and the model writes a short cited report. Claude only gets the report (~500-900 tokens)
instead of reading every search result and page itself.
Needs an Ollama API key (https://ollama.com/settings/keys) in config.json "research": {"api_key": "..."}
or the OLLAMA_API_KEY environment variable.
"""
import json
import os
import re
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

# ------------------------------------------------------------------ settings
SEARCH_URL = "https://ollama.com/api/web_search"
FETCH_URL = "https://ollama.com/api/web_fetch"
DEPTH = {"quick": (2, 3), "normal": (4, 6), "deep": (6, 10)}     # depth -> (search queries, pages read)
RESULTS_PER_QUERY = 5          # Ollama caps this at 10
PAGE_CHARS = 12000             # text kept per page
REPORT_TOKENS = 1800           # max answer size
MODEL_KIND = "research"        # cloud model (falls back to the local one)

PLAN_SYSTEM = """You plan web searches. Given a research question, reply JSON only:
{"queries": ["...", "..."]} with up to 6 short, specific search-engine queries that together cover the
question (different angles: official docs, comparisons, pricing, recent changes, known problems).
Include the current year when recency matters."""

REPORT_SYSTEM = """You are a careful research assistant. Answer the question using ONLY the numbered sources
given. Cite every claim with [n]. Lead with the direct answer, then the key facts / comparison (a compact
table is fine), then caveats, conflicting sources and what is unclear or out of date. Prefer official and
recent sources; say when a source is old or a vendor's own marketing. No filler. Max 450 words."""


def api_key(cfg):
    return ((cfg.get("research") or {}).get("api_key") or os.environ.get("OLLAMA_API_KEY") or "").strip()


def _post(url, body, key, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def web_search(q, key, n=RESULTS_PER_QUERY):
    return _post(SEARCH_URL, {"query": q, "max_results": max(1, min(10, n))}, key).get("results") or []


def web_fetch(url, key):
    return _post(FETCH_URL, {"url": url}, key, timeout=45)


def research(llm, key, question, depth="normal", progress=lambda s: None):
    n_q, n_pages = DEPTH.get(depth, DEPTH["normal"])
    progress("planning searches")
    try:
        plan = llm.chat(PLAN_SYSTEM, f"Today: {time.strftime('%Y-%m-%d')}\nQuestion: {question}", as_json=True,
                        max_tokens=400, kind=MODEL_KIND)
        if not isinstance(plan, dict):        # chat(as_json=True) returns the parsed object; be safe with text
            plan = json.loads(re.search(r"\{.*\}", str(plan), re.S).group(0))
        queries = [str(q) for q in plan.get("queries") or []][:n_q]
    except Exception:
        queries = []
    queries = queries or [question[:200]]
    progress(f"searching ({len(queries)} queries)")
    per_q = []
    for q in queries:
        try:
            per_q.append(web_search(q, key))
        except Exception as ex:
            per_q.append([])
            progress(f"search failed for {q!r}: {ex}")
    seen, picks = set(), []                      # round-robin across queries so every angle gets a page
    for i in range(RESULTS_PER_QUERY):
        for res in per_q:
            if i < len(res) and res[i].get("url") and res[i]["url"] not in seen:
                seen.add(res[i]["url"])
                picks.append(res[i])
    picks = picks[:n_pages]
    if not picks:
        raise RuntimeError("web search returned nothing (check the Ollama API key)")
    progress(f"reading {len(picks)} pages")

    def read(p):
        try:
            d = web_fetch(p["url"], key)
            return {"title": d.get("title") or p.get("title") or p["url"], "url": p["url"],
                    "text": (d.get("content") or p.get("content") or "")[:PAGE_CHARS]}
        except Exception:
            return {"title": p.get("title") or p["url"], "url": p["url"], "text": (p.get("content") or "")[:PAGE_CHARS]}
    with ThreadPoolExecutor(4) as ex:
        pages = list(ex.map(read, picks))
    progress("writing the report")
    src = "\n\n".join(f"[{i}] {p['title']} - {p['url']}\n{p['text']}" for i, p in enumerate(pages, 1))
    ans = llm.chat(REPORT_SYSTEM, f"Today: {time.strftime('%Y-%m-%d')}\nQuestion: {question}\n\nSources:\n{src}",
                   max_tokens=REPORT_TOKENS, kind=MODEL_KIND)
    return ans.strip() + "\n\nSources:\n" + "\n".join(f"[{i}] {p['title']} - {p['url']}" for i, p in enumerate(pages, 1)), queries


def start(jobs, llm, key, question, depth="normal"):
    """Run in the background inside the dashboard's job list (poll /api/ai/job?id=...)."""
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "status": "running", "answer": None, "started": time.time(), "stage": "starting", "kind": "research"}
    with jobs.lock:
        jobs.jobs[jid] = job

    def work():
        try:
            if not key:
                raise RuntimeError("no Ollama API key - add it to config.json \"research\": {\"api_key\": ...}")
            job["answer"], job["queries"] = research(llm, key, question, depth, lambda s: job.update(stage=s))
            job["status"] = "done"
        except Exception as ex:
            job["status"], job["error"] = "error", f"{type(ex).__name__}: {ex}"[:300]
    threading.Thread(target=work, daemon=True).start()
    return jid
