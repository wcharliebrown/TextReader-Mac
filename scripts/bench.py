"""Measure what the listener actually feels: time to first audio, then throughput.

Run against a live server:  .venv/bin/python scripts/bench.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8842"

# Twelve genuinely distinct paragraphs. Reusing one paragraph would let the
# content-addressed cache turn most chunks into hits and report a throughput
# figure that is not synthesis speed at all.
PARAGRAPHS = [
    "The quality gap between local and cloud text to speech has narrowed sharply over "
    "the past two years. A model small enough to run on a laptop can now narrate a long "
    "article with natural pacing and believable emphasis, without sending a single byte "
    "over the network.",

    "Economics drive most of the interest. Cloud narration is billed per character, so a "
    "habit of listening to three or four long reads a day turns into a recurring bill that "
    "nobody budgeted for. Running the model yourself converts that variable cost into a "
    "fixed one you have already paid.",

    "Latency matters more than raw fidelity for this use case. A listener will forgive a "
    "voice that is slightly less expressive, but will abandon a tool that makes them stare "
    "at a spinner for four seconds before the first word arrives.",

    "Chunking is the trick that makes streaming feel instant. Rather than rendering an "
    "entire article and then playing it, the text is cut at sentence boundaries and each "
    "piece is rendered while the previous one is still being spoken.",

    "Choosing where to cut is subtle. Cut too finely and the prosody sounds chopped, "
    "because the model loses the intonation contour that spans a full clause. Cut too "
    "coarsely and the first chunk takes long enough to undo the benefit.",

    "Normalisation carries more weight than most people expect. A model reading a raw web "
    "page will happily pronounce a citation marker, a currency symbol, and a bare year in "
    "ways that pull the listener straight out of the article.",

    "Abbreviations are a particular trap. The same period that ends a sentence also ends an "
    "honorific, so a naive splitter will break a paragraph in the middle of a name and "
    "leave an awkward pause where none belongs.",

    "Caching pays off quickly in practice. Readers re-listen to paragraphs, scrub backwards, "
    "and revisit articles days later, and a hash of the text and voice makes every one of "
    "those a disk read rather than a fresh render.",

    "Memory pressure stays modest throughout. The weights occupy a couple of gigabytes, the "
    "audio buffers are measured in megabytes, and nothing about the workload grows with the "
    "length of the document being read.",

    "Voice choice turns out to be deeply personal. Some listeners want a neutral newsreader "
    "for dense technical writing, and the same person will pick something warmer and slower "
    "for an essay they are reading late at night.",

    "The browser side has its own constraints. Extension background workers are terminated "
    "aggressively, so audio playback has to live somewhere that survives both navigation and "
    "the worker being shut down between events.",

    "What remains is expressiveness rather than intelligibility. A local model narrates "
    "clearly and pleasantly, but it will not act out dialogue or land a joke the way a "
    "premium cloud voice sometimes can, and that trade is worth naming honestly.",
]
ARTICLE = "\n\n".join(PARAGRAPHS)


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main() -> None:
    words = len(ARTICLE.split())
    print(f"article: {words} words, {len(ARTICLE)} chars")

    # Cold: clear cache so this measures synthesis, not disk reads.
    urllib.request.urlopen(
        urllib.request.Request(BASE + "/v1/cache/clear", data=b"", method="POST")
    ).read()

    t0 = time.perf_counter()
    job = post("/v1/speak/prepare", {"text": ARTICLE})
    prepare_ms = (time.perf_counter() - t0) * 1000
    n = len(job["sentences"])
    print(f"prepare:        {prepare_ms:7.1f} ms  ({n} chunks)")

    total_audio = 0.0
    first_ms = None
    for s in job["sentences"]:
        url = f"{BASE}/v1/audio/{s['key']}.wav?job={job['job_id']}&idx={s['idx']}"
        with urllib.request.urlopen(url) as r:
            data = r.read()
        if first_ms is None:
            first_ms = (time.perf_counter() - t0) * 1000
            print(f"FIRST AUDIO:    {first_ms:7.1f} ms  <- what the listener waits for")
        total_audio += (len(data) - 44) / 2 / 24000

    wall = time.perf_counter() - t0
    print(f"all chunks:     {wall * 1000:7.1f} ms for {total_audio:.1f}s of speech")
    print(f"RTF:            {wall / total_audio:7.4f}  ({total_audio / wall:.1f}x real-time)")

    # Warm: everything is on disk now.
    t0 = time.perf_counter()
    job = post("/v1/speak/prepare", {"text": ARTICLE})
    with urllib.request.urlopen(
        f"{BASE}/v1/audio/{job['sentences'][0]['key']}.wav"
    ) as r:
        r.read()
    print(f"cached first:   {(time.perf_counter() - t0) * 1000:7.1f} ms  <- re-read of same text")


if __name__ == "__main__":
    main()
