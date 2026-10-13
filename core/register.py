"""
Prism — the inquiry register
────────────────────────────
One CSV that keeps growing, one row per inquiry, opening in Excel like any
other file. If Prism were uninstalled tomorrow the register would still be
theirs and still work — which is the point. This is their book, not our
database.

Three things this module is careful about, all learned from how the file is
actually used:

  · **It is written atomically.** A crash mid-write must never truncate the
    only copy of somebody's order book. Same reasoning as config.save().

  · **It is often open in Excel.** On Windows that makes the file unwritable,
    and the honest response is to say "close the inquiry register in Excel",
    not to fail silently or lose the row.

  · **The owner edits it by hand.** They will re-sort it, add a column, and
    type in the Notes field. Reading is therefore forgiving: unknown columns
    are preserved untouched, and a row nobody recognises is left alone.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

# ── the statuses an inquiry moves through ────────────────────────────────────
NEW = "New"
QUOTED = "Quoted"
FOLLOWING_UP = "Following up"
NEGOTIATING = "Negotiating"
ACCEPTED = "Accepted"
CONVERTED = "Converted"
NOT_CONVERTED = "Not converted"

OPEN_STATUSES = (NEW, QUOTED, FOLLOWING_UP, NEGOTIATING, ACCEPTED)
CLOSED_STATUSES = (CONVERTED, NOT_CONVERTED)
STATUSES = OPEN_STATUSES + CLOSED_STATUSES

# Column order is the order they read it in. Identity first, then what was
# asked, then what we did, then how it ended.
COLUMNS = [
    # Time as well as date. Two inquiries from one customer on the same
    # morning are indistinguishable in a date-only register, and "which of
    # these came first" is exactly the question asked when somebody revises
    # their requirement an hour after sending it.
    "Inquiry no", "Date received", "Time received", "Customer",
    "Contact person", "Email",
    "Phone", "Product asked", "Quantity", "Drawing", "Status",
    "Quotation no", "Quotation date", "Quotation value", "Reminders sent",
    "Last contact", "Result", "Reason if lost", "PO number", "PO date",
    "Order value", "Folder", "Notes",
    # Not for reading — how a reply finds its way back to the right row.
    "Thread",
]

# The pipeline's own columns (2026-10-13). Appended, never inserted: a
# register written before them still opens exactly as it was, and a row
# saved back only gains these, empty. _write() puts them after any column
# the owner added by hand, so nothing they set up moves either.
#
#   Revision       0/blank for the first quotation, 1 for "Rev 1" ... The
#                  quotation number never changes; see revise_quotation().
#   Price history  every EARLIER total of this quotation, oldest first, as
#                  plain two-decimal numbers joined by "; " — "125000.00;
#                  118000.00". The current total stays in Quotation value.
#   Their ask      the price they countered with, plain digits, or blank.
#   Turn           "you" or "them" — who owes the next message. Blank reads
#                  as "you" on a new inquiry.
#   No quotation   "yes" when a PO came in with no quotation behind it;
#                  "maybe" when a mail only looked like one (flag_maybe_po).
#   Proforma       "to send", "sent", then "paid" — only used when the
#                  client switched the proforma step on.
REVISION = "Revision"
PRICE_HISTORY = "Price history"
THEIR_ASK = "Their ask"
TURN = "Turn"
NO_QUOTATION = "No quotation"
PROFORMA = "Proforma"
PIPELINE_COLUMNS = [REVISION, PRICE_HISTORY, THEIR_ASK, TURN, NO_QUOTATION,
                    PROFORMA]
_BASE_COLUMNS = list(COLUMNS)
COLUMNS = _BASE_COLUMNS + PIPELINE_COLUMNS

TURN_YOU, TURN_THEM = "you", "them"
YES = "yes"
MAYBE = "maybe"       # No quotation: the mail check thinks it may be a PO
PROFORMA_SENT, PROFORMA_PAID = "sent", "paid"
PROFORMA_DUE = "to send"     # PO accepted, proforma not sent yet (review R11)

FILENAME = "inquiries.csv"


class RegisterLocked(Exception):
    """The file could not be written because something else holds it open."""


def _friendly_lock(path: str) -> RegisterLocked:
    return RegisterLocked(
        f"The inquiry register is open in another program, so Prism can't add "
        f"to it. Close {os.path.basename(path)} in Excel and try again — "
        f"nothing has been lost.")


# ── the financial year, the way India counts it ──────────────────────────────

def fy_label(when: date | datetime | None = None) -> str:
    """"25-26" for any date from 1 April 2025 to 31 March 2026.

    Every quotation book and PO in the country is numbered this way, so a
    register that numbered by calendar year would be the one odd document in
    the office.
    """
    when = when or date.today()
    if isinstance(when, datetime):
        when = when.date()
    start = when.year if when.month >= 4 else when.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


# A trailing revision ("QTN/26-27/0101 Rev 1", "...-R1") is the same number:
# a revised quotation keeps its number, so it must never read as a new one.
_NUMBER = re.compile(r"^([A-Z]+)/(\d{2}-\d{2})/(\d+)"
                     r"(?:\s*[-/ ]\s*R(?:ev)?\.?\s*\d+)?$", re.I)

# The same number as it appears on disk and in a mail: the folder spelling
# INQ-26-27-0005, a file QTN-26-27-0001.csv, a subject "Quotation
# QTN/26-27/0001 — springs".
_NUMBER_ANYWHERE = re.compile(r"(?<![A-Za-z0-9])([A-Z]+)[/-](\d{2}-\d{2})[/-](\d+)(?!\d)",
                              re.I)


def next_number(rows: list[dict], prefix: str = "INQ",
                when: date | datetime | None = None, taken=()) -> str:
    """The next number in this financial year, e.g. INQ/25-26/0088.

    Counts from what is in the file rather than from a stored counter, so
    deleting the last row genuinely undoes it. `taken` is everything else
    that already carries a number — the inquiry folders on disk, the
    quotation files, the subjects of quotations already sent. A row can drop
    out of the CSV (a stale copy saved over it, a hand edit in Excel), and
    without this floor its number was handed out again, into a folder that
    still held the first job's drawings and history.
    """
    year = fy_label(when)
    highest = 0
    for row in rows:
        match = _NUMBER.match((row.get("Inquiry no") or "").strip())
        if match and match.group(1).upper() == prefix.upper() and match.group(2) == year:
            highest = max(highest, int(match.group(3)))
    for text in taken or ():
        for found in _NUMBER_ANYWHERE.finditer(str(text or "")):
            if found.group(1).upper() == prefix.upper() and found.group(2) == year:
                highest = max(highest, int(found.group(3)))
    return f"{prefix.upper()}/{year}/{highest + 1:04d}"


# ── reading and writing ───────────────────────────────────────────────────────

def path_in(folder: str, filename: str = FILENAME) -> str:
    return os.path.join(folder, filename)


def load(path: str) -> list[dict]:
    """Every row, as dicts. A missing file is an empty register, not an error.

    Columns the owner added by hand survive, because the row is kept as read
    and only the fields Prism knows about are ever replaced.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            names = _canonical_headers(reader.fieldnames or [])
            return [{names.get(k, k): v for k, v in r.items()}
                    for r in reader
                    if any((v or "").strip() for v in r.values()
                           if isinstance(v, str))]
    except OSError as e:
        raise RegisterLocked(
            f"Couldn't read the inquiry register: {e}") from e


def _squash(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


# Headers a hand-kept sheet uses for the columns Prism knows, squashed to
# letters and digits. "Inquiry No." is the one that mattered: read as an
# unknown column, every imported row had a blank number, so they all shared
# one "folder" (the inquiries folder itself) and one row on screen.
_HEADER_ALIASES = {
    "inquiryno": "Inquiry no", "inquirynumber": "Inquiry no",
    "enquiryno": "Inquiry no", "enquirynumber": "Inquiry no",
    "enqno": "Inquiry no", "inqno": "Inquiry no",
    "emailid": "Email", "emailaddress": "Email", "mail": "Email",
    "phoneno": "Phone", "mobile": "Phone", "mobileno": "Phone",
    "contactno": "Phone",
    "qty": "Quantity", "product": "Product asked",
    "quotationnumber": "Quotation no", "quoteno": "Quotation no",
    "qtnno": "Quotation no", "ponumber": "PO number", "pono": "PO number",
}


def _canonical_headers(fieldnames: list) -> dict:
    """{header as written: Prism's own name} for headers that only differ in
    case, punctuation or a common synonym. A header already spelled Prism's
    way is never displaced by a look-alike in the same file."""
    present = {n for n in fieldnames if n in COLUMNS}
    exact = {_squash(c): c for c in COLUMNS}
    out = {}
    for name in fieldnames:
        if name is None or name in COLUMNS:
            continue
        target = exact.get(_squash(name)) or _HEADER_ALIASES.get(_squash(name))
        if target and target not in present:
            out[name] = target
            present.add(target)
    return out


LOCK_WAIT = 5.0        # seconds a writer waits for another one to finish
LOCK_STALE = 30.0      # a lock older than this was left by a crashed writer


class _Locked:
    """`inquiries.csv.lock`, held around every read-modify-write.

    The mail check and the inquiry screen both rewrite the register, and
    each of them reads it first. Without a lock the one that wrote last
    erased whatever the other had written between its read and its write.
    The lock is a plain file made with O_EXCL — the one create that is
    atomic on Windows, macOS and network shares alike. A lock left behind
    by a crash is taken over once it is LOCK_STALE old. When the wait runs
    out the write is refused with a plain message rather than made anyway.
    """

    def __init__(self, path: str):
        self.lock = f"{path}.lock"
        self.held = False

    def __enter__(self):
        import time
        os.makedirs(os.path.dirname(os.path.abspath(self.lock)), exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            except PermissionError:
                # Windows: a lock file being deleted by its owner right now
                # reads as "access denied" for a moment. Same as busy.
                pass
            except OSError as e:
                raise RegisterLocked(f"Couldn't write the inquiry register: {e}") from e
            else:
                try:
                    os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
                finally:
                    os.close(fd)
                self.held = True
                return self
            try:
                if time.time() - os.path.getmtime(self.lock) > LOCK_STALE:
                    _retry(os.unlink, self.lock)
                    continue
            except OSError:
                pass       # gone already, or still held: try again
            if time.monotonic() >= deadline:
                raise RegisterLocked(
                    "Prism is still saving the inquiry register from another "
                    "window or computer. Try again in a moment — nothing has "
                    "been lost.")
            time.sleep(0.05)

    def __exit__(self, *exc):
        if self.held:
            try:
                _retry(os.unlink, self.lock)
            except OSError:
                pass       # LOCK_STALE recovers it
            self.held = False
        return False


def _retry(action, *args, attempts: int = 10):
    """Run a file operation, retrying a PermissionError for about half a
    second. On Windows os.replace() and os.unlink() fail while a virus
    scanner, the search indexer or a sync client has the file open for a
    moment; a real lock (Excel) outlasts the retries and is reported."""
    import time
    for attempt in range(attempts):
        try:
            return action(*args)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def save(rows: list[dict], path: str) -> None:
    """Write the whole register, atomically.

    Any column present in any row is written, so hand-added columns are not
    quietly deleted the next time Prism touches the file — which would be a
    very fast way to lose somebody's trust in it.
    """
    with _Locked(path):
        _write(rows, path)


def _write(rows: list[dict], path: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)

    extra = [k for row in rows for k in row if k not in COLUMNS]
    seen, columns = set(), list(_BASE_COLUMNS)
    for key in extra:
        if key not in seen:
            seen.add(key)
            columns.append(key)
    # After the owner's own columns, so an old register's hand-added
    # "Salesman" column stays where they put it — see PIPELINE_COLUMNS.
    columns += PIPELINE_COLUMNS

    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})
            f.flush()
            os.fsync(f.fileno())
        _retry(os.replace, tmp, path)
    except PermissionError as e:
        _cleanup(tmp)
        raise _friendly_lock(path) from e
    except OSError as e:
        _cleanup(tmp)
        raise RegisterLocked(f"Couldn't write the inquiry register: {e}") from e


def row_keys(rows: list[dict]) -> set:
    """What identifies each row — kept by a screen next to the rows it read,
    so save_merged() can tell a row it deleted from one added since."""
    return {k for k in (_merge_key(r) for r in rows) if k is not None}


def save_merged(rows: list[dict], path: str, known: set | None = None) -> list[dict]:
    """Save a list read earlier without erasing rows added to the file since.

    The inquiry screen holds the register in memory and writes it back after
    an edit. Between the read and the write a mail check, a second Prism on
    a shared folder, or the owner in Excel may have added a row; a plain
    save() wiped it, and the next check handed its number out again.

    `known` is row_keys() of the list as it was read. A row on disk that is
    in neither `rows` nor `known` was added since and is kept, after ours.
    A row in `known` but not in `rows` was deleted on purpose and stays
    gone. Without `known` this is a plain save(). Returns the rows kept.
    """
    if known is None:
        save(rows, path)
        return []
    with _Locked(path):
        try:
            on_disk = load(path)
        except RegisterLocked:
            on_disk = []
        mine = row_keys(rows)
        # A hand-kept row with no number is known by (Email, Date, Product).
        # If a check numbered it after this list was read, its key on disk
        # changed and it looked "added since" — the same row twice. It is
        # the row we hold: it takes the number (and folder and thread) the
        # check gave it, unless we deleted it.
        unnumbered = {_triple_key(r): r for r in rows
                      if not (r.get("Inquiry no") or "").strip()
                      and _triple_key(r) is not None}
        added = []
        for r in on_disk:
            key = _merge_key(r)
            if key is None or key in mine or key in known:
                continue
            triple = _triple_key(r)
            if key[0] == "no" and triple is not None and triple in unnumbered:
                ours = unnumbered.pop(triple)
                for column in ("Inquiry no", "Folder", "Thread"):
                    if not (ours.get(column) or "").strip():
                        ours[column] = r.get(column, "")
                continue
            if key[0] == "no" and triple is not None and triple in known:
                continue          # numbered since, and deleted here since
            added.append(r)
        _write(list(rows) + added, path)
    return added


def snapshot(rows: list[dict]) -> list:
    """What save_changes() compares against: each row as it was read."""
    return [(row, _merge_key(row), dict(row)) for row in rows]


# Columns where both sides' additions are kept when the file changed under
# us: thread ids are a set, notes and the drawing list only ever grow.
_APPENDED = {"Notes": " | ", "Drawing": ", "}


def save_changes(rows: list[dict], path: str, before: list) -> list[dict]:
    """Write back only what this run changed, onto the file as it is now.

    The mail check reads the register, spends a minute on AI calls and
    OCR, then writes. A plain save() of its copy brought back rows the
    owner deleted meanwhile and reverted statuses and quotation numbers
    recorded on screen. Instead, under the lock: re-read the file, append
    the rows this run added, and on rows that were already there apply
    only the fields this run changed (`before` is snapshot() of the rows
    as read). A row deleted on disk meanwhile stays deleted. A field the
    owner changed meanwhile keeps the owner's value, except Thread (both
    sets of ids) and Notes/Drawing (our addition appended to theirs).
    Returns the rows now in the file.
    """
    originals = {id(row) for row, _key, _old in before}
    added = [r for r in rows if id(r) not in originals]
    with _Locked(path):
        on_disk = load(path)
        index = {}
        for r in on_disk:
            key = _merge_key(r)
            if key is not None:
                index.setdefault(key, r)
        for row, key, old in before:
            if key is None or (target := index.get(key)) is None:
                continue
            for column in set(row) | set(old):
                new, was = row.get(column, "") or "", old.get(column, "") or ""
                if new == was:
                    continue
                now = target.get(column, "") or ""
                if now == was:
                    target[column] = new
                elif column == "Thread":
                    target[column] = " ".join(sorted(set(now.split()) | set(new.split())))
                elif column in _APPENDED and new.startswith(was):
                    extra = new[len(was):].lstrip(_APPENDED[column])
                    target[column] = (f"{now}{_APPENDED[column]}{extra}"
                                      if now and extra else (now or extra))
        merged = on_disk + added
        _write(merged, path)
    return merged


def _cleanup(tmp: str) -> None:
    try:
        os.unlink(tmp)
    except OSError:
        pass


# ── rows ──────────────────────────────────────────────────────────────────────

def blank_row() -> dict:
    return {c: "" for c in COLUMNS}


def thread_key(message) -> str:
    """The identifiers that tie a conversation together.

    Message-Id plus References, space separated. Mail clients quote these in
    every reply, so matching on any one of them is what keeps a four-message
    negotiation as one row instead of four inquiries.
    """
    ids = [getattr(message, "message_id", "") or ""]
    ids += list(getattr(message, "references", []) or [])
    reply_to = getattr(message, "in_reply_to", "") or ""
    if reply_to:
        ids.append(reply_to)
    return " ".join(sorted({i.strip() for i in ids if i.strip()}))


_REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg)\s*(\[\d+\])?\s*:\s*)+", re.I)

def _subject_core(subject: str) -> str:
    """The subject without its Re:/Fwd: prefixes, lower-cased, one-spaced."""
    return " ".join(_REPLY_PREFIX.sub("", subject or "").lower().split())


def _same_subject(a: str, b: str) -> bool:
    """One subject is the other, or begins it, word for word — "Enquiry for
    springs" and "Enquiry for springs — please quote 5000". Whole words, so
    "Re: Hi" is not a reply to an inquiry about hinges."""
    a, b = re.findall(r"\w+", a), re.findall(r"\w+", b)
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return long_[:len(short)] == short


def _reads_as_reply(message) -> bool:
    """A Re:/Fwd: subject, or reply headers pointing at an earlier mail."""
    return bool(_REPLY_PREFIX.match(getattr(message, "subject", "") or "")
                or (getattr(message, "in_reply_to", "") or "").strip()
                or list(getattr(message, "references", []) or []))


def _names_row(text: str, row: dict) -> bool:
    """Whether the mail quotes this row's inquiry or quotation number, in
    either the INQ/26-27/0005 or the folder's INQ-26-27-0005 spelling."""
    for column in ("Inquiry no", "Quotation no"):
        number = (row.get(column) or "").strip()
        if len(number) < 4:
            continue
        for spelling in {number, number.replace("/", "-")}:
            pattern = r"(?<![\w/-])" + re.escape(spelling) + r"(?![\w/-])"
            if re.search(pattern, text, re.I):
                return True
    return False


def find_by_thread(rows: list[dict], message, sent=None) -> dict | None:
    """The row this message belongs to, or None when it is new business.
    See match() for the rules."""
    return match(rows, message, sent)[0]


# How match() tied a mail to a row. Only the first two are certain enough
# to let the mail rewrite what the row says was asked.
BY_THREAD, BY_NUMBER, BY_SUBJECT = "thread", "number", "subject"


def match(rows: list[dict], message, sent=None) -> tuple[dict | None, str]:
    """(row, how) for the row this message belongs to; (None, "") when it is
    new business.

    Thread identifiers are exact and are tried first. After that a mail only
    joins an earlier inquiry from the same address when it clearly belongs
    to it:

      · it names that inquiry's number or its quotation number;
      · it is a reply (Re:/Fwd: subject, or reply headers) and its subject,
        without the Re:/Fwd:, is that open inquiry's "Product asked" or the
        subject of a mail we sent on it. `sent` is {inquiry no: [subjects]}
        from the sent log.

    Everything else is a new inquiry with its own number, even from a
    customer who has one open — the owner's rule is that a mail joins only
    when it is clearly a reply. Folding a fresh "Enquiry for lockers" into
    last week's springs row gave two jobs one number, one folder and one
    "Product asked", and the second job never showed up in "To quote". A
    forwarded "Fwd: Enquiry for SS lockers" is the same thing: purchase
    departments forward plant requirements as new enquiries. And a mail
    with no reply markers at all — "SS Locker 8 Comp", "Urgent", an empty
    subject — is never taken as an answer to a quotation. Closed inquiries
    never catch a subject match, so a new job from an old customer is not
    swallowed into last year's row.
    """
    ids = {i for i in thread_key(message).split() if i}
    if ids:
        for row in rows:
            known = {i for i in (row.get("Thread") or "").split() if i}
            if known & ids:
                return row, BY_THREAD
    sender = (getattr(message, "from_addr", "") or "").strip().lower()
    if not sender:
        return None, ""
    mine = [r for r in rows if (r.get("Email") or "").strip().lower() == sender]
    if not mine:
        return None, ""

    subject = getattr(message, "subject", "") or ""
    text = f"{subject}\n{(getattr(message, 'body', '') or '')[:8000]}"
    for row in reversed(mine):
        if _names_row(text, row):
            return row, BY_NUMBER

    if not _reads_as_reply(message):
        return None, ""
    core = _subject_core(subject)
    if not core:
        return None, ""
    for row in reversed(mine):
        if (row.get("Status") or "").strip() not in OPEN_STATUSES:
            continue
        ours = [row.get("Product asked") or ""]
        ours += list((sent or {}).get((row.get("Inquiry no") or "").strip().upper(), ()))
        for text in ours:
            if _same_subject(core, _subject_core(text)):
                return row, BY_SUBJECT
    return None, ""


def find(rows: list[dict], inquiry_no: str) -> dict | None:
    wanted = (inquiry_no or "").strip().upper()
    if not wanted:
        # A blank number names nothing. Matching it returned the first row
        # with a blank number, so every unnumbered row read as that one.
        return None
    for row in rows:
        if (row.get("Inquiry no") or "").strip().upper() == wanted:
            return row
    return None


def add_thread(row: dict, message) -> None:
    """Fold a message's identifiers into a row, without losing the old ones."""
    ids = {i for i in (row.get("Thread") or "").split() if i}
    ids |= {i for i in thread_key(message).split() if i}
    row["Thread"] = " ".join(sorted(ids))


# Subjects that say nothing about what was wanted. Half of all inquiries arrive
# under one of these, and on their own they are useless twice over: a human
# reading the register learns nothing, and matching them against a rate list
# finds nothing.
_GENERIC_SUBJECT = {
    "enquiry", "enquiry.", "inquiry", "quotation", "quote", "quotation required",
    "quotation request", "requirement", "requirements", "rfq", "query",
    "request", "price", "prices", "rate", "rates", "price list", "urgent",
    "hello", "hi", "regarding", "reg", "no subject", "your quotation",
}

_GREETING = ("dear", "hi ", "hii", "hello", "respected", "sir", "madam",
             "greetings", "good morning", "good afternoon", "good evening",
             "thanks", "thank you", "regards")


def _first_real_line(body: str, limit: int = 140) -> str:
    """The first line of an email that is not a greeting — what they want."""
    for line in (body or "").splitlines():
        line = line.strip(" >\t")
        if len(line) < 8:
            continue
        if line.lower().startswith(_GREETING):
            continue
        return line[:limit]
    return ""


def product_summary(message) -> str:
    """What was asked for, for the register and for matching a rate list.

    Uses the subject when the subject carries information. When it does not —
    a bare "Enquiry", which is how half of them arrive — the opening line of
    the message is added, because that is the line a person would read to find
    out what the mail is about.
    """
    subject = re.sub(r"^\s*(re|fw|fwd)\s*:\s*", "",
                     getattr(message, "subject", "") or "",
                     flags=re.IGNORECASE).strip()
    opening = _first_real_line(getattr(message, "body", "") or "")

    words = [w for w in re.findall(r"[A-Za-z0-9]+", subject) if len(w) > 1]
    named = bool(subject) and subject.lower() not in _GENERIC_SUBJECT and len(words) >= 4

    # The digits are the specification. "Enquiry for compression springs" names
    # the product and reads fine, but every spring on the rate list is a
    # compression spring — what picks the row is "2mm wire, 25 OD", and that
    # lives in the body. So a subject without numbers is never the whole story
    # when the first line of the message has them.
    subject_has_spec = bool(re.search(r"\d", subject))
    opening_has_spec = bool(re.search(r"\d", opening))

    if named and (subject_has_spec or not opening_has_spec):
        return subject[:200]
    if subject and opening:
        return f"{subject} — {opening}"[:200]
    return (opening or subject)[:200]


def from_message(message, details: dict | None = None, *, prefix: str = "INQ",
                 rows: list[dict] | None = None, folder: str = "",
                 taken=()) -> dict:
    """A fresh register row for a message that triage called an inquiry.

    `details` is whatever the extraction step worked out — product, quantity,
    contact name. It is allowed to be empty: a row with the sender, the date
    and the subject is still a row, and an inquiry that is in the register
    imperfectly beats one that is only in the inbox.
    """
    details = details or {}
    row = blank_row()
    row["Inquiry no"] = next_number(rows or [], prefix,
                                    getattr(message, "date", None) or date.today(),
                                    taken)
    when = getattr(message, "date", None)
    row["Date received"] = when.strftime("%d-%m-%Y") if when else date.today().strftime("%d-%m-%Y")
    # Only from a real timestamp. A row that fell back to today's date must
    # not carry the clock time of the moment Prism happened to run — that
    # would read as the time the customer wrote, and it isn't.
    row["Time received"] = _clock(when)
    row["Customer"] = details.get("customer") or getattr(message, "from_name", "") or ""
    row["Contact person"] = details.get("contact") or getattr(message, "from_name", "") or ""
    row["Email"] = getattr(message, "from_addr", "") or ""
    row["Phone"] = details.get("phone", "")
    row["Product asked"] = details.get("product") or product_summary(message)
    row["Quantity"] = details.get("quantity", "")
    names = list(getattr(message, "attachment_names", []) or [])
    row["Drawing"] = ", ".join(names) if names else "No"
    row["Status"] = NEW
    row["Last contact"] = row["Date received"]
    row["Folder"] = folder
    row["Notes"] = details.get("notes", "")
    add_thread(row, message)
    return row


def _clock(when) -> str:
    """"14:35" in the reader's own timezone, or "" when there is no timestamp.

    A mail Date header carries the SENDER's offset. Left as-is, a 9 a.m.
    enquiry from Germany files itself at 09:00 in a Gujarat register and the
    column stops meaning "when it reached us" — which is the only thing the
    owner reads it as. Naive timestamps are left alone: there is nothing to
    convert from, and guessing would be worse than the plain number.
    """
    if not isinstance(when, datetime):
        return ""
    if when.tzinfo is not None:
        try:
            when = when.astimezone()
        except (OSError, OverflowError, ValueError):
            pass       # a broken local timezone must not lose us the row
    return when.strftime("%H:%M")


def _merge_key(row: dict):
    """What makes two rows "the same inquiry" for merge_in().

    Inquiry no first — it is the one column meant to be a unique key, and a
    register kept in Excel before Prism will usually have re-typed Prism's
    own numbers onto rows it re-exported. Falling back to (Email, Date
    received, Product asked) covers a hand-kept sheet that never had a
    numbering scheme of its own; falling back further to nothing at all —
    rather than colliding every blank row into one "duplicate" — means a
    sheet with several genuinely blank entries does not lose all but the
    first of them.
    """
    no = (row.get("Inquiry no") or "").strip()
    if no:
        return ("no", no.lower())
    return _triple_key(row)


def _triple_key(row: dict):
    triple = ((row.get("Email") or "").strip().lower(),
              (row.get("Date received") or "").strip(),
              (row.get("Product asked") or "").strip().lower())
    return ("triple", triple) if any(triple) else None


def merge_in(existing: list[dict], incoming: list[dict]) -> tuple[list, int, int]:
    """Append rows from a CSV the owner kept before Prism, or one a colleague
    built up separately — skipping any that are clearly already here.

    Never rewrites or reorders a row already in `existing`; only adds. See
    _merge_key() for what counts as "already here". Returns (merged rows,
    how many were added, how many were left out as duplicates).
    """
    seen = {key for key in (_merge_key(r) for r in existing) if key is not None}
    merged = list(existing)
    added = skipped = 0
    for row in incoming:
        key = _merge_key(row)
        if key is not None and key in seen:
            skipped += 1
            continue
        if key is not None:
            seen.add(key)
        merged.append(dict(row))
        added += 1
    return merged, added, skipped


def update(rows: list[dict], inquiry_no: str, changes: dict) -> dict | None:
    """Apply changes to one row in place. Returns the row, or None if unknown."""
    row = find(rows, inquiry_no)
    if row is None:
        return None
    row.update({k: ("" if v is None else v) for k, v in changes.items()})
    return row


def mark_quoted(row: dict, quote_no: str, value, when: date | None = None) -> dict:
    when = when or date.today()
    row["Status"] = QUOTED
    row["Quotation no"] = quote_no
    row["Quotation date"] = when.strftime("%d-%m-%Y")
    row["Quotation value"] = money_str(value)
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


def mark_lost(row: dict, reason: str = "", when: date | None = None) -> dict:
    when = when or date.today()
    row["Status"] = NOT_CONVERTED
    row["Result"] = NOT_CONVERTED
    row["Reason if lost"] = reason
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


def mark_converted(row: dict, po_number: str, value, when: date | None = None) -> dict:
    when = when or date.today()
    row["Status"] = CONVERTED
    row["Result"] = CONVERTED
    row["PO number"] = po_number
    row["PO date"] = when.strftime("%d-%m-%Y")
    row["Order value"] = money_str(value)
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


# What a reply means, in register terms. The keys are mailflow's intents; they
# are strings rather than an import because mailflow imports this module and a
# cycle between the two would be a silly thing to introduce for four words.
#
# Note what is NOT here: "accepted" does not become Converted. A customer
# saying "go ahead" is a promise; Converted is a fact backed by a PO number and
# an order value. Collapsing the two would make the conversion figure in the
# month-end summary optimistic by exactly the orders that never arrived, and
# that figure is the one number the owner would repeat to a bank.
REPLY_STATUS = {
    "accepted": ACCEPTED,
    "rejected": NOT_CONVERTED,
    "negotiating": NEGOTIATING,
    "needs_info": NEGOTIATING,
}


def mark_reply(row: dict, intent: str, when: date | None = None,
               note: str = "", ask=None) -> dict:
    """Move a row on because the customer answered.

    An intent Prism could not read leaves the status exactly as it was and
    only touches Last contact. That is deliberate: a wrong status is worse
    than a stale one, because the owner acts on the register without
    re-reading the mail, and a quotation wrongly marked Not converted is one
    they will never chase again.
    """
    when = when or date.today()
    row["Last contact"] = when.strftime("%d-%m-%Y")
    if note:
        row["Notes"] = f"{(row.get('Notes') or '').strip()} {note}".strip()
    status = REPLY_STATUS.get((intent or "").strip().lower())
    if not status:
        return row
    if status == NOT_CONVERTED:
        # Reuse mark_lost so Result and Reason are filled the same way they are
        # when the owner closes a row by hand — one shape of "lost" in the file.
        return mark_lost(row, "Declined by customer", when)
    if status == NEGOTIATING:
        # They answered, so the next move is ours — and a price they named
        # is kept for the card (see note_counter).
        return note_counter(row, ask, when)
    row["Status"] = status
    return row


def note_reminder(row: dict, when: date | None = None) -> dict:
    when = when or date.today()
    try:
        sent = int(str(row.get("Reminders sent") or "0").strip() or 0)
    except ValueError:
        sent = 0
    row["Reminders sent"] = str(sent + 1)
    row["Status"] = FOLLOWING_UP
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


# ── the pipeline: stages, revisions, counter-offers, orders ─────────────────
# Stage MEANINGS are fixed and are worked out here from the status the file
# already holds plus the pipeline columns — no new statuses, so a register
# from before the pipeline reads the same. The words on screen and which
# optional steps a client uses are the client's (inquiry_config.pipeline_of)
# and never live in this module.

STAGE_INQUIRY, STAGE_QUOTED, STAGE_NEGO = "inquiry", "quoted", "nego"
STAGE_PO, STAGE_WON, STAGE_LOST = "po", "won", "lost"
STAGES = (STAGE_INQUIRY, STAGE_QUOTED, STAGE_NEGO, STAGE_PO, STAGE_WON,
          STAGE_LOST)

# Optional steps, as a client switches them. Missing means this default —
# which is today's behaviour. inquiry_config.PIPELINE_DEFAULTS["steps"]
# carries the same values (a test holds the two together).
STEP_DEFAULTS = {"negotiation": True, "details": True,
                 "acknowledgement": True, "proforma": False}


def step_on(pipeline: dict | None, step: str) -> bool:
    """Whether an optional step is switched on in `pipeline` (the dict at
    cfg["inquiry"]["pipeline"], or None). Anything unreadable is the
    default."""
    steps = (pipeline or {}).get("steps") if isinstance(pipeline, dict) else None
    value = steps.get(step) if isinstance(steps, dict) else None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.strip():
        return value.strip().lower() not in ("0", "false", "no", "off")
    return STEP_DEFAULTS.get(step, False)


def _flag(row: dict, column: str) -> bool:
    return (row.get(column) or "").strip().lower() in (YES, "y", "true", "1")


# Hand-typed statuses (the Client-file / Excel workflow): Prism's own words
# in any case and spacing, and a few common words for the same thing. One
# reader for the board (stage_of) and the colours (documents.status_key),
# so the two views can never disagree about a row (review R8).
STATUS_ALIASES = {
    "won": CONVERTED, "order": CONVERTED, "order received": CONVERTED,
    "lost": NOT_CONVERTED, "not converted": NOT_CONVERTED,
    "follow up": FOLLOWING_UP, "followup": FOLLOWING_UP,
    "following-up": FOLLOWING_UP, "negotiation": NEGOTIATING,
    "quote sent": QUOTED, "quotation sent": QUOTED,
}


def normalise_status(text) -> str:
    """The register status a cell means: Prism's spelling for any case or
    spacing of its own words or an alias, "New" for blank, and anything
    else returned as typed (collapsed whitespace)."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return NEW
    folded = raw.casefold()
    for status in STATUSES:
        if folded == status.casefold():
            return status
    return STATUS_ALIASES.get(folded, raw)


def stage_of(row: dict, pipeline: dict | None = None) -> tuple[str, str]:
    """(stage, sub) for one row — where its card sits on the board.

      inquiry   "you" (yours to answer) · "them" (you asked for details)
      quoted    "" · "countered" (they countered, negotiation switched off)
      nego      ""
      po        "check" (a PO number to check) · "nopo" (a PO with no
                quotation — price it) · "yes" (they said yes, PO awaited)
                · "proforma" (proforma sent, advance awaited)
      won, lost ""

    Optional steps switched off fold the way the approved design does: a
    negotiating row reads as quoted/countered, "asked for details" as
    yours to answer, and a proforma left from when the step was on as won.
    A blank or unknown status is an inquiry, or quoted if it has a
    quotation number — a hand-kept row is shown, never dropped. Pure.
    """
    status = normalise_status(row.get("Status"))
    quoted = bool((row.get("Quotation no") or "").strip())
    if status in (QUOTED, FOLLOWING_UP):
        return STAGE_QUOTED, ""
    if status == NEGOTIATING:
        if not step_on(pipeline, "negotiation"):
            return STAGE_QUOTED, "countered"
        return STAGE_NEGO, ""
    if status == ACCEPTED:
        if _flag(row, NO_QUOTATION) and not quoted:
            return STAGE_PO, "nopo"
        proforma = (row.get(PROFORMA) or "").strip().lower()
        if proforma == PROFORMA_SENT:
            if not step_on(pipeline, "proforma"):
                return STAGE_WON, ""
            return STAGE_PO, "proforma"
        if proforma == PROFORMA_DUE and step_on(pipeline, "proforma"):
            # Accepted, the proforma not sent yet: still ours to send.
            return STAGE_PO, "proforma"
        if (row.get("PO number") or "").strip():
            return STAGE_PO, "check"
        return STAGE_PO, "yes"
    if status == CONVERTED:
        return STAGE_WON, ""
    if status == NOT_CONVERTED:
        return STAGE_LOST, ""
    if status != NEW and quoted:
        return STAGE_QUOTED, ""
    turn = (row.get(TURN) or "").strip().lower()
    if turn == TURN_THEM and step_on(pipeline, "details"):
        return STAGE_INQUIRY, "them"
    return STAGE_INQUIRY, "you"


def revision_of(row: dict) -> int:
    """0 for the first quotation, n for "Rev n". The first whole number in
    the cell, so a hand-edited "Rev 1", "R2" or Excel's "2.0" still reads as
    1 and 2 (review R6) — reading them as 0 made the next revision reuse a
    file name the customer already holds. Unreadable is 0."""
    m = re.search(r"\d+", str(row.get(REVISION) or ""))
    return int(m.group(0)) if m else 0


def revision_on_disk(folder: str, quote_no: str) -> int:
    """The highest -Rn file of this quotation in `folder` (0 when none)."""
    base = quote_file_name(quote_no, 0, "")
    if not folder or not base or not os.path.isdir(folder):
        return 0
    tail = re.compile(re.escape(base) + r"-R(\d+)\.[A-Za-z0-9]+$", re.I)
    best = 0
    try:
        for name in os.listdir(folder):
            m = tail.match(name)
            if m:
                best = max(best, int(m.group(1)))
    except OSError:
        return best
    return best


def next_revision(row: dict) -> int:
    """The revision number to send next: one above both the register's
    Revision and every -Rn file already in the folder, so a revision the
    customer holds is never overwritten."""
    folder = row.get("Folder") or ""
    number = row.get("Quotation no") or ""
    n = max(revision_of(row), revision_on_disk(folder, number)) + 1
    while folder and any(
            os.path.exists(os.path.join(folder, quote_file_name(number, n, ext)))
            for ext in ("pdf", "csv")):
        n += 1
    return n


# One amount in a price-history cell: plain digits, or Indian/Western
# grouping ("1,25,000", "125,000"), with optional decimals and currency.
_HISTORY_AMOUNT = re.compile(
    r"^\s*(?:₹|rs\.?|inr)?\s*(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3}|\d+)"
    r"(?:\.\d+)?\s*(?:/-)?\s*$", re.I)


def _history_parts(cell: str) -> list[str]:
    parts = []
    for chunk in re.split(r"[;|\n]", cell or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if _HISTORY_AMOUNT.match(chunk):
            parts.append(chunk)
        else:
            # "125000, 118000" typed by hand: a comma list of amounts.
            parts.extend(p.strip() for p in chunk.split(",") if p.strip())
    return parts


def price_history(row: dict) -> list[Decimal]:
    """Every earlier total of this quotation, oldest first. See the
    PIPELINE_COLUMNS note for the format; "; " is ours, but a hand-typed
    "125000, 118000" reads too (review R13). An unreadable, zero or
    negative entry is skipped, never turned into a price."""
    out = []
    for part in _history_parts(row.get(PRICE_HISTORY) or ""):
        if not _HISTORY_AMOUNT.match(part):
            continue
        value = money(part)
        if value > 0:
            out.append(value)
    return out


def quote_label(row: dict) -> str:
    """"QTN/26-27/0101 Rev 1" — the number as the customer sees it."""
    number = (row.get("Quotation no") or "").strip()
    revision = revision_of(row)
    return f"{number} Rev {revision}" if number and revision else number


def quote_file_name(quote_no: str, revision: int = 0, ext: str = "pdf") -> str:
    """QTN-26-27-0101.pdf, and QTN-26-27-0101-R1.pdf for Rev 1. The same
    number with an -Rn tail, which numbers_in_use() and next_number() read
    as that same number, never as a new one."""
    name = (quote_no or "").strip().replace("/", "-").replace("\\", "-")
    if revision and revision > 0:
        name = f"{name}-R{int(revision)}"
    ext = (ext or "").lstrip(".")
    return f"{name}.{ext}" if ext else name


def revise_quotation(row: dict, new_value, when: date | None = None,
                     revision: int | None = None) -> dict:
    """Record a revised quotation: same number, Revision + 1, the old total
    appended to Price history, back to Quoted with their ask cleared and
    the reminders starting again. Sends nothing — the caller sends the
    revised PDF (quote_file_name(no, revision_of(row))) and calls this
    once it has gone.

    A row with no quotation number has nothing to revise; that is a
    ValueError rather than a quietly invented "Rev 1" of nothing.
    """
    if not (row.get("Quotation no") or "").strip():
        raise ValueError("This inquiry has no quotation to revise.")
    when = when or date.today()
    cell = (row.get(PRICE_HISTORY) or "").strip()
    history = price_history(row)
    old = (row.get("Quotation value") or "").strip()
    added = f"{money(old):.2f}" if old and money(old) > 0 else ""
    if len(history) < len(_history_parts(cell)):
        # Something in the cell could not be read: never shrink what the
        # owner typed — keep it as it is and append (review R13).
        row[PRICE_HISTORY] = "; ".join(x for x in (cell, added) if x)
    else:
        row[PRICE_HISTORY] = "; ".join(
            [f"{v:.2f}" for v in history] + ([added] if added else []))
    row[REVISION] = str(revision if revision and revision > 0
                        else revision_of(row) + 1)
    row["Quotation value"] = money_str(new_value)
    row["Status"] = QUOTED
    row[THEIR_ASK] = ""
    row[TURN] = TURN_THEM
    row["Reminders sent"] = "0"
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


def note_counter(row: dict, ask=None, when: date | None = None) -> dict:
    """They countered: Negotiating, your turn, and their price when it is
    known. An ask of None leaves an earlier one in place rather than
    erasing what was read off a previous mail."""
    when = when or date.today()
    row["Status"] = NEGOTIATING
    row[TURN] = TURN_YOU
    if ask is not None and money(ask) > 0:
        row[THEIR_ASK] = money_str(ask)
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


def hold_price(row: dict, when: date | None = None) -> dict:
    """You held the price — the next move is theirs. Status unchanged."""
    when = when or date.today()
    row[TURN] = TURN_THEM
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


def mark_asked_details(row: dict, when: date | None = None) -> dict:
    """You asked for details before quoting: still an inquiry, their turn."""
    when = when or date.today()
    row[TURN] = TURN_THEM
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


def mark_po_without_quote(row: dict, po_number: str = "",
                          when: date | None = None) -> dict:
    """A PO arrived with no quotation behind it: Accepted, flagged, so the
    board shows it as "price it from the rate list" instead of as a new
    inquiry waiting for a quotation nobody will send. A PO number, when
    one was read, is kept; an empty one never erases a typed one."""
    when = when or date.today()
    row["Status"] = ACCEPTED
    row[NO_QUOTATION] = YES
    if (po_number or "").strip():
        row["PO number"] = po_number.strip()
        row["PO date"] = when.strftime("%d-%m-%Y")
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return row


def flag_maybe_po(row: dict) -> dict:
    """The mail check thinks this inquiry's latest mail may be a PO, but
    not unmistakably (review R1). Nothing moves: Status and the stage stay
    as they are, and "No quotation" reads "maybe" (stage_of() only acts on
    "yes"), so the card can say "Looks like a PO" until the owner presses
    "They sent a PO" (mark_po_without_quote) or "Not an order". A "yes"
    already set is never weakened."""
    if not _flag(row, NO_QUOTATION):
        row[NO_QUOTATION] = MAYBE
    return row


def maybe_po(row: dict) -> bool:
    """True while the row carries flag_maybe_po()'s flag."""
    return (row.get(NO_QUOTATION) or "").strip().lower() == MAYBE


def mark_proforma_due(row: dict, pipeline: dict | None = None,
                      when: date | None = None) -> bool:
    """The PO is accepted and a proforma has to go out: Accepted, Proforma
    "to send". Nothing is recorded as sent — mark_proforma_sent() does
    that once the letter has actually gone. False, and no change, when the
    proforma step is off."""
    if not step_on(pipeline, "proforma"):
        return False
    when = when or date.today()
    row["Status"] = ACCEPTED
    row[PROFORMA] = PROFORMA_DUE
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return True


def proforma_due(row: dict) -> bool:
    return (row.get(PROFORMA) or "").strip().lower() == PROFORMA_DUE


def mark_proforma_sent(row: dict, pipeline: dict | None = None,
                       when: date | None = None) -> bool:
    """The proforma went out; the order waits for the advance. Only when
    the client uses the proforma step — otherwise nothing changes and this
    returns False, so a stray call cannot park an order in a stage the
    client's board does not have."""
    if not step_on(pipeline, "proforma"):
        return False
    when = when or date.today()
    row["Status"] = ACCEPTED
    row[PROFORMA] = PROFORMA_SENT
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return True


def mark_advance_received(row: dict, pipeline: dict | None = None,
                          value=None, when: date | None = None) -> bool:
    """The advance against the proforma arrived: Converted. Keeps the PO
    number and date already on the row; the order value is `value`, else
    what is already there, else the quotation's. False, and no change,
    when the proforma step is off."""
    if not step_on(pipeline, "proforma"):
        return False
    when = when or date.today()
    row["Status"] = CONVERTED
    row["Result"] = CONVERTED
    row[PROFORMA] = PROFORMA_PAID
    if value is not None:
        row["Order value"] = money_str(value)
    elif not (row.get("Order value") or "").strip():
        row["Order value"] = money_str(row.get("Quotation value") or 0)
    row["Last contact"] = when.strftime("%d-%m-%Y")
    return True


# Reading the price out of a counter-offer. Strict on purpose: a wrong ask
# on the card is worse than none, because the owner prices the revision off
# it. Three things make a number a price — a currency in front, a lakh/k
# multiplier or "/-" behind, or a price word just before it — and any
# number that is a quantity, a percentage or a discount is dropped.
_ASK = re.compile(
    r"(?<![\w.,/])(?P<cur>(?:₹|rs\.?|inr|usd|eur|gbp|aed|\$|€|£)\s*)?"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<mult>lakhs?|lacs?|crores?|cr|thousand|k|l)(?![a-z]))?"
    r"(?P<dash>\s*/-)?", re.I)
_GROUPED = re.compile(r"^\d{1,3}(,\d{3})+$|^\d{1,2}(,\d{2})*,\d{3}$|^\d+$")
_MULTIPLIER = {"lakh": 100000, "lakhs": 100000, "lac": 100000, "lacs": 100000,
               "l": 100000, "crore": 10000000, "crores": 10000000,
               "cr": 10000000, "thousand": 1000, "k": 1000}
# Multipliers that only ever mean money. "k" and "thousand" also count
# units ("100k units"), so on their own they are not a currency mark.
_MONEY_MULTIPLIERS = ("lakh", "lakhs", "lac", "lacs", "crore", "crores", "cr")
# A price word just before the figure. Deliberately no "at", "for", "do" or
# "pay": "call me at 98765…", "delivery at 380015", "at 2026 rates" and
# "can you do 100 pcs" are not prices. Those words count only when a
# currency follows them, which the currency mark already covers.
_PRICE_BEFORE = re.compile(
    r"\b(price|rate|budget|offer|amount|total|cost|value|upto|up\s+to|"
    r"max(imum)?)\b\W{0,3}(of|is|be|:|=|to)?\W{0,3}$", re.I)
_NOT_A_PRICE_BEFORE = re.compile(
    r"\b(discount|less|reduc\w*(\s+it)?\s+by|rebate|extra|minus|call\s+(?:me\s+)?at)\b"
    r"\W{0,3}(of|by)?\W{0,3}$", re.I)
_NOT_A_PRICE_AFTER = re.compile(r"^\s*(%|percent|off\b|less\b|discount)", re.I)
# A figure followed by these is a per-unit rate, not the total they ask.
_PER_UNIT_AFTER = re.compile(
    r"^\s*(?:@|/\s*-?\s*(?:unit|pc|pcs|piece|kg|nos?|mtr|meter|metre|ltr|set)\b|"
    r"per\b|each\b|apiece\b|a\s+piece\b)", re.I)
# Six digits after one of these, with no currency: an Indian PIN code.
_PIN_BEFORE = re.compile(r"\b(?:pin|pincode|pin\s*code|delivery|deliver(?:ed)?\s+to|at)\b"
                         r"\W{0,3}$", re.I)


def read_ask(text: str, quoted=None) -> Decimal | None:
    """The price a customer's counter-offer names, or None when unsure.

    Reads "₹1,10,000", "Rs. 110000/-", "$110,000", "1.1 lakh" and
    "price 1,10,000". Indian and Western digit grouping both, and nothing
    else — "1,1000" is a typo, not a number to guess at.

    Dropped on purpose (review R2, 2026-10-13): phone numbers (any run of 10+
    digits), PIN codes (six digits after pin/delivery/at with no currency),
    per-unit rates ("75000 per kg", "95,000 each", "/unit", "@"), quantities
    ("100k units", "50 nos"), and percentages or discounts. With no readable
    quotation total to compare against, only a figure carrying a currency,
    a "/-" or a lakh/crore is believed. With the quotation's total as
    `quoted`, a figure that is not a plausible counter (our own price read
    back, or under 30% of it) is dropped. Two different plausible figures
    are unsure, so None.
    """
    from .quoting import _QTY_UNITS
    unit_after = re.compile(r"^\s*(?:" + _QTY_UNITS + r")\b", re.I)
    text = text or ""
    quoted = money(quoted) if quoted not in (None, "") else Decimal(0)
    found = set()
    for m in _ASK.finditer(text):
        raw = m.group("num").rstrip(",")
        whole = raw.split(".")[0]
        if not _GROUPED.match(whole):
            continue
        digits = re.sub(r"\D", "", whole)
        if len(digits) >= 10:
            continue                  # a phone number, an account number
        cur, dash = bool(m.group("cur")), bool(m.group("dash"))
        mult = (m.group("mult") or "").lower()
        if mult == "l" and not cur:
            continue                  # "5 l" is litres until a ₹ says otherwise
        before = text[max(0, m.start() - 30):m.start()]
        after = text[m.end():m.end() + 15]
        if _NOT_A_PRICE_BEFORE.search(before) or _NOT_A_PRICE_AFTER.match(after):
            continue
        # Units are checked after the multiplier too: "100k units".
        if _PER_UNIT_AFTER.match(after) or unit_after.match(after):
            continue
        if len(digits) == 6 and not cur and _PIN_BEFORE.search(before):
            continue
        money_mark = cur or dash or mult in _MONEY_MULTIPLIERS
        if quoted <= 0 and not money_mark:
            continue
        if not (money_mark or mult or _PRICE_BEFORE.search(before)):
            continue
        try:
            value = Decimal(raw.replace(",", "")) * _MULTIPLIER.get(mult, 1)
        except Exception:                               # noqa: BLE001
            continue
        if value <= 0:
            continue
        if quoted > 0 and not (quoted * Decimal("0.3") <= value < quoted):
            continue
        found.add(value.quantize(Decimal("0.01")))
    return found.pop() if len(found) == 1 else None


def plausible_ask(ask, quoted) -> bool:
    """True when `ask` can stand as their counter to `quoted`: a positive
    figure below our total and at least 30% of it. The same band read_ask()
    uses, for callers holding a value that came from the register file."""
    try:
        ask, quoted = money(ask), money(quoted)
    except Exception:                                   # noqa: BLE001
        return False
    return quoted > 0 and quoted * Decimal("0.3") <= ask < quoted


# ── money and dates, read back from a file a human has been editing ──────────

def money(value) -> Decimal:
    """A Decimal from whatever is in the cell — "₹ 1,42,500.00", "Rs.1,000/-".

    One parser, shared with quoting, so the register's idea of what a rupee
    looks like can never drift from the quotation's. Anything unreadable is
    zero rather than an exception: one mistyped cell must not stop the
    month-end summary.
    """
    from .quoting import to_decimal
    return to_decimal(value)


def money_str(value) -> str:
    """Plain digits with two decimals. No symbol, no grouping — the register is
    read by Excel as often as by a person, and a currency symbol in the cell
    turns the whole column into text."""
    return f"{money(value):.2f}"


_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y")


def parse_date(text: str) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ── what the owner is shown ───────────────────────────────────────────────────

@dataclass
class Summary:
    received: int = 0
    quoted: int = 0
    quoted_value: Decimal = Decimal(0)
    converted: int = 0
    converted_value: Decimal = Decimal(0)
    waiting: int = 0
    lost: int = 0
    reasons: dict | None = None

    @property
    def conversion(self) -> float:
        """Orders won as a percentage of quotations sent.

        Against quotations, not inquiries — an inquiry nobody quoted was never
        a chance, and counting it would make the number flattering and useless.
        """
        return round(100.0 * self.converted / self.quoted, 1) if self.quoted else 0.0


def summarise(rows: list[dict], since: date | None = None,
              until: date | None = None) -> Summary:
    """The month-end numbers. Rows with an unreadable date are counted in the
    totals but never filtered out by a date window they cannot be tested
    against — silently dropping somebody's order is worse than a loose count."""
    out = Summary(reasons={})
    for row in rows:
        received = parse_date(row.get("Date received", ""))
        if since and received and received < since:
            continue
        if until and received and received > until:
            continue
        out.received += 1
        status = (row.get("Status") or "").strip()
        if row.get("Quotation no"):
            out.quoted += 1
            out.quoted_value += money(row.get("Quotation value"))
        if status == CONVERTED:
            out.converted += 1
            out.converted_value += money(row.get("Order value"))
        elif status == NOT_CONVERTED:
            out.lost += 1
            reason = (row.get("Reason if lost") or "not given").strip() or "not given"
            out.reasons[reason] = out.reasons.get(reason, 0) + 1
        elif status in (QUOTED, FOLLOWING_UP):
            # Negotiating and Accepted both mean the customer already
            # replied — counting them here is what made the dashboard's
            # "Quotations with no answer yet" disagree with the working
            # window's own "No answer yet" tab, which only ever counted
            # these two. "Waiting on a reply" (EMAIL_WORKFLOW.md §8) means
            # literally that: nothing has come back yet.
            out.waiting += 1
    return out


def awaiting_followup(rows: list[dict], after_days: int = 3,
                      max_reminders: int = 3,
                      today: date | None = None) -> list[dict]:
    """Quotations that have gone quiet and are due a nudge.

    The single most valuable list in the file: it is the money already earned
    and not yet collected on. Stops after `max_reminders` because a fourth
    chase stops being a follow-up and starts being a nuisance.
    """
    today = today or date.today()
    due = []
    for row in rows:
        if (row.get("Status") or "").strip() not in (QUOTED, FOLLOWING_UP, NEGOTIATING):
            continue
        try:
            sent = int(str(row.get("Reminders sent") or "0").strip() or 0)
        except ValueError:
            sent = 0
        if sent >= max_reminders:
            continue
        last = parse_date(row.get("Last contact", "")) or parse_date(
            row.get("Quotation date", ""))
        if last and (today - last).days >= after_days:
            due.append(row)
    return due
