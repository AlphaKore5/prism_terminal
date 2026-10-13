"""
Prism — the daily loop
──────────────────────
Ties inbox, triage, register, quoting, sop and po together into the one thing
the owner actually does: press *Check my mail*.

One call to check() does everything that cannot cost money if it goes wrong —
fetch, sort, file the attachments, register the inquiries, work out what is due
a follow-up — and hands back a worklist of the things that need a person. It
never sends anything. Sending is the caller's decision, made once per item,
because both remaining human steps are about money leaving the company.

Errors do not raise out of check(). This runs on a ten-minute timer next to
somebody trying to run a factory: a mail server having a bad afternoon belongs
in a status line, not in a dialog box.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date

from . import history, inbox, ocr, register, sop, triage, ui, worklist
from .inbox import Message, State

# Sub-folders under the company folder. Flat, obvious names — the owner will
# open these in Explorer, and a folder called "artifacts" helps nobody.
INQUIRIES_DIR = "inquiries"
SOPS_DIR = "sops"
QUOTES_DIR = "quotations"


@dataclass
class Paths:
    """Where everything lives, derived from one folder the customer chooses."""
    root: str

    @property
    def inquiries(self) -> str:
        return os.path.join(self.root, INQUIRIES_DIR)

    @property
    def register_csv(self) -> str:
        return os.path.join(self.root, register.FILENAME)

    @property
    def worklist_dir(self) -> str:
        """One JSON file per section — see core/worklist.py."""
        return os.path.join(self.root, worklist.DIRNAME)

    @property
    def worklist_json(self) -> str:
        """The single file an older Prism kept. Only worklist.migrate() has
        a reason to want it; everything else reads worklist_dir."""
        return os.path.join(self.root, worklist.LEGACY_FILENAME)

    @property
    def sops(self) -> str:
        return os.path.join(self.root, SOPS_DIR)

    @property
    def sop_log(self) -> str:
        return os.path.join(self.root, SOPS_DIR, sop.LOG_FILENAME)

    @property
    def client_sops(self) -> str:
        return os.path.join(self.root, SOPS_DIR, sop.CLIENTS_FILENAME)

    @property
    def quotations(self) -> str:
        return os.path.join(self.root, QUOTES_DIR)

    def folder_for(self, inquiry_no: str) -> str:
        """One folder per inquiry: the mail, the drawings, the quote, the PO.

        The number carries slashes (INQ/25-26/0087) and a slash in a path is a
        directory separator, so it becomes INQ-25-26-0087 on disk. Reversible
        by eye, which is what matters when somebody is looking for it.
        """
        name = (inquiry_no or "").strip().replace("/", "-").replace("\\", "-")
        # Never the inquiries folder itself: a row with no number used to
        # file its drawings loose among every other inquiry's folders.
        return os.path.join(self.inquiries, name or "unnumbered")

    def ensure(self) -> None:
        for folder in (self.inquiries, self.sops, self.quotations):
            os.makedirs(folder, exist_ok=True)


_PRISM_FILE = re.compile(r"^[A-Z]+-\d{2}-\d{2}-\d+\.(csv|pdf|xlsx|txt)$", re.I)


def numbers_in_use(paths: Paths) -> list[str]:
    """Every inquiry and quotation number already given out, from outside
    the register: the inquiry folders, the quotation files Prism saved in
    them, and the subjects of the mails it sent. register.next_number() and
    quoting.next_quote_number() take this as a floor, so a row that drops
    out of the CSV never gets its number reissued. Never raises — an
    unreadable folder only means a lower floor."""
    found: list[str] = []
    for folder in (paths.inquiries, paths.quotations):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            found.append(entry.name)
            if folder != paths.inquiries:
                continue
            try:
                found += [f.name for f in os.scandir(entry.path)
                          if _PRISM_FILE.match(f.name)]
            except OSError:
                pass
    try:
        found += [r.get("subject", "") or ""
                  for r in worklist._read_kind(paths.root, "sent")]
    except Exception:                                   # noqa: BLE001
        pass
    return found


def _sent_subjects(paths: Paths) -> dict:
    """{inquiry no: [subjects of the mails Prism sent on it]} from the sent
    log — a "Re:" of one of these is a reply to us. Never raises."""
    out: dict = {}
    try:
        for r in worklist._read_kind(paths.root, "sent"):
            number = (r.get("inquiry_no") or "").strip().upper()
            if number and r.get("subject"):
                out.setdefault(number, []).append(r["subject"])
    except Exception:                                   # noqa: BLE001
        pass
    return out


# ── pulling the details out of an inquiry ────────────────────────────────────

_DETAILS_PROMPT = """Read this email from a customer and return ONLY a JSON
object. No explanation, no markdown fences.

Use exactly these keys:
{
  "customer": "the company name, if stated",
  "contact": "the person's name, if stated",
  "phone": "phone number, if stated",
  "product": "what they want, in under 15 words, using their own words",
  "quantity": "the quantity with its unit, e.g. 5000 nos — empty if not stated",
  "notes": "anything a salesperson would need to know, in one short line"
}

Rules:
- Use empty strings for anything not stated. Never invent a company name, a
  quantity or a phone number.
- Do not summarise the whole email — just fill the fields.
- Copy specifications exactly as written, including numbers and units.

EMAIL:
"""


def _json_from(text: str) -> dict:
    raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", (text or "").strip(),
                 flags=re.I | re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_details(message: Message, api_key: str = "", model: str = "") -> dict:
    """Customer, contact, product, quantity — as far as the mail states them.

    Everything here is optional. A failed call returns {} and the register row
    is still created from the sender and the subject, because an inquiry
    recorded imperfectly beats one that exists only in the inbox.
    """
    if not api_key:
        return {}
    from .router import groq_chat
    try:
        reply = groq_chat(api_key, model or "",
                          _DETAILS_PROMPT + message.snippet(3000),
                          temperature=0.0, timeout=45)
    except Exception:
        return {}
    data = _json_from(reply)
    return {k: str(v or "").strip() for k, v in data.items() if isinstance(k, str)}


# ── what a reply means ────────────────────────────────────────────────────────

ACCEPTED = "accepted"
REJECTED = "rejected"
NEGOTIATING = "negotiating"
NEEDS_INFO = "needs_info"
UNCLEAR = "unclear"

_INTENT_PROMPT = """A supplier sent a customer a quotation. Here is the
customer's reply. What is the customer doing?

Answer with ONE word, nothing else:
  accepted     - they are confirming the order or asking to proceed
  rejected     - they are declining, or have bought elsewhere
  negotiating  - they want a better rate, or different terms
  needs_info   - they are asking a question before deciding
  unclear      - it is genuinely not any of the above

REPLY:
"""


# ── reading a reply without asking anybody ───────────────────────────────────
# Replies to a quotation are close to formulaic. "We confirm the order",
# "kindly send your best price", "we have finalised with another party" —
# three sentences that turn up in some form on most replies a factory gets,
# and none of them need a language model.
#
# Every one of these settled locally is one fewer AI call, one fewer message
# leaving the computer, and one fewer thing to go wrong at 9am. The ambiguous
# remainder still goes to the model, which is where it belongs.
#
# Order matters and is not alphabetical: a reply can contain the words of two
# of these at once ("your rate is high, but we confirm the order"), and the
# first rule to match wins.

_QUOTED_LINE = re.compile(r"^\s*(>|on .{5,80}wrote:|from:\s)", re.I)

_LOCAL_INTENT = [
    # Acceptance first. It is the only one where acting on a wrong reading
    # costs the owner an order rather than a follow-up.
    (ACCEPTED, re.compile(
        r"\b(we\s+confirm|confirming\s+the\s+order|order\s+confirmed|"
        r"please\s+proceed|kindly\s+proceed|you\s+may\s+proceed|"
        r"go\s+ahead|approved\s+for\s+production|"
        r"release[sd]?\s+the\s+order|placing\s+the\s+order|"
        r"our\s+(purchase\s+order|po)\s+(is\s+)?attach|"
        r"attached\s+(is\s+)?(our\s+)?(purchase\s+order|po)\b)", re.I)),
    # A clean refusal. Deliberately narrow: it must name the OUTCOME, not just
    # an objection. "Your rate is too high" is a complaint about price and
    # belongs below, not here.
    (REJECTED, re.compile(
        r"\b(not\s+interested|no\s+longer\s+(interested|required)|"
        r"we\s+regret|regret\s+to\s+inform|"
        r"(finali[sz]ed|placed|ordered|gone|going)\s+(the\s+order\s+)?"
        r"(with|to|elsewhere|another)|"
        r"another\s+(supplier|vendor|party)|"
        r"(project|requirement|enquiry|inquiry)\s+(is\s+)?"
        r"(dropped|cancelled|canceled|on\s+hold|shelved)|"
        r"drop\s+this\s+(enquiry|inquiry))", re.I)),
    # Haggling. The commonest reply there is, and the most expensive to
    # misread as a refusal — the deal is still very much alive.
    (NEGOTIATING, re.compile(
        r"\b(best\s+(and\s+final\s+)?(price|rate|offer)|last\s+price|"
        r"final\s+rate|(rate|price)s?\s+(is|are|seems?|looks?)\s+"
        r"(too\s+)?(high|steep|much)|"
        r"(reduce|revise|reconsider|relook|work\s+out)\s+"
        r"(the\s+|your\s+)?(rate|price|quote|quotation)|"
        r"(any|some)\s+(discount|concession|rebate)|"
        r"more\s+competitive|match\s+(the|this|their)\s+(rate|price)|"
        r"budget\s+is)", re.I)),
    # A question before deciding.
    (NEEDS_INFO, re.compile(
        r"(\?)|"
        r"\b(what\s+is\s+the|when\s+can\s+you|how\s+(soon|long|many)|"
        r"kindly\s+(confirm|clarify|advise)|please\s+(confirm|clarify|advise)|"
        r"lead\s+time|delivery\s+(time|schedule|period)|"
        r"send\s+(us\s+)?(the\s+)?(drawing|sample|datasheet|catalogue))", re.I)),
]


def _own_words(message: Message, limit: int = 1500) -> str:
    """The part of a reply the customer actually typed.

    A reply carries our own quotation underneath it, and our covering letter
    says things like "please let us know if you need anything clarified" —
    which is a question, and would have every reply in the world read as
    NEEDS_INFO. Everything from the first quoted line down is dropped.
    """
    kept = []
    for line in (getattr(message, "body", "") or "").splitlines():
        if _QUOTED_LINE.match(line):
            break
        kept.append(line)
        if sum(len(k) for k in kept) > limit:
            break
    text = "\n".join(kept).strip()
    # A top-posted reply with no separator we recognise still gives us the
    # first paragraph, which is where the answer lives.
    return text[:limit] if text else (getattr(message, "body", "") or "")[:limit]


def local_intent(message: Message) -> str:
    """What the reply plainly says, or "" when it is not plain.

    Returning "" is a real answer and the common one for anything subtle. It
    means "ask somebody cleverer", not "no idea".
    """
    text = _own_words(message)
    if not text.strip():
        return ""
    for intent, pattern in _LOCAL_INTENT:
        if pattern.search(text):
            return intent
    return ""


def reply_intent(message: Message, api_key: str = "", model: str = "") -> str:
    """What the customer's reply means. UNCLEAR when it cannot be told.

    "Please send your best price" is negotiating, not acceptance, and the cost
    of getting that wrong is a register that lies. So the honest answer is
    allowed and is the default: an UNCLEAR reply is shown to the owner rather
    than acted on.

    Local rules first, always — including when a key is configured. Most
    replies say what they mean in words we can match, and a reply settled here
    never leaves the computer.
    """
    plain = local_intent(message)
    if plain:
        return plain
    if not api_key:
        return UNCLEAR
    from .router import groq_chat
    try:
        reply = groq_chat(api_key, model or triage.FAST_MODEL,
                          _INTENT_PROMPT + message.snippet(1500),
                          temperature=0.0, timeout=30)
    except Exception:
        return UNCLEAR
    word = (reply or "").strip().lower()
    for candidate in (ACCEPTED, REJECTED, NEGOTIATING, NEEDS_INFO):
        if candidate in word:
            return candidate
    return UNCLEAR


# ── the worklist ──────────────────────────────────────────────────────────────

@dataclass
class Item:
    """One thing that came out of a check and may need a person."""
    kind: str                      # "inquiry" · "reply" · "order" · "sop"
    message: Message | None = None
    row: dict | None = None        # its register row, where it has one
    folder: str = ""
    files: list[str] = field(default_factory=list)
    intent: str = ""
    note: str = ""

    @property
    def inquiry_no(self) -> str:
        return (self.row or {}).get("Inquiry no", "")


@dataclass
class Result:
    counts: dict = field(default_factory=dict)
    # Every message with what it was sorted as, in arrival order — including
    # the ones nothing happens to. The counts alone are a summary; a customer
    # deciding whether to trust the sorting needs to see the newsletter sitting
    # in the promotions column with "carries an unsubscribe link" next to it.
    sorted_mail: list = field(default_factory=list)   # [(Message, Verdict)]
    new_inquiries: list[Item] = field(default_factory=list)
    replies: list[Item] = field(default_factory=list)
    orders: list[Item] = field(default_factory=list)
    followups: list[dict] = field(default_factory=list)
    sops: list = field(default_factory=list)
    state: State = field(default_factory=State)
    knowledge: triage.Knowledge = field(default_factory=triage.Knowledge)
    error: str = ""
    fetched: int = 0

    @property
    def needs_attention(self) -> int:
        return len(self.new_inquiries) + len(self.replies) + len(self.orders)

    def headline(self) -> str:
        """The one line shown after a check, in the owner's words."""
        if self.error:
            return self.error
        if not self.fetched:
            return "No new mail."
        parts = [triage.describe(self.counts)]
        if self.followups:
            parts.append(f"{len(self.followups)} quotation(s) due a reminder")
        if self.sops:
            parts.append(f"{len(self.sops)} SOP(s) due")
        return " · ".join(parts)


# classify()'s ORDER category is deliberately wide — CATEGORIES[ORDER] reads
# "a purchase order, an order confirmation, or a customer saying they are
# placing the order" — because a genuinely new correspondent's very first
# mail saying "please supply 500 units" really is an order with nothing
# else to call it. That same wide net catches a bare "Done deal" or
# "please proceed" on a thread that already has an open, quoted inquiry —
# where the far better reading is an ACCEPTED reply (reply_intent() already
# knows "please proceed" and "go ahead" mean exactly that, see
# _LOCAL_INTENT above), not a purchase order Prism cannot read a single
# field out of. This only narrows the ORDER path for messages already tied
# to an existing inquiry — a brand-new correspondent's first mail is
# unaffected, and still needs nothing more than the wide category to be
# treated as an order.
_PO_LIKE = re.compile(
    r"\bpurchase\s*order\b|\bp\.?\s*o\.?\s*(no\.?|number|#)\s*[:\-]?\s*\w",
    re.I)


def _read_pictures(files: list, folder: str) -> str:
    """OCR every picture among the files just saved, append what it says to
    the inquiry folder's image_text.txt (a reply's picture adds to the
    first mail's), and return this batch's text. Never raises: a picture
    that cannot be read is an inquiry with less in it, not a failed check."""
    try:
        text = ocr.read_files(files)
    except Exception:                                   # noqa: BLE001
        return ""
    if not text:
        return ""
    earlier = ocr.recall(folder)
    ocr.remember(folder, (earlier + "\n\n" + text).strip() if earlier else text)
    return text


def _picture_summary(text: str, limit: int = 140) -> str:
    """The picture's words as a "Product asked" line: the lines of text,
    file names dropped, joined with spaces, marked as read from a picture
    so nobody mistakes it for something the customer typed."""
    words = " ".join(ln.strip() for ln in text.splitlines()
                     if ln.strip() and not ln.startswith("["))
    words = words[:limit].rstrip()
    return f"{words} (read from the picture)" if words else ""


def _refresh_unquoted_row(row: dict, message: Message, files: list,
                          picture_text: str, api_key: str, model: str) -> None:
    """A customer who writes back on an inquiry we have not quoted yet is
    usually saying what they actually want ("I want this product, 10
    pieces", with a picture). Without this the row keeps its first, vague
    wording ("more information") and nothing in "To quote" shows the new
    ask. Only fills in what the reply states; never blanks a field."""
    body = (message.body or "").strip()
    if not body and not files:
        return
    details = extract_details(message, api_key, model) if body else {}
    product = details.get("product") or (
        register.product_summary(message) if body else "")
    if picture_text and (not product or product.lower().startswith("re:")):
        product = _picture_summary(picture_text)
    if product:
        row["Product asked"] = product
    if details.get("quantity"):
        row["Quantity"] = details["quantity"]
    elif body and not row.get("Quantity"):
        found = re.search(r"(\d[\d,]*)\s*(?:pcs?|pieces?|nos?|units?)\b", body, re.I)
        if found:
            row["Quantity"] = found.group(0)
    if files:
        old = [n for n in (row.get("Drawing") or "").split(", ") if n and n != "No"]
        row["Drawing"] = ", ".join(old + [os.path.basename(f) for f in files
                                          if os.path.basename(f) not in old])
    when = date.today().strftime("%d-%m-%Y")
    note = f"Wrote again {when}: {' '.join(body.split())[:120]}" if body else f"Sent a file {when}"
    row["Notes"] = (f"{row['Notes']} | {note}" if row.get("Notes") else note)


def _save_mail(folder: str, message: Message, row: dict) -> str:
    """Keep the mail itself in the inquiry's folder as an .eml — the file
    Outlook, Apple Mail and Thunderbird open as the original, pictures and
    all. history.txt keeps the readable text; this keeps the evidence.
    Never fails the check: returns "" when there is nothing to write."""
    raw = getattr(message, "raw", b"") or b""
    if not raw or not folder:
        return ""
    when = message.date.strftime("%Y-%m-%d_%H%M") if message.date else "undated"
    stem = f"{_file_prefix(row) or 'mail'}_mail_{when}"
    # Written once, then let go: the decoded attachments are already held,
    # and a second copy of a 20 MB drawing per mail adds up on a laptop.
    message.raw = b""
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{stem}.eml")
        for n in range(2, 100):
            if not os.path.exists(path):
                break
            path = os.path.join(folder, f"{stem}-{n}.eml")
        with open(path, "wb") as f:
            f.write(raw)
        return path
    except OSError:
        return ""


def _file_prefix(row: dict) -> str:
    """INQ-26-27-0005 — what a saved attachment's name starts with."""
    return (row.get("Inquiry no") or "").strip().replace("/", "-")


def _reads_as_an_order(message: Message) -> bool:
    """An attachment, or text that actually names a PO — not just any
    confirmation-shaped sentence."""
    if message.attachments:
        return True
    return bool(_PO_LIKE.search(getattr(message, "body", "") or ""))


def _log_history(folder: str, event: str, message: Message) -> None:
    """One line into history.txt for a mail that arrived — see core/history.py.
    Never lets a logging failure stop the check; the register row and the
    saved attachments are the record that matters if this fails."""
    who = message.from_name or message.from_addr
    if message.from_name and message.from_addr:
        who = f"{message.from_name} <{message.from_addr}>"
    try:
        history.append(folder, event, who=who, subject=message.subject,
                       body=message.body, when=message.date)
    except Exception:                                   # noqa: BLE001
        pass


def check(cfg: dict, paths: Paths, *, state: State | None = None,
          knowledge: triage.Knowledge | None = None,
          model: str = "", local_only: bool = False,
          followup_days: int = 2, max_reminders: int = 3,
          today: date | None = None) -> Result:
    """One run of the whole loop. Never raises, never sends.

    Order of work is deliberate: fetch, sort, then act only on what sorting
    called actionable. Everything else is counted and left alone, so a busy
    inbox costs one AI call for the handful of genuinely new correspondents
    rather than one per message.
    """
    from . import checklog
    checklog.line("── Check my mail — starting ──")
    t0 = __import__("time").monotonic()
    today = today or date.today()
    api_key = (cfg or {}).get("api_key", "")
    # local_only has to mean every AI call on message content, not just the
    # sorting one. It was passed to triage alone at first, which left the
    # detail extraction and the reply reading still sending customer
    # correspondence out — a privacy switch that quietly did two thirds of
    # what its name promises is worse than not offering one.
    if local_only:
        api_key = ""
    knowledge = knowledge or triage.Knowledge()
    out = Result(state=state or State(), knowledge=knowledge)

    paths.ensure()
    messages, new_state, error = inbox.fetch_new(cfg, state)
    out.state = new_state
    if error:
        checklog.line(f"stopped: {error}")
        out.error = error
        return out
    out.fetched = len(messages)

    # cfg["model"] is where a retired default gets replaced once a fallback
    # answers — see router._remember_model. Leaving this at classify()'s own
    # hardcoded default meant every check restarted from the dead model
    # regardless of what had already been learned and saved to disk.
    verdicts = triage.classify(messages, api_key, knowledge=knowledge,
                               model=cfg.get("model") or triage.FAST_MODEL,
                               local_only=local_only)
    out.counts = triage.summarise(verdicts)
    out.sorted_mail = list(zip(messages, verdicts))

    try:
        rows = register.load(paths.register_csv)
    except register.RegisterLocked as e:
        out.error = str(e)
        return out
    # The rows as read. The owner keeps working on screen while this runs
    # (deleting rows, sending quotations), so the save at the end applies
    # only what this check changed onto the file as it is by then.
    as_read = register.snapshot(rows)

    dirty = False
    taken = None          # numbers_in_use(), read once and only when needed
    sent = None           # subjects of mails we sent, per inquiry, for matching
    for message, verdict in zip(messages, verdicts):
        if not verdict.actionable:
            # Its .eml is never written; holding every newsletter's raw
            # MIME (attachments and all) for the rest of the check is what
            # doubled the memory a large inbox took.
            message.raw = b""
            continue

        sent = sent if sent is not None else _sent_subjects(paths)
        existing, how = register.match(rows, message, sent)

        # A reply on an inquiry we already know about is never a new inquiry,
        # whatever the sorter called it — otherwise a three-message
        # negotiation becomes three rows and the register stops being true.
        if existing is not None:
            register.add_thread(existing, message)
            existing["Last contact"] = today.strftime("%d-%m-%Y")
            if not (existing.get("Inquiry no") or "").strip():
                # A hand-kept row with no number of its own: give it one now,
                # or its files would have nowhere of their own to go.
                taken = taken if taken is not None else numbers_in_use(paths)
                existing["Inquiry no"] = register.next_number(
                    rows, "INQ", message.date or today, taken)
            folder = existing.get("Folder") or paths.folder_for(
                existing.get("Inquiry no", ""))
            files = inbox.save_attachments(message, folder,
                                           _file_prefix(existing))
            _save_mail(folder, message, existing)
            picture_text = _read_pictures(files, folder)
            # Only a mail that is certainly on this inquiry (its thread, or
            # it names the number) may rewrite what the row says was asked.
            # A subject match is likely, not certain, and a wrong refresh
            # erases the first enquiry from "To quote".
            if ((existing.get("Status") or register.NEW) == register.NEW
                    and how in (register.BY_THREAD, register.BY_NUMBER)):
                _refresh_unquoted_row(existing, message, files, picture_text,
                                      api_key, model)
            is_order = (verdict.category == triage.ORDER
                       and _reads_as_an_order(message))
            item = Item("order" if is_order else "reply",
                        message, existing, folder, files)
            if is_order:
                item.note = "a purchase order may be attached"
                out.orders.append(item)
                _log_history(folder, "Purchase order received", message)
            else:
                item.intent = reply_intent(message, api_key, model)
                out.replies.append(item)
                _log_history(folder, "Reply received", message)
            dirty = True
            continue

        # Genuinely new. An ORDER from somebody with no inquiry on file is
        # still an order — it gets a row so it can be tracked, marked as
        # having skipped the quotation stage.
        details = extract_details(message, api_key, model)
        taken = taken if taken is not None else numbers_in_use(paths)
        row = register.from_message(message, details, rows=rows, taken=taken)
        folder = paths.folder_for(row["Inquiry no"])
        row["Folder"] = folder
        # Made even when nothing was attached. The register points at this
        # folder from the moment the row exists, and the quotation and the PO
        # land in it later — a path in the file that opens onto nothing is the
        # kind of small broken thing that makes people stop trusting the rest.
        os.makedirs(folder, exist_ok=True)
        files = inbox.save_attachments(message, folder, _file_prefix(row))
        _save_mail(folder, message, row)
        if files:
            row["Drawing"] = ", ".join(os.path.basename(f) for f in files)
        # A customer who sends a picture instead of words: read the text in
        # it (locally -- the picture never leaves the machine), keep it
        # beside the picture, and let it stand in for "Product asked" when
        # the mail itself said nothing. The quotation window and the
        # code finder read the same file.
        picture_text = _read_pictures(files, folder)
        if picture_text and not (message.body or "").strip():
            # No words in the mail at all: the picture IS the inquiry, and
            # a "Product asked" of just the subject line ("Enquiry") would
            # say nothing. Typed words are never overwritten.
            row["Product asked"] = _picture_summary(picture_text) or row["Product asked"]
        rows.append(row)
        dirty = True

        item = Item("order" if verdict.category == triage.ORDER else "inquiry",
                    message, row, folder, files)
        if verdict.category == triage.ORDER:
            item.note = "ordered without a quotation from us"
            out.orders.append(item)
            _log_history(folder, "Order received (no prior quotation)", message)
        else:
            out.new_inquiries.append(item)
            _log_history(folder, "Enquiry received", message)

    if dirty:
        try:
            rows = register.save_changes(rows, paths.register_csv, as_read)
        except register.RegisterLocked as e:
            # The work is done and in memory; only the write failed. Say so —
            # the owner can close Excel and press check again, and nothing is
            # lost because unsaved rows have no bookmark advance behind them.
            out.error = str(e)
            out.state = state or State()
            return out

    out.followups = register.awaiting_followup(rows, after_days=followup_days,
                                               max_reminders=max_reminders,
                                               today=today)
    out.sops = _sops_due(paths, today)
    checklog.line(f"── done in {__import__('time').monotonic() - t0:.1f}s — "
                  f"{out.fetched} fetched, "
                  f"{len(out.new_inquiries)} new inquiry(ies), "
                  f"{len(out.orders)} order(s), {len(out.replies)} reply(ies) ──")
    return out


def _sops_due(paths: Paths, today: date) -> list:
    """Documents due to go out. Never fails the whole check.

    An unreadable client map is a setup problem, not a reason to stop sorting
    somebody's mail — so it is reported through the log and the run carries on.
    """
    try:
        library = sop.load_library(paths.sops)
        if not library:
            return []
        rules = sop.load_client_map(paths.client_sops)
        if not rules:
            return []
        return sop.pending(rules, library, sop.load_log(paths.sop_log), today=today)
    except Exception as e:
        ui.warn(f"Couldn't check which SOPs are due: {e}")
        return []


# ── the end-of-day note ───────────────────────────────────────────────────────

def day_summary(paths: Paths, *, today: date | None = None) -> str:
    """The fifteen-second read at 6 p.m.

    Deliberately ends on what is still open. The count of quotations waiting on
    a reply is the money already earned and not yet collected on, and it is the
    number nobody in a small factory knows today.
    """
    today = today or date.today()
    try:
        rows = register.load(paths.register_csv)
    except register.RegisterLocked as e:
        return str(e)
    month_start = today.replace(day=1)
    stats = register.summarise(rows, since=month_start, until=today)

    from .quoting import indian_currency
    lines = [f"This month ({month_start.strftime('%B %Y')}):",
             f"  Inquiries received : {stats.received}",
             f"  Quotations sent    : {stats.quoted}  "
             f"— ₹{indian_currency(stats.quoted_value)}",
             f"  Orders won         : {stats.converted}  "
             f"— ₹{indian_currency(stats.converted_value)}",
             f"  Conversion         : {stats.conversion}%",
             f"  Waiting on a reply : {stats.waiting}"]
    if stats.reasons:
        worst = sorted(stats.reasons.items(), key=lambda kv: -kv[1])
        lines.append("  Lost because       : " +
                     ", ".join(f"{reason} ({count})" for reason, count in worst))
    return "\n".join(lines)
