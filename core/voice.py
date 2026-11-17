"""
Prism — voice input (push-to-talk bridge)
─────────────────────────────────────────
Lets the REPL prompt take speech: SPACE starts a take, SPACE again stops it,
the audio goes to Groq Whisper (whisper-large-v3, multilingual — language is
auto-detected per take) and the transcript drops into the REPL exactly as if
it had been typed.

Deliberately simple: no VAD, no async — record while toggled, one WAV upload.
Needs only `pyaudio` (+ the `requests` Prism already uses). If pyaudio is
missing or stdin isn't a real terminal, `available()` is False and the REPL
falls back to typed input only.
"""
from __future__ import annotations
import io
import os
import sys
import time
import wave
import requests

from . import ai_client as _ai

# Speech-to-text has no route on Prism's AI server yet (it is not a chat
# completion), so with AI running through the server there is no key to send.
VOICE_UNAVAILABLE = ("Voice dictation isn't part of your Prism plan yet, so it "
                     "can't be used right now. Type your request instead.")

TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_MODEL = "whisper-large-v3"   # OpenAI's open-source Whisper, hosted by Groq
SAMPLE_RATE = 16000
CHUNK = 1024
MAX_TAKE_S = 300     # safety cap for a forgotten toggle


def available() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        import pyaudio  # noqa: F401
        return True
    except Exception:
        return False


class RawKeys:
    """Single-keypress terminal reads, cross-platform (termios / msvcrt)."""

    def __enter__(self):
        if os.name != "nt":
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)   # keeps Ctrl-C working
        return self

    def __exit__(self, *exc):
        if os.name != "nt":
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def poll(self) -> str | None:
        if os.name == "nt":
            import msvcrt
            if msvcrt.kbhit():
                try:
                    return msvcrt.getch().decode(errors="ignore")
                except Exception:
                    return None
            return None
        import select
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if readable:
            # os.read, NEVER sys.stdin.read(1). The buffered text wrapper
            # slurps everything available — a whole pasted command — into
            # its own internal buffer just to hand back one character. The
            # rest then haunts the session from inside Python, invisible to
            # every select()-based drain, and a stray 'n' from a paste's
            # second line answered a Y/n prompt on two real runs. A raw
            # one-byte read takes the keypress and leaves the terminal's
            # buffer exactly as it was.
            try:
                return os.read(self.fd, 1).decode(errors="ignore")
            except OSError:
                return None
        return None

    def wait(self) -> str:
        while True:
            ch = self.poll()
            if ch:
                return ch
            time.sleep(0.02)


def choose(hint: str) -> str:
    """Show a one-line hint, return the single key the user presses."""
    sys.stdout.write(hint)
    sys.stdout.flush()
    try:
        with RawKeys() as keys:
            return keys.wait()
    finally:
        sys.stdout.write("\r" + " " * len(hint) + "\r")
        sys.stdout.flush()


import contextlib


@contextlib.contextmanager
def _silence_alsa():
    """Silence ALSA / JACK library C-level stderr noise on Linux during device scan."""
    if os.name == "nt":
        yield
        return
    try:
        from ctypes import CFUNCTYPE, c_char_p, c_int, cdll
        c_error_handler = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)(lambda *_: None)
        asound = cdll.LoadLibrary("libasound.so.2")
        asound.snd_lib_error_set_handler(c_error_handler)
    except Exception:
        pass
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        saved_stderr = os.dup(2)
        os.dup2(devnull, 2)
    except Exception:
        saved_stderr = None
    try:
        yield
    finally:
        if saved_stderr is not None:
            try:
                os.dup2(saved_stderr, 2)
                os.close(saved_stderr)
                os.close(devnull)
            except Exception:
                pass


def record_until(should_stop) -> bytes:
    """Record the mic until `should_stop()` returns True. Returns a WAV byte
    string. Pulled out of record_and_transcribe() so any front-end (CLI
    keypress, GUI button, wake-word loop) can supply its own stop condition
    without re-implementing the actual PyAudio capture."""
    import pyaudio

    with _silence_alsa():
        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                         input=True, frames_per_buffer=CHUNK)
    frames: list[bytes] = []
    start = time.time()
    try:
        while True:
            frames.append(stream.read(CHUNK, exception_on_overflow=False))
            if should_stop() or (time.time() - start) > MAX_TAKE_S:
                break
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)   # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


def transcribe(wav_bytes: bytes, cfg: dict) -> tuple[str, str]:
    """Send a WAV recording to Groq Whisper. Returns (text, detected_language)."""
    if _ai.is_server_key(cfg.get("api_key")):
        raise RuntimeError(VOICE_UNAVAILABLE)
    resp = requests.post(
        TRANSCRIBE_URL,
        headers={"Authorization": f"Bearer {cfg.get('api_key', '')}"},
        files={"file": ("take.wav", wav_bytes, "audio/wav")},
        data={"model": WHISPER_MODEL, "response_format": "verbose_json"},
        timeout=60,
    )
    rj = resp.json()
    if "text" not in rj:
        raise RuntimeError(f"transcription failed: {str(rj)[:200]}")
    return (rj.get("text") or "").strip(), (rj.get("language") or "")


def record_and_transcribe(cfg: dict, stop_key: str = " ") -> tuple[str, str]:
    """CLI entry point: record the mic until stop_key is pressed again (raw
    terminal keypress), then transcribe via Groq Whisper."""
    with RawKeys() as keys:
        wav_bytes = record_until(lambda: keys.poll() == stop_key)
    return transcribe(wav_bytes, cfg)


def interpret(raw: str, cfg: dict) -> dict:
    """The Wispr-Flow-style layer: polish the raw transcript and split it into
    file references + the actual task, however casually it was phrased.
    Returns {"cleaned": str, "files": [descriptions], "task": str}.
    Fail-soft: on any error the raw text comes back as the task, no files."""
    import json
    # ok=False tells the caller this is the degraded path — file references
    # in the speech were NOT extracted, and the UI must say so.
    fallback = {"cleaned": raw, "files": [], "task": raw, "ok": False}
    api_key = cfg.get("api_key")
    if not api_key:
        return fallback

    prompt = f"""You are the dictation brain of a terminal AI assistant (think Wispr Flow).
The user SPOKE the transcript below. Speech-to-text is messy: fillers, repeated
words, mis-hearings, spoken punctuation ("dot PDF" means ".pdf"). The word "Prism"
(possibly mis-heard as prism/prisim/prison) is the assistant's OWN name — strip it
ONLY when it is a standalone wake-word at the very start of the utterance
addressing the assistant (e.g. "Prism, do this…" → "do this…"). NEVER remove
"Prism"/"prism" anywhere else — it is also this project's OWN name, so phrases
like "Prism Terminal", "Prism AI Flow", "the prism folder", "using Prism" are
real content (folder names, product names, tool names) and MUST be kept intact.

CRITICAL RULE — you are a TRANSCRIPT CLEANER, not a summarizer. Never shorten,
generalize, or paraphrase away detail. Every distinct requirement, example,
audience, platform, or use-case the user names must survive into "task"
word-for-word or as close to it as grammar allows. If the transcript is long
and rambling, "task" should ALSO be long — cutting it down to one tidy
sentence is a FAILURE even if that sentence sounds more professional. Do not
invent anything that is not in the transcript.

Return ONLY JSON:
{{"cleaned": "...", "files": ["..."], "task": "..."}}

- "cleaned": the FULL utterance as polished text — only fillers ("uh", "um"),
  exact word repetitions, and stutters dropped; grammar lightly fixed; spoken
  punctuation converted ("delta prototype dot pdf" → "delta prototype.pdf");
  assistant-name prefix removed. Same length and same level of detail as the
  transcript, same language as spoken. If the speaker corrected themself
  mid-sentence ("delta prototype... delta working prototype"), keep only the
  correction — that is the ONLY case where content is dropped, not shortened.
- "files": one entry per file OR FOLDER the user wants fetched/attached/read
  from — trigger on ANY phrasing that names a location to pull content FROM,
  not just ones that literally say the word "folder": "take/get/grab/extract
  the content from X", "X is on the desktop", "inside X inside Y inside Z"
  (a nested nested-folder nested chain spoken with prepositions instead of
  slashes), "from my documents", etc. Build ONE entry with the full location
  chain exactly as spoken, e.g. "prism terminal inside prism ai flow inside
  python program inside documents" or "delta working prototype.pdf on the
  desktop". When no filename is said, it's a folder — that's fine, still
  emit the location chain as one entry. Only skip this if NO location or
  file/folder was mentioned at all. Empty list if none.
- "task": "cleaned" with ONLY the file-fetching sentence(s) removed — nothing
  else cut, reworded into a summary, or made generic. Keep every specific
  requirement/example/platform/audience the user listed, in their own words.
  Empty string if they only asked for files.

Transcript: {raw}"""
    try:
        if _ai.active():
            text = _ai.chat("classify", prompt, addon="core", feature="dictation",
                            temperature=0, timeout=45)
        else:
            resp = requests.post(
                CHAT_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": cfg.get("model", "llama-3.3-70b-versatile"),
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0},
                timeout=45,
            )
            rj = resp.json()
            text = rj["choices"][0]["message"]["content"]
        s, e = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[s:e])
        return {
            "cleaned": (data.get("cleaned") or raw).strip(),
            "files": [f for f in (data.get("files") or []) if f and isinstance(f, str)],
            "task": (data.get("task") or "").strip(),
            "ok": True,
        }
    except Exception:
        return fallback
