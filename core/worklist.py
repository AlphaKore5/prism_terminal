"""
Prism — the pending-work files
───────────────────────────────
The working dialog's tabs used to show only what the LAST check happened to
find. Each check's result was held in memory and nowhere else, so the moment
the inbox's own read bookmark moved past a message, whatever had been shown
for it was gone — switching tabs, or simply reopening the dialog the next
morning, was enough to lose a customer's reply that still needed an answer,
or a purchase order nobody had accepted yet.

This is the fix: one plain JSON file per section, inside a `worklist/`
folder next to the register, appended to every time a check finds something
(or Prism sends something) and read back every time a tab needs to draw
itself — so what is on screen is never just "since the last check", it is
"everything that has not been dealt with yet", however many checks ago it
arrived. The owner asked for exactly this — "a file for every section and
every phase" — and one file per section is also what makes the folder
readable by eye: open `replies.json` and it is the replies, nothing else.

    <inquiry folder>/worklist/
        arrived.json    every mail Prism sorted — a permanent log; nothing
                        here is ever "done", it exists so a sender can be
                        found again next week, not just glimpsed once
        replies.json    a customer's answer to a quotation — resolved once
                        it is applied to the register
        orders.json     a purchase order read off the mail — resolved once
                        it is accepted or otherwise dealt with
        sent.json       every mail Prism sent on the owner's behalf — a
                        quotation, a reminder, a win-back — so "Waiting on a
                        reply" can say "reminder sent 24-08, 25-08" instead
                        of just a count

An older Prism kept all of this in one `worklist.json`. migrate() folds that
file into the four above the first time anything reads the folder, and
leaves the original behind as `worklist.json.bak` — never deleted, never
rewritten, because it might be the only copy of something.

Every write is atomic, same discipline as register.py: a crash mid-write
must never truncate the only record of what still needs a reply.

The logs (`arrived`, `sent`) are still capped, for the working file to stay
small enough to rewrite on every check. What the cap evicts used to simply
be deleted — silently, and in contradiction to the promise above. It is not
deleted any more: it is appended, one JSON object per line, to
`arrived.archive.jsonl` / `sent.archive.jsonl` next to the working file,
before the working file is trimmed. Nothing a customer sent is gone; it has
just moved to the file a person, not a tab, reads.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

DIRNAME = "worklist"
LEGACY_FILENAME = "worklist.json"
# Kept under its old name too: mailflow.Paths.worklist_json still points at
# the legacy file, which is exactly the path migrate() needs to find it.
FILENAME = LEGACY_FILENAME

KINDS = ("arrived", "replies", "orders", "sent")
# Logs never resolve — there is nothing to "deal with" about a mail that was
# sorted, or one that was sent — so they carry no resolved flag and are
# trimmed only by an outright cap. "replies" and "orders" are todo lists:
# only rows long since resolved are ever eligible for trimming there. An
# unread purchase order from three weeks ago is exactly the row this folder
# exists to keep from vanishing, so it is never trimmed for being old.
LOG_KINDS = ("arrived", "sent")
ARRIVED_KEEP = 5000
SENT_KEEP = 5000
RESOLVED_KEEP = 500
ARCHIVE_SUFFIX = ".archive.jsonl"


# ── where things are ──────────────────────────────────────────────────────────

def dir_in(folder: str) -> str:
    return os.path.join(folder, DIRNAME)


def path_for(folder: str, kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown worklist kind {kind!r}")
    return os.path.join(dir_in(folder), f"{kind}.json")


def archive_path_for(folder: str, kind: str) -> str:
    """Where a cap's evictions go instead of the bin. Append-only, one JSON
    object per line — never read by Prism itself, so a very old mailbox's
    history costs nothing at runtime; it exists so a person can open it."""
    return os.path.join(dir_in(folder), f"{kind}{ARCHIVE_SUFFIX}")


def has_archive(folder: str, kind: str) -> bool:
    """True once a cap has ever evicted a row of this kind — the UI's cue
    that "everything" now means "everything, some of it in the archive
    file" rather than a silent lie."""
    path = archive_path_for(folder, kind)
    return os.path.exists(path) and os.path.getsize(path) > 0


def path_in(folder: str) -> str:
    """The pre-folder single file. Only migrate() has a reason to want it."""
    return os.path.join(folder, LEGACY_FILENAME)


def _blank() -> dict:
    return {kind: [] for kind in KINDS}


# ── reading and writing one kind ──────────────────────────────────────────────

def _read_kind(folder: str, kind: str) -> list[dict]:
    path = path_for(folder, kind)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def _write_kind(folder: str, kind: str, rows: list[dict]) -> None:
    """Atomic: written beside, fsync'd, then swapped in — the only record of
    a reply nobody has answered yet must never be half a file."""
    os.makedirs(dir_in(folder), exist_ok=True)
    path = path_for(folder, kind)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load(folder: str) -> dict:
    """Every section, or empty lists for a folder nothing has been written
    to yet — a mailbox nobody has checked is not an error."""
    migrate(folder)
    return {kind: _read_kind(folder, kind) for kind in KINDS}


def save(folder: str, data: dict, kinds=None) -> None:
    """Write the sections named (default: every one present in `data`)."""
    for kind in (kinds or [k for k in KINDS if k in data]):
        _write_kind(folder, kind, list(data.get(kind) or []))


# ── the one file from before the folder existed ───────────────────────────────

def migrate(folder: str) -> bool:
    """Fold an older single `worklist.json` into the per-section files.

    Idempotent, and it never loses a row: each section is the union — by
    _key() — of whatever the per-section file already holds and what the
    old file holds, with the per-section row winning a tie (it may carry a
    newer `resolved` flag or a correction). Only once every section has
    been written is the old file moved aside to `.bak`, so a crash half-way
    leaves both in place and the next call simply unions again. An old file
    that cannot be read is left exactly where it is.

    Returns True if something was migrated.
    """
    legacy = path_in(folder)
    if not os.path.exists(legacy):
        return False
    try:
        with open(legacy, "r", encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(old, dict):
        return False
    for kind in KINDS:
        incoming = old.get(kind)
        if not isinstance(incoming, list):
            continue
        current = _read_kind(folder, kind)
        seen = {_key(r) for r in current}
        for row in incoming:
            if isinstance(row, dict) and _key(row) not in seen:
                seen.add(_key(row))
                current.append(row)
        _write_kind(folder, kind, current)
    backup = legacy + ".bak"
    if os.path.exists(backup):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{legacy}.bak.{stamp}"
    os.replace(legacy, backup)
    return True


# ── entries ───────────────────────────────────────────────────────────────────

def _key(entry: dict) -> str:
    """A message-id when there is one — the one identifier that survives a
    re-check — falling back to sender+subject+date for anything unusual
    enough to lack one."""
    message_id = (entry.get("message_id") or "").strip()
    if message_id:
        return message_id
    return "|".join((entry.get("from_addr", ""), entry.get("subject", ""),
                     entry.get("date", "")))


entry_key = _key       # the public name: what remove() expects


def remove(folder: str, kind: str, keys) -> dict:
    """Take entries out of `kind` for good, by entry_key() — "Delete" on
    All mail. Unknown keys change nothing."""
    if kind not in KINDS:
        raise ValueError(f"unknown worklist kind: {kind!r}")
    wanted = {k for k in keys if k}
    migrate(folder)
    rows = [r for r in _read_kind(folder, kind) if _key(r) not in wanted]
    _write_kind(folder, kind, rows)
    data = load(folder)
    data[kind] = rows
    return data


def clear(folder: str, kind: str) -> None:
    """Empty one kind completely — the "start over" behind remove()."""
    if kind not in KINDS:
        raise ValueError(f"unknown worklist kind: {kind!r}")
    migrate(folder)
    _write_kind(folder, kind, [])


def append(folder: str, kind: str, entries: list[dict]) -> dict:
    """Merge new entries into `kind`, keyed by _key() so the same message
    from two checks (the bookmark did not move, or the walk re-read it)
    never doubles up. An entry already on file is left exactly as it is —
    the resolved flag, or a correction, a person made is theirs to keep,
    not the next check's to quietly undo."""
    if kind not in KINDS:
        raise ValueError(f"unknown worklist kind {kind!r}")
    migrate(folder)
    rows = _read_kind(folder, kind)
    seen = {_key(r) for r in rows}
    for entry in entries:
        k = _key(entry)
        if k in seen:
            continue
        seen.add(k)
        entry = dict(entry)
        if kind not in LOG_KINDS:
            entry.setdefault("resolved", False)
        rows.append(entry)
    rows = _trimmed(folder, kind, rows)
    _write_kind(folder, kind, rows)
    data = load(folder)
    data[kind] = rows
    return data


def _archive(folder: str, kind: str, rows: list[dict]) -> None:
    """Append rows a cap is about to evict, oldest first, so the archive
    file reads in the same order the working file did. One write, so a
    crash mid-append costs at most the row being written, never an
    earlier one — the same discipline as _write_kind(), just additive
    instead of atomic-replace, because this file is never rewritten."""
    if not rows:
        return
    os.makedirs(dir_in(folder), exist_ok=True)
    with open(archive_path_for(folder, kind), "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def _trimmed(folder: str, kind: str, rows: list[dict]) -> list[dict]:
    """Whatever a cap would evict is archived first — see _archive() — so
    the working file stays small without anything being deleted."""
    if kind == "arrived":
        if len(rows) <= ARRIVED_KEEP:
            return rows
        _archive(folder, kind, rows[:-ARRIVED_KEEP])
        return rows[-ARRIVED_KEEP:]
    if kind == "sent":
        if len(rows) <= SENT_KEEP:
            return rows
        _archive(folder, kind, rows[:-SENT_KEEP])
        return rows[-SENT_KEEP:]
    pending_rows = [r for r in rows if not r.get("resolved")]
    resolved = [r for r in rows if r.get("resolved")]
    if len(resolved) > RESOLVED_KEEP:
        _archive(folder, kind, resolved[:-RESOLVED_KEEP])
        resolved = resolved[-RESOLVED_KEEP:]
    # Stable order: interleave back by original position rather than
    # pending-then-resolved, so the screen reads oldest-to-newest as typed.
    kept_keys = {_key(r) for r in pending_rows + resolved}
    return [r for r in rows if _key(r) in kept_keys]


def update(folder: str, kind: str, message_id: str, changes: dict) -> dict:
    """Apply changes to one row, found by message id — a correction to how
    a sender is sorted, or marking a reply resolved once it is applied."""
    migrate(folder)
    rows = _read_kind(folder, kind)
    for row in rows:
        if row.get("message_id") == message_id:
            row.update(changes)
            break
    _write_kind(folder, kind, rows)
    data = load(folder)
    data[kind] = rows
    return data


def resolve(folder: str, kind: str, message_id: str) -> dict:
    return update(folder, kind, message_id, {"resolved": True})


def resolve_many(folder: str, kind: str, message_ids) -> dict:
    """resolve() for several mails at once, in one write — what "Delete"
    on a multi-row selection in "They answered" or "Order came" does. The
    mail only leaves that list; the inquiry it belongs to is not touched."""
    wanted = {m for m in message_ids if m}
    migrate(folder)
    rows = _read_kind(folder, kind)
    for row in rows:
        if row.get("message_id") in wanted:
            row["resolved"] = True
    _write_kind(folder, kind, rows)
    data = load(folder)
    data[kind] = rows
    return data


def log_sent(folder: str, kind: str, *, to: str, subject: str,
             inquiry_no: str = "", quotation_no: str = "",
             when: datetime | None = None) -> dict:
    """Record one mail Prism sent on the owner's behalf.

    `kind` is what it was — "quotation", "reminder", "winback". The id is
    generated rather than taken off the mail, so two reminders sent on the
    same day are two rows, not one de-duplicated into the other.
    """
    when = when or datetime.now()
    entry = {
        "message_id": f"<sent-{uuid.uuid4().hex}@prism>",
        "kind": kind,
        "date": when.strftime("%Y-%m-%d"),
        "time": when.strftime("%H:%M"),
        "to": to or "",
        "subject": subject or "",
        "inquiry_no": inquiry_no or "",
        "quotation_no": quotation_no or "",
    }
    append(folder, "sent", [entry])
    return entry


# ── reading back ──────────────────────────────────────────────────────────────

def pending(data: dict, kind: str) -> list[dict]:
    """Not yet dealt with, oldest first — the actionable list for "replies"
    and "orders"."""
    return [r for r in data.get(kind, []) if not r.get("resolved")]


def history(data: dict, kind: str, days: int | None = None) -> list[dict]:
    """Everything, newest first — the log view for "arrived" and "sent", and
    the "show what has already been handled too" view for the other two.
    `days` filters on the entry's own "date" (a plain YYYY-MM-DD), inclusive
    of today; None returns all of it."""
    rows = list(data.get(kind, []))
    rows.reverse()
    if days is not None:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        rows = [r for r in rows if (r.get("date") or "") >= cutoff]
    return rows


def sent_for(data: dict, inquiry_no: str) -> list[dict]:
    """Everything Prism sent about one inquiry, oldest first — what the
    "Sent so far" line and the reminder column are drawn from."""
    inquiry_no = (inquiry_no or "").strip()
    if not inquiry_no:
        return []
    return [r for r in data.get("sent", [])
            if (r.get("inquiry_no") or "").strip() == inquiry_no]
