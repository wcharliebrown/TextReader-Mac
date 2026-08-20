"""Round-trip diagnostics: speak the text, listen to it, diff what came back.

The normalization rules in textnorm.py exist because the G2P sometimes says
something other than what the text means. The only way to find the next such
rule is to listen - so this tool automates listening. It sends text through the
running server exactly as the extension would, transcribes each chunk's audio
with Whisper, and aligns the transcript against the text the engine was asked
to say. A mismatch is a place worth a new rule; a clean pass is a regression
test for the rules already there.

    .venv/bin/python -m server.diagnose article.txt
    pbpaste | .venv/bin/python -m server.diagnose -

Honest limits: speech-to-text is a language model too, and it silently corrects
the very mistakes we hunt - audio saying "reed" in a past-tense sentence still
comes back spelled "read". The round trip catches dropped words, garbled
respellings, letter-spaced acronyms and mangled numbers; it cannot hear a wrong
vowel that spells the same word. Vowel-level checks live in
tests/test_pronunciation.py, which asserts phonemes instead of audio.
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import re
import sys
import unicodedata
import urllib.request
import wave

from .textnorm import expand_text

DEFAULT_SERVER = "http://127.0.0.1:8842"
DEFAULT_STT = "mlx-community/whisper-large-v3-turbo"

# ---------------------------------------------------------------------------
# comparison (pure - unit tested)
# ---------------------------------------------------------------------------

# Whisper and textnorm legitimately disagree on these spellings; treating them
# as equal keeps the report about pronunciation, not orthography.
_SPELLING = {
    "ok": "okay", "mr": "mister", "mrs": "missus", "dr": "doctor",
    "st": "saint", "vs": "versus", "oclock": "o'clock",
    "am": "a m", "pm": "p m",
    "kilometres": "kilometers", "centimetres": "centimeters",
    "millimetres": "millimeters", "nanometres": "nanometers", "metres": "meters",
}

# Pairs that sound identical, so the transcript's spelling proves nothing.
# Most are our own respellings coming back in dictionary form - which is the
# round trip *working*, not failing.
_SOUNDS_SAME = frozenset(
    frozenset(p)
    for p in [
        ("led", "lead"), ("red", "read"), ("reed", "read"), ("base", "bass"),
        ("jason", "json"), ("yammel", "yaml"), ("won", "one"), ("to", "two"),
        ("to", "too"), ("their", "there"), ("its", "it's"),
    ]
)

# Whisper sometimes writes a spoken time British-style ("3.30 PM"); turn the
# dot back into a colon so expand_text reads it as the time it was.
_DOTTED_TIME = re.compile(r"\b(\d{1,2})\.([0-5]\d)\s?(?=[APap]\.?[Mm]\b)")

# The transcript writes money compactly ("$4.5 million"); putting the amount
# back in front of the magnitude lets expand_text below agree with textnorm's
# own ordering ("four point five million dollars").
_MONEY_MAG = re.compile(
    r"([$£€¥₹])\s?([\d.,]+)\s+(thousand|million|billion|trillion)",
    re.I,
)

_DROP = re.compile(r"[^\w\s']")
_APOS = re.compile(r"(?<!\w)'|'(?!\w)")


def canon_words(text: str) -> list[str]:
    """Reduce text to the word sequence a listener would report.

    Both sides of the diff pass through here, and both get expand_text: the
    transcript often writes digits back ("3:30", "$4.5 million") for words it
    heard, and expanding them again makes the two spellings meet in the middle.
    """
    from .textnorm import _CURRENCY_WORDS  # symbol -> plural word, one source

    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _DOTTED_TIME.sub(r"\1:\2 ", text)
    text = _MONEY_MAG.sub(lambda m: f"{m.group(2)} {m.group(3)} {_CURRENCY_WORDS[m.group(1)]}", text)
    text = expand_text(text)
    text = text.replace("-", " ")
    text = _DROP.sub(" ", text)
    text = _APOS.sub(" ", text)
    words = [w for w in text.lower().split() if w]
    return [x for w in words for x in _SPELLING.get(w, w).split()]


def align(intended: list[str], heard: list[str]) -> list[tuple[str, str, str]]:
    """Places where the transcript diverges: (op, intended words, heard words)."""
    out = []
    ops = difflib.SequenceMatcher(a=intended, b=heard, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            continue
        want, got = intended[i1:i2], heard[j1:j2]
        # Dates are deliberately read with their slashes ("twelve slash five");
        # the transcript writes the date back as digits and the word vanishes.
        if op == "delete" and set(want) == {"slash"}:
            continue
        if len(want) == len(got) and all(
            a == b or frozenset((a, b)) in _SOUNDS_SAME for a, b in zip(want, got)
        ):
            continue
        out.append((op, " ".join(want), " ".join(got)))
    return out


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------

def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def _wav_to_float16k(data: bytes):
    """Decode the server's 16-bit mono WAV and resample to Whisper's 16 kHz."""
    import numpy as np

    with wave.open(io.BytesIO(data)) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    if rate == 16000:
        return audio
    n = int(round(len(audio) * 16000 / rate))
    return np.interp(
        np.linspace(0, len(audio) - 1, n, dtype=np.float64),
        np.arange(len(audio), dtype=np.float64),
        audio,
    ).astype(np.float32)


def diagnose(text: str, server: str, voice: str, speed: float, stt_model: str) -> int:
    import mlx_whisper

    job = _post(f"{server}/v1/speak/prepare", {"text": text, "voice": voice, "speed": speed})
    sentences = job["sentences"]
    print(f"{len(sentences)} chunks, voice {job['voice']}, transcribing with {stt_model}\n")

    mismatched = 0
    for s in sentences:
        with urllib.request.urlopen(
            f"{server}/v1/audio/{s['key']}.wav?job={job['job_id']}&idx={s['idx']}"
        ) as r:
            audio = _wav_to_float16k(r.read())
        heard = mlx_whisper.transcribe(
            audio, path_or_hf_repo=stt_model, language="en", temperature=0.0
        )["text"].strip()

        diffs = align(canon_words(s["text"]), canon_words(heard))
        if not diffs:
            print(f"chunk {s['idx']:2d}  ok      {s['text'][:70]}")
            continue
        mismatched += 1
        print(f"chunk {s['idx']:2d}  DIFFERS {s['text'][:70]}")
        for op, want, got in diffs:
            print(f"    said {want!r:35s} heard {got!r}")
    print(f"\n{mismatched} of {len(sentences)} chunks differ from their transcript")
    return 1 if mismatched else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("file", help="text file to round-trip, or - for stdin")
    p.add_argument("--server", default=DEFAULT_SERVER)
    p.add_argument("--voice", default="af_heart")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--stt-model", default=DEFAULT_STT)
    a = p.parse_args()
    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    return diagnose(text, a.server.rstrip("/"), a.voice, a.speed, a.stt_model)


if __name__ == "__main__":
    raise SystemExit(main())
