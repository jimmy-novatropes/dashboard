"""Add structured logging (oflog) to every Lambda under serverless_lambdas/.  Idempotent.

  python apply_oflog.py <repo_root> [--dry-run]
"""
import difflib
import os
import re
import shutil
import sys

ROOT = sys.argv[1]
DRY = "--dry-run" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDAS = os.path.join(ROOT, "serverless_lambdas")
changed = []


def write(path, old, new):
    if old == new:
        return
    if "\r\n" in old:                       # keep Windows line endings as they were
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    changed.append(path)
    if DRY:
        sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                   os.path.relpath(path, ROOT), os.path.relpath(path, ROOT), n=1))
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new)


def add_import(text):
    if re.search(r"^import oflog\b", text, re.M):
        return text
    m = re.search(r"^(?:import|from)\s+\S+.*$", text, re.M)
    if not m:
        return "import oflog\n" + text
    # put it after the first import statement (handles multi-line "from x import (...)")
    end = m.end()
    if text[m.start():end].rstrip().endswith("("):
        end = text.index(")", end) + 1
    return text[:end] + "\nimport oflog  # structured log lines for the error dashboard" + text[end:]


# ---------------------------------------------------------------- http_retry.py
HTTP_RETRY_EDITS = [
    ('import requests\n',
     'import requests\n\ntry:\n    import oflog  # structured log lines for the error dashboard\nexcept ImportError:  # pragma: no cover\n    oflog = None\n'),
    ('            print(f"⏳ {type(exc).__name__} on {method} {path} — retry {attempt + 1}/{MAX_RETRIES} in {wait:.1f}s")',
     '            _retry_log(f"⏳ {type(exc).__name__} on {method} {path} — retry {attempt + 1}/{MAX_RETRIES} in {wait:.1f}s",\n'
     '                       url, method, path, attempt, wait, error_type=type(exc).__name__)'),
    ('        print(f"⏳ {resp.status_code} on {method} {path} — retry {attempt + 1}/{MAX_RETRIES} in {wait:.1f}s")',
     '        _retry_log(f"⏳ {resp.status_code} on {method} {path} — retry {attempt + 1}/{MAX_RETRIES} in {wait:.1f}s",\n'
     '                   url, method, path, attempt, wait, status=resp.status_code)'),
    ('def get(url, **kwargs):',
     'def _retry_log(text, url, method, path, attempt, wait, **fields):\n'
     '    if not oflog:\n        print(text)\n        return\n'
     '    oflog.count("retries")\n'
     '    oflog.log("WARNING", "http_retry", text, partner=oflog.partner_of(url), method=method, path=path,\n'
     '              attempt=attempt + 1, wait_s=round(wait, 1), **fields)\n\n\n'
     'def get(url, **kwargs):'),
    ('''    if not resp.ok:
        print(f"❌ HTTP error {context}: {describe(resp)}\\n   body: {_snippet(resp)!r}")
    resp.raise_for_status()''',
     '''    if not resp.ok:
        if oflog:
            oflog.check_response(resp, f"HTTP error {context}".strip())
        else:
            print(f"❌ HTTP error {context}: {describe(resp)}\\n   body: {_snippet(resp)!r}")
    resp.raise_for_status()'''),
    ('''    except ValueError:
        print(f"❌ Non-JSON response {context}: {describe(resp)}\\n   body: {_snippet(resp)!r}")
        raise''',
     '''    except ValueError:
        if oflog:
            oflog.log("ERROR", "non_json_response", f"❌ Non-JSON response {context}: {describe(resp)}",
                      partner=oflog.partner_of(resp.url), status=resp.status_code,
                      content_type=resp.headers.get("Content-Type"), body=_snippet(resp))
        else:
            print(f"❌ Non-JSON response {context}: {describe(resp)}\\n   body: {_snippet(resp)!r}")
        raise'''),
    ('''    message and context (not cut at 200 chars) plus the first input sent."""
''',
     '''    message and context (not cut at 200 chars) plus the first input sent."""
    if oflog:
        if not oflog.check_response(resp, label, inputs=inputs):
            oflog.log("ERROR", f"{oflog.partner_of(resp.url)}_batch_failed", f"❌ {label}: {describe(resp)}",
                      partner=oflog.partner_of(resp.url), status=resp.status_code,
                      batch_size=len(inputs) if inputs else None)
        return
'''),
]

# "if not resp.ok:  print(f"❌ Error {resp.status_code}: {resp.text}")"  ->  one structured line
# (also catches HubSpot batch calls that succeed with per-record errors, which were silent before)
ERR_BLOCK = re.compile(r'^([ \t]*)if not (\w+)\.ok:[ \t]*\r?\n\1[ \t]+print\(f"❌ Error \{\2\.status_code\}: \{\2\.text\}"\)[ \t]*(?=\r?$)',
                       re.M)


def enclosing_function(text, pos):
    defs = [m for m in re.finditer(r"^[ \t]*def (\w+)\(", text[:pos], re.M)]
    return defs[-1].group(1) if defs else "request"


def patch_errors(text):
    out, last, n = [], 0, 0
    for m in ERR_BLOCK.finditer(text):
        rest = text[m.end():].lstrip("\r\n").split("\n", 1)[0]
        nxt_indent = len(rest) - len(rest.lstrip())
        if rest.strip() and nxt_indent > len(m.group(1)):
            continue                  # the if-block does more than print: leave it alone
        out.append(text[last:m.start()])
        out.append(f'{m.group(1)}oflog.check_response({m.group(2)}, "{enclosing_function(text, m.start())}")')
        last, n = m.end(), n + 1
    out.append(text[last:])
    return "".join(out), n


def patch_handler(text):
    if "@oflog.run" in text:
        return text
    m = re.search(r"^def lambda_handler\(", text, re.M)
    if not m:
        return text
    before = text[:m.start()]
    lines = before.rstrip("\n").split("\n")
    # walk up over decorators directly above the def
    i = len(lines)
    while i > 0 and lines[i - 1].startswith("@"):
        i -= 1
    decos = lines[i:]
    deco = "@oflog.run" if any(d.startswith("@log_traceback") for d in decos) else "@oflog.run(print_traceback=True)"
    new_before = "\n".join(lines[:i] + [deco] + decos) + "\n"
    return add_import(new_before + text[m.start():])


report = []
for name in sorted(os.listdir(LAMBDAS)):
    d = os.path.join(LAMBDAS, name)
    if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "handler.py")):
        continue
    dst = os.path.join(d, "oflog.py")
    src = os.path.join(HERE, "oflog.py")
    if not os.path.exists(dst) or open(dst, encoding="utf-8").read() != open(src, encoding="utf-8").read():
        changed.append(dst)
        if not DRY:
            shutil.copyfile(src, dst)
    notes = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".py") or fn == "oflog.py":
            continue
        p = os.path.join(d, fn)
        old = open(p, encoding="utf-8", newline="").read()
        new = old
        if fn == "http_retry.py":
            if "oflog" not in new:
                for a, b in HTTP_RETRY_EDITS:
                    if a not in new:
                        notes.append(f"http_retry: pattern not found: {a[:40]!r}")
                        continue
                    new = new.replace(a, b, 1)
            notes.append("http_retry structured")
        else:
            new, n = patch_errors(new)
            if n:
                new = add_import(new)
                notes.append(f"{fn}: {n} error print(s)")
        if fn == "handler.py":
            new2 = patch_handler(new)
            if new2 != new:
                notes.append("handler: @" + ("oflog.run" if "@oflog.run\n@log_traceback" in new2 else "oflog.run(print_traceback=True)"))
            new = new2
        write(p, old, new)
    report.append(f"{name}: " + ("; ".join(notes) or "nothing to change"))

print("\n".join(report))
print(f"{len(changed)} file(s) {'would change' if DRY else 'changed'}")
