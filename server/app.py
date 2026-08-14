"""TextReaderAPI - local neural TTS over HTTP.

Two-step protocol for low latency:

    POST /v1/speak/prepare   -> ordered sentences, each with a content hash
    GET  /v1/audio/{key}.wav -> that sentence's audio

Preparing also starts a background pass that synthesizes the article in reading
order, so by the time the client asks for sentence N it is usually already on
disk. That is what gets first audio out in ~200 ms instead of after the whole
article has been rendered.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, audio as au, config as cfg
from .cache import AudioCache, make_key
from .engine import TTSEngine, create_engine
from .textnorm import normalize_chunks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("textreader")

cache = AudioCache()
engine: TTSEngine | None = None

# One worker, created and torn down with the app. Synthesis is serialised
# because the model is not reentrant, and Kokoro is fast enough that queueing
# beats contention. Tying its lifetime to the lifespan (rather than to module
# import) keeps the app restartable in-process, which the tests rely on.
_executor: ThreadPoolExecutor | None = None


@dataclass
class Job:
    id: str
    voice: str
    speed: float
    sentences: list[dict] = field(default_factory=list)
    cursor: int = 0
    task: asyncio.Task | None = None


_jobs: dict[str, Job] = {}
_job_order: list[str] = []
_prefetch: asyncio.Task | None = None


def _require_engine() -> TTSEngine:
    if engine is None:
        raise HTTPException(503, "engine still loading")
    return engine


def _require_executor() -> ThreadPoolExecutor:
    if _executor is None:
        raise HTTPException(503, "server is shutting down")
    return _executor


def _known_voices() -> set[str]:
    return {v.id for v in _require_engine().voices()}


def _validate(voice: str, speed: float, text: str) -> tuple[str, float]:
    if not text.strip():
        raise HTTPException(400, "input text is empty")
    if len(text) > cfg.MAX_INPUT_CHARS:
        raise HTTPException(413, f"input exceeds {cfg.MAX_INPUT_CHARS} characters")
    if voice not in _known_voices():
        raise HTTPException(400, f"unknown voice: {voice}")
    return voice, max(0.5, min(2.0, speed))


async def _synth_wav(text: str, voice: str, speed: float) -> bytes:
    """Cached synthesis of one chunk, returned as a complete WAV."""
    eng = _require_engine()
    key = make_key(eng.id, voice, speed, text)
    cache.remember(key, text, voice, speed)

    async def run() -> bytes:
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(_require_executor(), eng.synth, text, voice, speed)
        # Trimmed before caching, so the silence is paid for once and every
        # consumer - playback, stream, export - sees the same clean edges.
        return au.to_wav(au.trim_edges(pcm))

    return await cache.get_or_synth(key, run)


def _gap_after(para_end: bool) -> float:
    return cfg.PARAGRAPH_GAP_S if para_end else cfg.SENTENCE_GAP_S


async def _synth_all(
    chunks: list[tuple[str, bool]], voice: str, speed: float
) -> np.ndarray:
    """Synthesize every chunk and concatenate with natural pauses."""
    segments = []
    for text, _ in chunks:
        wav = await _synth_wav(text, voice, speed)
        pcm = np.frombuffer(au.wav_payload(wav), dtype="<i2").astype(np.float32) / 32767.0
        segments.append(pcm)
    return au.join(segments, [_gap_after(para_end) for _, para_end in chunks])


async def _prefetch_job(job: Job) -> None:
    """Render the job in reading order, jumping ahead if the client seeks."""
    i = 0
    while i < len(job.sentences):
        i = max(i, job.cursor)
        if i >= len(job.sentences):
            break
        sentence = job.sentences[i]
        try:
            await _synth_wav(sentence["text"], job.voice, job.speed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad chunk must not stop the rest
            log.warning("prefetch failed for chunk %d", i, exc_info=True)
        i += 1


def _register(job: Job) -> None:
    _jobs[job.id] = job
    _job_order.append(job.id)
    while len(_job_order) > cfg.JOB_HISTORY:
        old = _job_order.pop(0)
        stale = _jobs.pop(old, None)
        if stale and stale.task and not stale.task.done():
            stale.task.cancel()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, _executor, _prefetch
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="synth")
    loop = asyncio.get_running_loop()
    log.info("loading engine %s (%s)", cfg.ENGINE, cfg.MODEL_ID)
    engine = await loop.run_in_executor(_executor, create_engine, cfg.ENGINE)
    await loop.run_in_executor(None, cache.prune)
    try:
        yield
    finally:
        if _prefetch and not _prefetch.done():
            _prefetch.cancel()
        _prefetch = None
        for job in _jobs.values():
            if job.task and not job.task.done():
                job.task.cancel()
        _jobs.clear()
        _job_order.clear()
        engine = None
        executor, _executor = _executor, None
        executor.shutdown(wait=False)


app = FastAPI(title="TextReaderAPI", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension://.*|moz-extension://.*|https?://.*)$",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

class PrepareRequest(BaseModel):
    text: str
    voice: str = cfg.DEFAULT_VOICE
    speed: float = cfg.DEFAULT_SPEED


class SpeechRequest(BaseModel):
    """OpenAI /v1/audio/speech schema. `model` is accepted and ignored."""

    input: str
    model: str = "kokoro"
    voice: str = cfg.DEFAULT_VOICE
    speed: float = cfg.DEFAULT_SPEED
    response_format: Literal["mp3", "wav", "opus", "aac", "flac", "pcm"] = "mp3"


class CancelRequest(BaseModel):
    job_id: str | None = None


class ExportRequest(BaseModel):
    text: str
    voice: str = cfg.DEFAULT_VOICE
    speed: float = cfg.DEFAULT_SPEED
    format: Literal["mp3", "m4a", "wav", "flac", "opus"] = "mp3"
    filename: str = Field(default="article")


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    ready = engine is not None
    return {
        "ok": ready,
        "version": __version__,
        "engine": engine.id if ready else None,
        "model": cfg.MODEL_ID,
        "sample_rate": cfg.SAMPLE_RATE,
        "voices": len(engine.voices()) if ready else 0,
        "cache": cache.stats(),
    }


@app.get("/v1/voices")
async def voices():
    return {
        "default": cfg.DEFAULT_VOICE,
        "voices": [
            {
                "id": v.id,
                "name": v.name,
                "gender": v.gender,
                "language": v.language,
                "locale": v.locale,
            }
            for v in _require_engine().voices()
        ],
    }


@app.post("/v1/speak/prepare")
async def prepare(req: PrepareRequest):
    global _prefetch
    voice, speed = _validate(req.voice, req.speed, req.text)
    eng = _require_engine()

    chunks = normalize_chunks(req.text)
    if not chunks:
        raise HTTPException(400, "nothing speakable in input")

    job = Job(id=uuid.uuid4().hex[:12], voice=voice, speed=speed)
    for idx, (text, para_end) in enumerate(chunks):
        key = make_key(eng.id, voice, speed, text)
        cache.remember(key, text, voice, speed)
        # The client schedules chunks itself, so it needs the pause to leave
        # after each one. Keeping the tuning here means playback and a
        # downloaded file are paced identically.
        job.sentences.append(
            {"idx": idx, "text": text, "key": key, "gap": _gap_after(para_end)}
        )
    _register(job)

    # A newly selected passage takes priority over whatever was being rendered.
    if _prefetch and not _prefetch.done():
        _prefetch.cancel()
    _prefetch = asyncio.create_task(_prefetch_job(job))
    job.task = _prefetch

    return {"job_id": job.id, "voice": voice, "speed": speed, "sentences": job.sentences}


@app.post("/v1/speak/cancel")
async def cancel(req: CancelRequest):
    """Stop rendering a job nobody is listening to any more.

    Without this, stopping playback leaves the background pass grinding through
    the rest of the article, occupying the single synthesis worker and delaying
    whatever the listener picks next.
    """
    global _prefetch
    target = _jobs.get(req.job_id) if req.job_id else None

    if req.job_id and target is None:
        return {"cancelled": False, "reason": "unknown job_id"}

    task = target.task if target else _prefetch
    if task is None or task.done():
        return {"cancelled": False, "reason": "nothing running"}

    task.cancel()
    if task is _prefetch:
        _prefetch = None
    return {"cancelled": True}


@app.get("/v1/audio/{key}.wav")
async def audio(key: str, job: str | None = None, idx: int | None = None):
    if job and idx is not None and job in _jobs:
        # Tell the prefetch pass where the listener actually is.
        _jobs[job].cursor = idx

    cached = cache.get(key)
    if cached is not None:
        return Response(cached, media_type="audio/wav", headers={"Cache-Control": "max-age=31536000"})

    params = cache.params_for(key)
    if params is None:
        raise HTTPException(404, "unknown audio key; call /v1/speak/prepare first")
    text, voice, speed = params
    data = await _synth_wav(text, voice, speed)
    return Response(data, media_type="audio/wav", headers={"Cache-Control": "max-age=31536000"})


@app.get("/v1/speak/stream")
async def stream(
    text: str | None = None,
    job_id: str | None = None,
    voice: str = cfg.DEFAULT_VOICE,
    speed: float = cfg.DEFAULT_SPEED,
):
    """Chunked WAV of a whole passage, for `<audio src>` and `curl | afplay`."""
    if job_id:
        target = _jobs.get(job_id)
        if target is None:
            raise HTTPException(404, "unknown job_id")
        chunks = [(s["text"], s.get("gap") == cfg.PARAGRAPH_GAP_S) for s in target.sentences]
        voice, speed = target.voice, target.speed
    elif text:
        voice, speed = _validate(voice, speed, text)
        chunks = normalize_chunks(text)
    else:
        raise HTTPException(400, "pass either text or job_id")

    async def body():
        yield au.streaming_wav_header()
        for i, (chunk, para_end) in enumerate(chunks):
            if i:
                yield au.to_pcm16(au.silence(_gap_after(chunks[i - 1][1])))
            wav = await _synth_wav(chunk, voice, speed)
            yield au.wav_payload(wav)

    return StreamingResponse(body(), media_type="audio/wav")


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    """OpenAI-compatible endpoint, so existing OpenAI-TTS clients just work."""
    voice, speed = _validate(req.voice, req.speed, req.input)
    chunks = normalize_chunks(req.input)
    if not chunks:
        raise HTTPException(400, "nothing speakable in input")
    pcm = await _synth_all(chunks, voice, speed)
    try:
        data, content_type = au.encode(pcm, req.response_format)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, f"encoding to {req.response_format} failed: {exc}") from exc
    return Response(data, media_type=content_type)


@app.post("/v1/export")
async def export(req: ExportRequest):
    voice, speed = _validate(req.voice, req.speed, req.text)
    chunks = normalize_chunks(req.text)
    if not chunks:
        raise HTTPException(400, "nothing speakable in input")
    pcm = await _synth_all(chunks, voice, speed)
    try:
        data, content_type = au.encode(pcm, req.format)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, f"encoding to {req.format} failed: {exc}") from exc
    safe = "".join(c for c in req.filename if c.isalnum() or c in " -_").strip() or "article"
    return Response(
        data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.{req.format}"',
            "X-Duration-Seconds": f"{len(pcm) / cfg.SAMPLE_RATE:.1f}",
        },
    )


@app.post("/v1/diag")
async def diag(payload: dict):
    """Sink for extension telemetry.

    The offscreen document has no console anyone can reach without opening its
    dedicated devtools window, so it reports here instead and the events land in
    the normal server log next to the requests they explain.
    """
    event = payload.pop("event", "?")
    where = payload.pop("where", "?")
    log.warning("DIAG %-10s %-22s %s", where, event, payload)
    return {"ok": True}


@app.post("/v1/cache/clear")
async def cache_clear():
    before = cache.stats()
    for path in cache.root.rglob("*.wav"):
        path.unlink(missing_ok=True)
    return {"cleared": before}


@app.get("/", response_class=HTMLResponse)
async def index():
    """Minimal test page - verify the server without loading the extension."""
    return _INDEX_HTML


_INDEX_HTML = """<!doctype html>
<meta charset="utf-8"><title>TextReaderAPI</title>
<style>
 body{font:16px/1.5 system-ui;margin:0;padding:2rem;max-width:46rem;background:#111;color:#eee}
 textarea{width:100%;height:11rem;font:inherit;padding:.6rem;background:#1b1b1b;color:#eee;
   border:1px solid #333;border-radius:6px}
 button,select,input{font:inherit;padding:.4rem .7rem;margin:.3rem .3rem 0 0;background:#222;
   color:#eee;border:1px solid #444;border-radius:6px}
 button{cursor:pointer} button:hover{background:#2c2c2c}
 #log{white-space:pre-wrap;color:#8ab;margin-top:1rem;font-size:.85rem}
</style>
<h1>TextReaderAPI</h1>
<textarea id="t">Dr. Smith raised $1.2M in 1995, i.e. approx. 3% of the fund.
The GDP of the U.S. rose 2.5% between 2010-2015, per NASA and the IMF.</textarea>
<div>
  <select id="voice"></select>
  <input id="speed" type="number" step="0.05" min="0.5" max="2" value="1">
  <button onclick="speak()">Speak</button>
  <button onclick="dl()">Download MP3</button>
</div>
<div id="log"></div>
<script>
const log = m => document.getElementById('log').textContent = m;
fetch('/v1/voices').then(r=>r.json()).then(d=>{
  const s = document.getElementById('voice');
  d.voices.forEach(v=>{
    const o=document.createElement('option');
    o.value=v.id; o.textContent=`${v.id} - ${v.name} (${v.gender}, ${v.locale})`;
    if(v.id===d.default) o.selected=true; s.append(o);
  });
});
const params = () => ({
  text: document.getElementById('t').value,
  voice: document.getElementById('voice').value,
  speed: parseFloat(document.getElementById('speed').value),
});
let ctx, playing = [];
async function speak(){
  playing.forEach(s=>{try{s.stop()}catch(e){}}); playing=[];
  ctx = ctx || new AudioContext();
  const t0 = performance.now();
  const r = await fetch('/v1/speak/prepare',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(params())});
  const job = await r.json();
  if(!r.ok){ log('error: '+JSON.stringify(job)); return; }
  log(`${job.sentences.length} chunks prepared in ${(performance.now()-t0).toFixed(0)} ms`);
  let at = ctx.currentTime + 0.05, first = null;
  for(const s of job.sentences){
    const res = await fetch(`/v1/audio/${s.key}.wav?job=${job.job_id}&idx=${s.idx}`);
    const buf = await ctx.decodeAudioData(await res.arrayBuffer());
    if(first===null){ first = performance.now()-t0; log(`first audio in ${first.toFixed(0)} ms`); }
    const src = ctx.createBufferSource(); src.buffer = buf; src.connect(ctx.destination);
    at = Math.max(at, ctx.currentTime); src.start(at); at += buf.duration;
    playing.push(src);
  }
}
async function dl(){
  const r = await fetch('/v1/export',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({...params(), format:'mp3'})});
  const b = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(b); a.download='article.mp3'; a.click();
}
</script>
"""
