"""
Prism — natural-language file finder
────────────────────────────────────
Turns a casually spoken/typed description of where a file lives —
  "you can get that file from the prism ai flow folder of python program
   in documents"
— into a real path on disk.

Two steps:
  1. Groq parses the description into ordered folder hints + a filename
     ("of X in Y" phrasing is normalised to outermost → innermost). Falls
     back to a local heuristic when the API is unavailable.
  2. A deterministic fuzzy walker resolves the hints against the real
     filesystem, so "python program" matches "PythonProgram" and
     "prism ai flow" matches "prism-ai-flow". No AI ever lists your disk;
     only the description the user already typed is sent to Groq.
"""
from __future__ import annotations
import os
import re
import json
import difflib
import requests

from . import ai_client as _ai

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Directories that are never worth descending into.
_SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv", "Library",
              "Applications", ".Trash", "site-packages"}

_MAX_CANDIDATE_DIRS = 8      # parallel branches kept while resolving hints
_MAX_FILE_MATCHES = 5        # file suggestions returned
_MAX_WALK_ENTRIES = 4000     # hard cap on filesystem entries touched per find

_KNOWN_ROOTS = {
    "documents": "~/Documents", "document": "~/Documents",
    "desktop": "~/Desktop", "downloads": "~/Downloads",
    "download": "~/Downloads", "home": "~", "pictures": "~/Pictures",
    "movies": "~/Movies", "music": "~/Music",
}


def _norm(s: str) -> str:
    """'Python Program' / 'python-program' / 'PythonProgram' → 'pythonprogram'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _score(hint: str, name: str) -> float:
    hn, nn = _norm(hint), _norm(name)
    if not hn or not nn:
        return 0.0
    if hn == nn:
        return 1.0
    if hn in nn or nn in hn:
        return 0.9
    return difflib.SequenceMatcher(None, hn, nn).ratio()


# ── Step 1: parse the description ─────────────────────────────────────────────

def _llm_parse(desc: str, api_key: str, model: str) -> dict | None:
    prompt = f"""A user described where a file OR folder lives on their computer, in
casual language. Extract the location. Return ONLY JSON, no commentary:
{{"folders": ["outermost", "...", "innermost"], "filename": "name-or-null"}}

Rules:
- "folders" must be ordered OUTERMOST first, regardless of phrasing or word
  order. The marker word ("folder"/"directory") can appear BEFORE or AFTER
  the name — both mean the same thing:
    "the X folder of Y in documents"        → documents → Y → X
    "the folder X inside Y inside documents" → documents → Y → X
  Every named folder/project in the chain becomes one entry, innermost last.
- Keep folder names exactly as spoken ("python program", not corrected).
- Drop filler/verb words: "folder", "directory", "file", "the", "my",
  "analyze", "get", "take", "select", "open", "go to".
- "filename" must be null UNLESS there is POSITIVE evidence this is a FILE,
  not a folder: either a visible extension (.pdf, .py, .txt, .zip, …) or the
  user explicitly says "the file/document called/named X". A bare
  project/folder name with no extension (e.g. "prism_terminal", "prism ai
  flow", "the delta project") is a FOLDER, never a guessed filename — put it
  in "folders", leave "filename" null. When in doubt, null.

Description: {desc}"""
    if _ai.active():
        text = _ai.chat("classify", prompt, addon="core", feature="pathfinder",
                        temperature=0, timeout=30, model_hint=model)
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
        rj = resp.json()
        if "choices" not in rj:
            return None
        text = rj["choices"][0]["message"]["content"]
    s, e = text.find("{"), text.rfind("}") + 1
    if s == -1 or e <= s:
        return None
    data = json.loads(text[s:e])
    folders = [f for f in (data.get("folders") or []) if f and isinstance(f, str)]
    filename = data.get("filename")
    if filename in ("null", "", None):
        filename = None
    return {"folders": folders, "filename": filename}


def _heuristic_parse(desc: str) -> dict:
    """No-API fallback: split on location prepositions; English descriptions
    usually run innermost → outermost ('X folder of Y in documents'), so the
    fragments are reversed."""
    text = desc.lower()
    ext_match = re.search(r"\b([\w-]+\.(?:pdf|docx?|txt|md|csv|png|jpe?g|py|json|pptx?|xlsx?|zip|mp[34]))\b", text)
    filename = ext_match.group(1) if ext_match else None
    if filename:
        text = text.replace(filename, " ")
    fragments = re.split(r"\bin\b|\bof\b|\bfrom\b|\bunder\b|\binside\b|,", text)
    stop = {"folder", "directory", "file", "the", "my", "a", "an", "that", "this",
            "you", "can", "get", "it", "is", "there", "named", "called", "go", "to"}
    folders = []
    for frag in fragments:
        words = [w for w in re.findall(r"[\w-]+", frag) if w not in stop]
        if words:
            folders.append(" ".join(words))
    folders.reverse()   # innermost-first speech → outermost-first list
    return {"folders": folders, "filename": filename}


def parse_description(desc: str, cfg: dict) -> dict:
    api_key = cfg.get("api_key")
    if api_key:
        try:
            parsed = _llm_parse(desc, api_key, cfg.get("model", "llama-3.3-70b-versatile"))
            if parsed and parsed["folders"]:
                return parsed
        except Exception:
            pass
    return _heuristic_parse(desc)


# ── Step 2: resolve hints against the real filesystem ─────────────────────────

def _subdirs(parent: str) -> list[str]:
    try:
        return [e for e in os.listdir(parent)
                if not e.startswith(".") and e not in _SKIP_DIRS
                and os.path.isdir(os.path.join(parent, e))]
    except OSError:
        return []


def _match_hint(candidates: list[str], hint: str) -> list[str]:
    """Best-scoring subdirectories (depth ≤ 2) of the candidate dirs for one hint."""
    scored = []
    for parent in candidates:
        for d1 in _subdirs(parent):
            p1 = os.path.join(parent, d1)
            sc = _score(hint, d1)
            if sc >= 0.65:
                scored.append((sc, p1))
            else:
                for d2 in _subdirs(p1):
                    sc2 = _score(hint, d2)
                    if sc2 >= 0.65:
                        scored.append((sc2 * 0.95, os.path.join(p1, d2)))
    scored.sort(key=lambda t: -t[0])
    return [p for _, p in scored[:_MAX_CANDIDATE_DIRS]]


def _resolve_folders(folders: list[str]) -> list[str]:
    home = os.path.expanduser("~")
    candidates = [home]
    hints = list(folders)
    # A leading well-known root ("documents") anchors the search directly.
    if hints and _norm(hints[0]) in {_norm(k) for k in _KNOWN_ROOTS}:
        root = _KNOWN_ROOTS.get(hints[0].lower().strip())
        if root:
            candidates = [os.path.expanduser(root)]
            hints = hints[1:]
    for hint in hints:
        matched = _match_hint(candidates, hint)
        if matched:
            candidates = matched
        # unmatched hint → noise word survived parsing; keep going with what we have
    return candidates


def _find_files(dirs: list[str], filename: str | None) -> list[str]:
    """Fuzzy-match the filename inside candidate dirs (depth ≤ 3)."""
    scored, seen = [], 0
    stem = os.path.splitext(filename)[0] if filename else None
    for base in dirs:
        for root, subdirs, files in os.walk(base):
            subdirs[:] = [d for d in subdirs
                          if not d.startswith(".") and d not in _SKIP_DIRS]
            if root[len(base):].count(os.sep) >= 3:
                subdirs[:] = []
            for f in files:
                if f.startswith("."):
                    continue
                seen += 1
                if seen > _MAX_WALK_ENTRIES:
                    break
                if stem:
                    sc = max(_score(filename, f), _score(stem, os.path.splitext(f)[0]))
                    if sc >= 0.6:
                        scored.append((sc, os.path.join(root, f)))
            if seen > _MAX_WALK_ENTRIES:
                break
        if seen > _MAX_WALK_ENTRIES:
            break
    scored.sort(key=lambda t: -t[0])
    return scored[:_MAX_FILE_MATCHES]


def find(desc: str, cfg: dict) -> dict:
    """Resolve a natural-language location description.
    Returns {"folders": hints, "filename": hint, "dir": best_dir_or_None,
             "files": [matched paths], "score": best_match_confidence_or_None}."""
    expanded = os.path.abspath(os.path.expanduser(desc.strip()))
    if os.path.exists(expanded):
        if os.path.isdir(expanded):
            return {"folders": [desc], "filename": None, "dir": expanded, "files": [], "score": 1.0}
        return {"folders": [os.path.dirname(expanded)], "filename": os.path.basename(expanded),
                "dir": os.path.dirname(expanded), "files": [expanded], "score": 1.0}

    parsed = parse_description(desc, cfg)
    filename = parsed.get("filename")
    # Defensive net: a "filename" with no extension that just repeats one of
    # the folder hints is almost certainly a mis-parsed folder name, not a
    # real file (this exact failure mode has silently attached the wrong
    # file more than once) — null it out regardless of what the parser said.
    if filename and "." not in filename:
        fn_norm = _norm(filename)
        if any(fn_norm and (fn_norm == _norm(f) or fn_norm in _norm(f) or _norm(f) in fn_norm)
               for f in parsed.get("folders", [])):
            filename = None
    dirs = _resolve_folders(parsed["folders"])
    best_dir = dirs[0] if dirs else None
    scored = _find_files(dirs, filename) if filename else []
    return {"folders": parsed["folders"], "filename": filename,
            "dir": best_dir, "files": [p for _, p in scored],
            "score": scored[0][0] if scored else None}


def list_dir_files(path: str, limit: int = 15) -> list[str]:
    """Plain files directly inside a dir — shown when no filename was spoken."""
    try:
        return sorted(
            os.path.join(path, f) for f in os.listdir(path)
            if not f.startswith(".") and os.path.isfile(os.path.join(path, f))
        )[:limit]
    except OSError:
        return []
