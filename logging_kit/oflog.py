"""
Structured log lines for the AWS Error Feed dashboard.

Each call prints ONE line of JSON to CloudWatch, e.g.
  {"level": "ERROR", "event": "hubspot_batch_failed", "partner": "hubspot", "status": 400, ...}

The dashboard groups errors by "event" (stable name, not the changing message), finds records
by "record", and counts each run from the "run_summary" line, so runs that did nothing or
partly failed stand out even when nothing crashed.

Usage
-----
    import oflog

    @oflog.run                      # on lambda_handler: run_start / run_summary / lambda_failed
    def lambda_handler(event, context): ...

    oflog.log("ERROR", "netsuite_upsert_failed", record="deal 991", partner="netsuite",
              status=429, error=str(e))
    oflog.count("skipped")          # adds to this run's counters in run_summary
    oflog.check_response(resp, "HubSpot batch upsert", inputs=batch)   # logs failures + 207 partials

Rules: keep "event" names stable (snake_case, no ids in them); put ids in "record";
never log secrets, tokens or full customer payloads.
"""
import functools
import json
import os
import time
import traceback
from urllib.parse import urlparse

_run = {"counts": {}, "function": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local"), "request_id": None}
_MAX = 20000


def log(level, event, msg="", **fields):
    """Print one structured line. level: ERROR / WARNING / INFO."""
    level = str(level).upper()
    line = {"level": level, "event": event}
    if msg:
        line["msg"] = msg
    line.update({k: v for k, v in fields.items() if v is not None})
    if _run["request_id"]:
        line.setdefault("request_id", _run["request_id"])
    if level == "ERROR":
        count("errors")
    elif level == "WARNING":
        count("warnings")
    text = json.dumps(line, default=str, ensure_ascii=False)
    if len(text) > _MAX:                      # keep it one valid JSON line even when huge
        line = {k: (v[:2000] + "…" if isinstance(v, str) and len(v) > 2000 else v) for k, v in line.items()}
        text = json.dumps(line, default=str, ensure_ascii=False)[:_MAX]
    print(text, flush=True)


def count(name, n=1):
    """Add n to a counter that is reported in this run's run_summary line."""
    c = _run["counts"]
    c[name] = c.get(name, 0) + (n or 0)


def partner_of(url):
    """Short, stable name for the API a URL belongs to."""
    host = urlparse(url or "").netloc.lower()
    if "hubapi.com" in host or "hubspot" in host:
        return "hubspot"
    if "compliancelogin.com" in host:
        return "compliance"
    if "netsuite" in host:
        return "netsuite"
    return host.split(":")[0] or "unknown"


def check_response(resp, label="request", inputs=None, partner=None):
    """Log one structured line for a failed or partly failed HTTP response - including HubSpot
    batch calls that return 200/207 but list per-record "errors". Returns True if it logged."""
    status = getattr(resp, "status_code", None)
    try:
        data = resp.json()
    except Exception:
        data = None
    errors = (data.get("errors") or []) if isinstance(data, dict) else []
    num = data.get("numErrors") if isinstance(data, dict) else None
    if getattr(resp, "ok", False) and not errors and not num:
        return False
    url = getattr(resp, "url", "") or ""
    req = getattr(resp, "request", None)
    method = getattr(req, "method", None)
    path = urlparse(url).path
    partner = partner or partner_of(url)
    records = []
    for e in errors:
        ctx = e.get("context") if isinstance(e, dict) else None
        if isinstance(ctx, dict):
            for v in ctx.values():
                if isinstance(v, list):
                    records += [str(x) for x in v[:20]]
    n_failed = num or len(errors) or (len(inputs) if inputs and not getattr(resp, "ok", False) else 0)
    count("records_failed", n_failed)
    first = [{"category": e.get("category"), "message": str(e.get("message"))[:300],
              "context": json.dumps(e.get("context"), default=str)[:300]}
             for e in errors[:5] if isinstance(e, dict)]
    body = None
    if not errors:
        try:
            body = (resp.text or "")[:1000]
        except Exception:
            body = "<unreadable body>"
    log("ERROR", f"{partner}_batch_failed" if "/batch" in path else f"{partner}_request_failed",
        msg=f"❌ {label}: {status} on {method} {path}" + (f" ({len(errors)} record error(s))" if errors else ""),
        partner=partner, status=status, method=method, path=path,
        ok_results=len(data.get("results") or []) if isinstance(data, dict) else None,
        errors=len(errors) or None, failed=n_failed or None,
        categories=sorted({str(e.get("category")) for e in errors if isinstance(e, dict)}) or None,
        first_errors=first or None, records=records[:20] or None,
        batch_size=len(inputs) if inputs else None, body=body)
    return True


_COUNTISH = __import__("re").compile(r"(count|_ids|records|processed|created|updated|failed|skipped|total|errors|sent)$", 2)


def _numbers(obj, out, depth=0):
    """Collect numeric results (summed by key) from a handler's return value."""
    if depth > 4 or len(out) > 25:
        return
    if isinstance(obj, str) and depth > 0 and obj[:1] in "{[":
        try:
            obj = json.loads(obj)
        except ValueError:
            return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("statusCode",):
                continue
            if isinstance(v, bool):
                out.setdefault(k, v)
            elif isinstance(v, (int, float)):
                if _COUNTISH.search(k) and isinstance(out.get(k, 0), (int, float)):
                    out[k] = out.get(k, 0) + v       # counts add up across batches / record types
                else:
                    out.setdefault(k, v)             # pages, ids, flags: keep the first value
            elif isinstance(v, (dict, list, str)):
                _numbers(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:500]:
            if isinstance(v, (dict, list)):
                _numbers(v, out, depth + 1)


def _where(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    f = frames[-1] if frames else None
    return f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}()" if f else "unknown"


def run(fn=None, *, print_traceback=False):
    """Decorator for lambda_handler. Logs run_start, then run_summary (with the numbers the
    handler returned and this run's counters), or lambda_failed + run_summary and re-raises.
    Use print_traceback=True when there is no other decorator printing the traceback."""
    def deco(func):
        @functools.wraps(func)
        def wrapper(event=None, context=None, *a, **kw):
            _run["counts"] = {}
            _run["request_id"] = getattr(context, "aws_request_id", None)
            t0 = time.time()
            keys = list(event.keys())[:15] if isinstance(event, dict) else type(event).__name__
            log("INFO", "run_start", function=_run["function"], input_keys=keys,
                input_bytes=len(json.dumps(event, default=str)) if event is not None else 0)
            try:
                result = func(event, context, *a, **kw)
            except Exception as exc:
                if print_traceback:
                    print(traceback.format_exc(), flush=True)
                log("ERROR", "lambda_failed", function=_run["function"], error_type=type(exc).__name__,
                    error=str(exc)[:1000], where=_where(exc))
                log("INFO", "run_summary", function=_run["function"], status="failed",
                    duration_ms=round((time.time() - t0) * 1000), **_run["counts"])
                raise
            nums = {}
            _numbers(result, nums)
            log("INFO", "run_summary", function=_run["function"], status="ok",
                duration_ms=round((time.time() - t0) * 1000), results=nums or None, **_run["counts"])
            return result
        return wrapper
    return deco(fn) if fn else deco
