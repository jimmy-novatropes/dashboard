"""
read_shunt.py - Claude Code PreToolUse hook: large file reads go to Ollama first.

When Claude tries to Read a whole text file over MIN_LINES lines, this hook blocks
that read and hands Claude instead:
  - an outline (functions/classes with line numbers), and
  - a short summary written by your local Ollama model.
Claude then reads only the line range it needs (Read with offset/limit).

Safety valves (the hook never blocks work):
  - a second full Read of the same file in the same session is allowed
  - reads that already use offset/limit are allowed
  - if Ollama is down, slow or errors, the read is allowed
Summaries are cached by file path + modification time.

Installed by setup_token_savers.py (ADD_READ_SHUNT = True).
"""
from pathlib import Path

# ============================ SETTINGS ============================
MIN_LINES = 350                       # files at or below this are read normally
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = ""                     # "" = auto-pick an installed model (prefers PREFERRED_MODELS)
PREFERRED_MODELS = ["qwen2.5-coder", "qwen3", "deepseek-coder", "codellama", "llama3"]
OLLAMA_TIMEOUT_S = 90                 # keep below the hook timeout in settings.json (120)
NUM_CTX = 16384                       # Ollama context window for the summary call
MAX_CHARS_TO_MODEL = 48000            # longer files are cut before sending (outline still covers all)
SUMMARY_WORDS = 250
OUTLINE_MAX_ITEMS = 120
SKIP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ipynb", ".svg", ".ico"}
SKIP_PATH_PARTS = ["\\.claude\\", "/.claude/"]    # never shunt Claude's own config/skills
CACHE_DIR = Path.home() / ".cache" / "read_shunt"
LOG_FILE = CACHE_DIR / "read_shunt.log"
ENABLED = True
SHUNT_SHELL_DUMPS = True              # also catch whole-file dumps via the shell (cat, type, Get-Content ...)
SHELL_DUMP_RE = (r'^\s*(?:rtk\s+)?(?:cat|type|more|bat|gc|Get-Content(?:\s+-Raw)?)\s+'
                 r'(?:"([^"]+)"|\'([^\']+)\'|([^\s|;&<>"\']+))\s*$')
# ==================================================================

import ast, hashlib, json, re, sys, time, urllib.request

OUTLINE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|interface|type|enum)\s+[\w$]+"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+[\w$]+\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>"
    r"|^\s*(?:CREATE|ALTER)\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE)\s+\S+"
    r"|^#{1,3}\s+\S", re.IGNORECASE)


def log(msg):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def allow():
    sys.exit(0)          # no output = let the Read run normally


def outline(path: Path, text: str):
    items = []
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else "def"
                    end = getattr(node, "end_lineno", node.lineno)
                    items.append((node.lineno, f"L{node.lineno}-{end}  {kind} {node.name}"))
        except SyntaxError:
            pass
    if not items:
        for i, line in enumerate(text.splitlines(), 1):
            if OUTLINE_RE.match(line):
                items.append((i, f"L{i}  {line.strip()[:100]}"))
    items.sort()
    lines = [s for _, s in items]
    if len(lines) > OUTLINE_MAX_ITEMS:
        lines = lines[:OUTLINE_MAX_ITEMS] + [f"... {len(items) - OUTLINE_MAX_ITEMS} more"]
    return "\n".join(lines) or "(no outline found)"


def pick_model():
    if OLLAMA_MODEL:
        return OLLAMA_MODEL
    with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
        names = [m["name"] for m in json.load(r).get("models", [])]
    names = [n for n in names if "embed" not in n]
    for pref in PREFERRED_MODELS:
        for n in names:
            if n.startswith(pref):
                return n
    return names[0] if names else None


def summarize(path: Path, text: str):
    model = pick_model()
    if not model:
        raise RuntimeError("no Ollama model installed")
    body = text[:MAX_CHARS_TO_MODEL]
    cut = "" if len(text) <= MAX_CHARS_TO_MODEL else "\n[file truncated here]"
    prompt = (f"Summarize this file for a developer who will then open specific line ranges.\n"
              f"File: {path.name}\nIn at most {SUMMARY_WORDS} words: purpose, main functions/classes "
              f"and what they do, key config/constants, external services or APIs used, anything unusual. "
              f"Plain text, no code blocks.\n\n{body}{cut}")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                         "options": {"num_ctx": NUM_CTX, "temperature": 0.1}}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as r:
        return model, json.load(r).get("response", "").strip()


def main():
    if not ENABLED:
        allow()
    try:
        data = json.load(sys.stdin)
    except Exception:
        allow()
    tool = data.get("tool_name")
    ti = data.get("tool_input") or {}
    if tool == "Read":
        if ti.get("offset") or ti.get("limit") or ti.get("pages"):
            allow()
        fp = ti.get("file_path") or ""
    elif tool == "Bash" and SHUNT_SHELL_DUMPS:
        m = re.match(SHELL_DUMP_RE, ti.get("command") or "", re.IGNORECASE)
        if not m:
            allow()
        fp = next(g for g in m.groups() if g)
        gb = re.match(r"^/([a-zA-Z])/(.*)", fp)          # Git Bash style /c/Users/... -> C:/Users/...
        if gb and sys.platform == "win32":
            fp = f"{gb.group(1).upper()}:/{gb.group(2)}"
        if not Path(fp).is_absolute():
            fp = str(Path(data.get("cwd") or ".") / fp)
    else:
        allow()
    path = Path(fp)
    if not fp or path.suffix.lower() in SKIP_EXTENSIONS or any(p in fp for p in SKIP_PATH_PARTS):
        allow()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        allow()
    n_lines = text.count("\n") + 1
    if n_lines <= MIN_LINES:
        allow()

    # second full read of the same file in this session -> let it through
    session = data.get("session_id", "nosession")
    seen_file = CACHE_DIR / f"seen-{hashlib.md5(session.encode()).hexdigest()[:12]}.txt"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    seen = seen_file.read_text(encoding="utf-8").splitlines() if seen_file.exists() else []
    if fp in seen:
        log(f"ALLOW repeat {fp}")
        allow()

    key = hashlib.md5(f"{fp}|{path.stat().st_mtime}".encode()).hexdigest()
    cache = CACHE_DIR / f"{key}.json"
    try:
        if cache.exists():
            cached = json.loads(cache.read_text(encoding="utf-8"))
            model, summary = cached["model"], cached["summary"]
        else:
            t0 = time.time()
            model, summary = summarize(path, text)
            if not summary:
                raise RuntimeError("empty summary")
            cache.write_text(json.dumps({"model": model, "summary": summary}), encoding="utf-8")
            log(f"SUMMARIZED {fp} ({n_lines} lines, {time.time() - t0:.1f}s, {model})")
    except Exception as e:
        log(f"ALLOW (ollama failed: {e}) {fp}")
        allow()

    with open(seen_file, "a", encoding="utf-8") as f:
        f.write(fp + "\n")

    reason = (f"[read_shunt] {path.name} has {n_lines} lines, so the full read was replaced by an outline "
              f"and a local-model summary.\n"
              f"Next: Read with offset/limit for just the lines you need (needed before any Edit). "
              f"Don't dump the whole file via the shell either; a second full Read is allowed if you truly need all of it.\n\n"
              f"OUTLINE\n{outline(path, text)}\n\nSUMMARY ({model}, may be imprecise)\n{summary}")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


if __name__ == "__main__":
    main()
