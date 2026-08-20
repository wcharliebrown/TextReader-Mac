# TextReader

Highlight text in Chrome, right-click, **Speak selection** — and hear it in a
neural voice generated on your own Mac. No API keys, no credits, no network
calls, no per-word billing. The model is [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
running on Apple MLX, and it starts talking in about a third of a second.

Built because the two obvious options both fail for reading articles: the macOS
built-in voices are not pleasant enough to listen to for twenty minutes, and
ElevenLabs is pleasant enough but bills per character, so a habit of listening
to a few long reads a day turns into a recurring cost that quietly caps how much
you use it.

Measured on an M1 Mac Studio (32 GB):

| | |
|---|---|
| Time to first audio | **~275 ms** |
| Synthesis throughput | **24× real-time** (RTF 0.041) |
| Cached re-read | **~6 ms** |
| Memory | ~2 GB resident |
| Cost per article | **£0** |

## Requirements

Apple silicon (M1 or later) and macOS 13+. The MLX backend is Metal-only; there
is a PyTorch fallback engine, but this is not built for Intel Macs.

## Install

```sh
brew install uv espeak-ng
git clone https://github.com/wcharliebrown/TextReader-Mac.git
cd TextReader-Mac
uv venv --python 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/python -m spacy download en_core_web_sm    # misaki's homograph tagger
```

Run it at login:

```sh
./scripts/install-launchagent.sh
```

Or in the foreground while developing:

```sh
.venv/bin/python -m uvicorn server.app:app --port 8842
```

Then open <http://127.0.0.1:8842> for a test page that exercises the whole path
without the extension. The first run downloads ~330 MB of model weights.

## The Chrome extension

`chrome://extensions` → enable **Developer mode** → **Load unpacked** → select
the `extension/` directory.

- Right-click a selection → **Speak selection**
- Right-click a page → **Speak this article** (extracts the main content)
- Right-click anywhere → **Stop speaking** (shown only while it is speaking)
- Right-click a selection → **Download selection as MP3**
- Right-click a page → **Download this article as MP3** (named after the page title)
- `Cmd+Shift+S` speak, `Cmd+Shift+P` play/pause, `Cmd+Shift+X` stop
- An on-page bar gives prev / play / stop / next and a speed toggle
- Click the toolbar icon for settings — server address, voice, speed

No content script runs until you ask for one, and the controls live in a closed
shadow root so page CSS cannot reach them.

## Why it is built this way

**Playback lives in an offscreen document.** MV3 service workers are terminated
between events, and audio started from a content script dies the moment you
navigate. An offscreen document with the `AUDIO_PLAYBACK` reason is the only
extension context that survives both, so that is where the audio queue lives.

**MP3 export goes through Chrome's download manager,** not through that
offscreen document. Chrome closes an `AUDIO_PLAYBACK` document after 30 seconds
without audio playing, and rendering a whole article takes longer than that, so
fetching the file inside the extension had it reaped mid-request. Handing the
`POST` to `chrome.downloads` makes the browser own the transfer, which outlives
both the offscreen document and the service worker.

**Two round trips, not one.** `POST /v1/speak/prepare` returns the article split
into sentence-sized chunks, each identified by a content hash; the client then
pulls chunks as it needs them, three ahead of the playhead. Rendering a whole
article before playing anything would mean staring at a spinner for seven
seconds. This way the first chunk arrives in ~275 ms and the rest renders
while you are listening to it. Seeking tells the server where you jumped to, so
skipping ahead does not queue behind chunks nobody is waiting for.

**Only the first chunk is held short.** It alone decides how soon audio starts;
by the time it is playing, the prefetch is already ahead. So the first chunk is
capped at 150 characters and the rest are packed with whole sentences up to 300,
which lets Kokoro carry an intonation contour across a full stop instead of
restarting at every one. A typical article ends up with three of every four
sentence boundaries rendered by the model rather than assembled by us. The hard
ceiling is 450: measured, Kokoro renders one segment up to ~493 characters and
splits internally at ~521, and an internal seam bakes in silence that cannot be
trimmed afterwards.

**Seams are trimmed, then paced deliberately.** Kokoro renders ~0.30 s of
lead-in and ~0.47 s of tail on every chunk regardless of its length. Left in,
they stack with the gap between chunks, so a sentence boundary ran to ~0.86 s —
about double the 0.30–0.52 s the model renders between sentences handed to it
together, which is what made assembled audio sound like a list being read.
Chunks are now trimmed before caching and one deliberate pause is inserted:
0.34 s between sentences, 0.60 s between paragraphs. The server sends the pause
along with each chunk, so playback and a downloaded file are paced identically.
`AUDIO_REV` in the cache key means changing any of this retires old renders
rather than serving them forever.

**Text normalisation does most of the work.** A model reading raw web text will
happily pronounce `[1]`, read `1995` as "one thousand nine hundred ninety-five",
say "G-D-P" as a word and break a sentence in the middle of `Dr. Smith`.
`server/textnorm.py` is four pure functions — clean, disambiguate, expand, split
— and it is where most of the "sounds less robotic" gain lives.

The disambiguate stage exists because spelling does not settle how a word is
said. Kokoro picks a heteronym's sound from a part-of-speech tag and is right
about "live", "wind", "record" and two dozen others — but it reads a present
tense "they read" as *red*, and it has no entry at all for lead the metal, so
"lead pipe" always rhymed with *feed*. Those cases are respelled in context
(`read` → `reed`, `lead` → `led`) unless the sentence carries a past-tense cue.
Respelling in ordinary words rather than phoneme markup keeps the stage
independent of which engine is behind it.

Initialisms turned out to be a case of doing too much rather than too little.
Spacing out the letters — `CIA` → `C I A` — was meant to make the model say
letter names, but a lone capital A is the article, so it came out "see eye *uh*",
and `AI` came out "*uh* eye". Handed the token intact, Kokoro spells it
correctly by itself; measured across sixty-odd initialisms only `JSON`, `YAML`
and `IKEA` needed help, and they get a respelling each.

**Everything is cached by content hash.** The key covers text, voice and speed,
so a re-read is a disk read. Stopping also cancels the server's rendering pass,
freeing the single synthesis worker immediately rather than grinding through an
article nobody is listening to.

## How it sounds

Kokoro is a clear step up from the macOS built-in voices and is very good at
article narration — clean prosody, natural pacing, 54 voices. It is *not* as
expressive as ElevenLabs on dialogue or emphasis. For listening to prose while
doing something else the difference is small; for character work it is audible.

`server/engine.py` defines a `TTSEngine` protocol, so a heavier model such as
Chatterbox or Orpheus can be added as one new class without touching anything
else. Select it with `TEXTREADER_ENGINE`.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/speak/prepare` | Text → ordered chunks with content hashes; starts background rendering |
| `GET` | `/v1/audio/{key}.wav` | One chunk's audio |
| `POST` | `/v1/speak/cancel` | Stop rendering a job (`{job_id}`, or omit for whatever is running) |
| `GET` | `/v1/speak/stream` | Chunked WAV of a whole passage (`?text=` or `?job_id=`) |
| `POST` | `/v1/audio/speech` | **OpenAI-compatible** — point any OpenAI TTS client here |
| `POST` | `/v1/export` | Whole passage as one mp3/m4a/flac/opus file |
| `GET` | `/v1/voices` | 54 voices with language and gender |
| `POST` | `/v1/cache/clear` | Drop every cached render |
| `POST` | `/v1/diag` | Sink for the extension's own diagnostics (see below) |
| `GET` | `/healthz` | Engine, model, cache size |

Because `/v1/audio/speech` matches OpenAI's schema, anything that already speaks
OpenAI TTS works against it unchanged:

```sh
curl -s -X POST localhost:8842/v1/audio/speech \
  -H 'content-type: application/json' \
  -d '{"model":"kokoro","input":"Hello there.","voice":"af_heart"}' \
  --output out.mp3
```

## Layout

```
server/
  app.py        FastAPI routes, job registry, background rendering
  engine.py     TTSEngine protocol; MLX (primary) and PyTorch (fallback) Kokoro
  textnorm.py   clean → disambiguate → expand → split; decides how it sounds
  diagnose.py   round-trip check: speak, transcribe, diff against the input
  cache.py      content-addressed WAV cache, in-flight dedup, LRU prune
  audio.py      WAV framing, ffmpeg encoding
extension/      Chrome MV3: service worker, offscreen player, on-page controls
scripts/        benchmark, LaunchAgent install, browser test harnesses
```

## Configuration

All `TEXTREADER_*` environment variables: `ENGINE`, `MODEL`, `VOICE`, `SPEED`,
`HOST`, `PORT`, `CACHE_DIR`, `CACHE_MAX_BYTES`, `CACHE`, `MIN_CHUNK`,
`MAX_CHUNK`, `MAX_INPUT`. See `server/config.py`.

Audio is cached in `~/Library/Caches/TextReaderAPI`, capped at 2 GB, pruned LRU.

## Tests

```sh
.venv/bin/python -m pytest          # 197: text pipeline, phonemes, cache, HTTP API
node scripts/test_player.mjs        # 17: offscreen player state machine
node scripts/test_background.mjs    # 17: menu / hotkey / offscreen / download routing
.venv/bin/python scripts/bench.py   # latency and throughput
```

There is also a diagnostics mode for tuning the normalization rules themselves
(`uv pip install -e '.[diagnose]'` first — it needs Whisper):

```sh
.venv/bin/python -m server.diagnose article.txt   # or:  pbpaste | ... -m server.diagnose -
```

It sends the text through the running server, transcribes each chunk's audio
with Whisper on MLX, and prints every place the transcript disagrees with what
the engine was asked to say — each one a candidate for a new rule in
`textnorm.py`. It hears dropped words, garbled respellings and mangled numbers;
it cannot hear a wrong vowel that spells the same word (Whisper corrects those
while listening), which is why the phoneme assertions in
`tests/test_pronunciation.py` exist as well.

The two Node harnesses exist because the extension's long-lived contexts have no
console reachable without opening their own devtools windows. They stub `chrome.*`
and Web Audio and drive the real modules — the player harness runs against a live
server. When something does go wrong in a browser, the extension also reports to
`POST /v1/diag`, which lands in the server log next to the requests it explains.
That endpoint is kept deliberately, not left over from debugging: it has since
been the thing that identified two failures invisible from the browser side,
including one where Chrome was closing the offscreen document mid-request. It
posts only to this server, on this machine.

## Not implemented

Highlighting the sentence being spoken inside the page. The player reports its
position, so the hook is there, but mapping a normalised chunk back to the
original DOM range is a separate piece of work.

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it.
