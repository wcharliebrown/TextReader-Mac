// The player. Lives in an offscreen document because that is the only extension
// context whose audio survives both page navigation and the service worker
// being torn down between events.
//
// Scheduling notes:
//  - Chunks are scheduled ahead on the AudioContext clock rather than chained
//    with `onended`, so a busy page cannot introduce audible gaps.
//  - Pause is ctx.suspend(), which freezes currentTime and leaves every already
//    scheduled chunk valid. Resume needs no rescheduling arithmetic at all.

// Reports to the server, because an offscreen document's console is not
// reachable without opening its own devtools window.
let DIAG_SERVER = 'http://127.0.0.1:8842';
function report(event, extra = {}) {
  try {
    fetch(`${DIAG_SERVER}/v1/diag`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ where: 'offscreen', event, ...extra }),
    }).catch(() => {});
  } catch { /* never let telemetry break playback */ }
}
self.addEventListener('error', (e) => report('window.error', { message: String(e.message) }));
self.addEventListener('unhandledrejection', (e) =>
  report('unhandled.rejection', { message: String(e.reason) }));

const HORIZON = 3;      // chunks scheduled ahead of the playhead
const PREFETCH = 4;     // chunks fetched ahead of the playhead
// Fallback only. The server sends a per-chunk gap, because it knows which
// chunks end a paragraph and it has to pace an exported file identically.
// Chunk audio is trimmed server-side, so this gap is the whole pause.
const GAP = 0.34;

class Player {
  constructor() {
    this.ctx = null;
    this.reset();
  }

  reset() {
    this.job = null;
    this.chunks = [];
    this.buffers = new Map();   // index -> AudioBuffer (null while in flight)
    this.scheduled = [];        // { index, node, startAt, endAt }
    this.cursor = 0;            // next chunk to schedule
    this.index = 0;             // chunk the listener is hearing
    this.playhead = 0;          // ctx time at which the scheduled tail ends
    this.state = 'idle';
    this.rate = 1;
    this.settings = null;
    this.generation = 0;        // bumped on every new passage; cancels stale work
    this.failures = new Map();  // index -> attempts, so a bad chunk is not retried forever
  }

  emit(extra = {}) {
    chrome.runtime.sendMessage({
      target: 'background',
      from: 'offscreen',
      state: this.state,
      index: this.index,
      total: this.chunks.length,
      rate: this.rate,
      text: this.chunks[this.index]?.text ?? '',
      ...extra,
    }).catch(() => {});
  }

  async load(text, settings) {
    DIAG_SERVER = settings.server || DIAG_SERVER;
    report('load', { chars: text.length, voice: settings.voice, speed: settings.speed });
    this.stop();
    const generation = ++this.generation;
    this.settings = settings;
    this.rate = settings.rate ?? 1;
    this.state = 'preparing';
    this.emit();

    let res;
    try {
      res = await fetch(`${settings.server}/v1/speak/prepare`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text, voice: settings.voice, speed: settings.speed }),
      });
    } catch {
      res = null;
    }
    if (generation !== this.generation) return;
    if (!res || !res.ok) {
      this.state = 'error';
      this.emit({ message: `no server at ${settings.server}` });
      return;
    }

    const job = await res.json();
    if (generation !== this.generation) return;
    this.job = job;
    this.chunks = job.sentences;
    this.buffers = new Map();
    this.failures = new Map();
    this.cursor = 0;
    this.index = 0;
    await this.start();
  }

  audioUrl(i) {
    const c = this.chunks[i];
    return `${this.settings.server}/v1/audio/${c.key}.wav?job=${this.job.job_id}&idx=${i}`;
  }

  async fetchChunk(i) {
    if (this.buffers.has(i) || i >= this.chunks.length) return;
    this.buffers.set(i, null);   // claim the slot so it is fetched once
    const generation = this.generation;
    try {
      const res = await fetch(this.audioUrl(i));
      if (!res.ok) throw new Error(`chunk ${i}: HTTP ${res.status}`);
      const buf = await this.ctx.decodeAudioData(await res.arrayBuffer());
      if (generation !== this.generation) return;
      this.buffers.set(i, buf);
      if (i === 0) report('chunk.decoded', { index: i, duration: buf.duration });
      this.pump();
    } catch (e) {
      if (generation !== this.generation) return;
      const attempts = (this.failures.get(i) ?? 0) + 1;
      this.failures.set(i, attempts);
      report('chunk.failed', { index: i, attempts, message: String(e) });
      // Only free the slot while retries remain; otherwise a permanently bad
      // chunk gets refetched on every single pump.
      if (attempts < 3) this.buffers.delete(i);
    }
  }

  prefetch() {
    const end = Math.min(this.cursor + PREFETCH, this.chunks.length);
    for (let i = this.cursor; i < end; i++) this.fetchChunk(i);
  }

  /** Schedule every decoded chunk that fits inside the horizon. */
  pump() {
    if (this.state !== 'playing' || !this.ctx) return;
    while (this.scheduled.length < HORIZON && this.cursor < this.chunks.length) {
      const buf = this.buffers.get(this.cursor);
      if (!buf) break;                    // not decoded yet; pump() reruns on arrival
      const node = this.ctx.createBufferSource();
      node.buffer = buf;
      node.playbackRate.value = this.rate;
      node.connect(this.ctx.destination);
      const startAt = Math.max(this.playhead, this.ctx.currentTime + 0.02);
      node.start(startAt);
      const entry = {
        index: this.cursor,
        node,
        startAt,
        endAt: startAt + buf.duration / this.rate,
      };
      node.onended = () => this.onChunkEnded(entry);
      this.scheduled.push(entry);
      // The pause that follows this chunk, not a uniform one: a paragraph
      // break is given longer than a sentence break.
      this.playhead = entry.endAt + (this.chunks[this.cursor]?.gap ?? GAP) / this.rate;
      report('scheduled', {
        index: entry.index, startAt: +startAt.toFixed(2),
        now: +this.ctx.currentTime.toFixed(2), ctxState: this.ctx.state,
      });
      this.cursor++;
    }
    this.prefetch();
  }

  onChunkEnded(entry) {
    this.scheduled = this.scheduled.filter((e) => e !== entry);
    if (this.state !== 'playing') return;
    if (this.scheduled.length) {
      this.index = this.scheduled[0].index;
      this.emit();
    } else if (this.cursor >= this.chunks.length) {
      this.state = 'ended';
      this.emit();
    }
    this.pump();
  }

  async start() {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      report('ctx.created', { state: this.ctx.state, sampleRate: this.ctx.sampleRate });
    }
    if (this.ctx.state === 'suspended') {
      // resume() never settles without user activation, so it must be raced.
      const timedOut = Symbol('timeout');
      const outcome = await Promise.race([
        this.ctx.resume().then(() => 'resumed').catch((e) => `rejected: ${e}`),
        new Promise((r) => setTimeout(() => r(timedOut), 1500)),
      ]);
      report('ctx.resume', { outcome: outcome === timedOut ? 'HUNG' : outcome, state: this.ctx.state });
    }
    this.state = 'playing';
    this.playhead = this.ctx.currentTime;
    this.emit();
    this.pump();
    report('start', { ctxState: this.ctx.state, chunks: this.chunks.length, cursor: this.cursor });
  }

  clearScheduled() {
    for (const entry of this.scheduled) {
      entry.node.onended = null;
      try { entry.node.stop(); } catch { /* already finished */ }
    }
    this.scheduled = [];
  }

  async pause() {
    if (this.state !== 'playing' || !this.ctx) return;
    await this.ctx.suspend();   // freezing the clock keeps scheduled chunks valid
    this.state = 'paused';
    this.emit();
  }

  async resume() {
    if (this.state !== 'paused' || !this.ctx) return;
    await this.ctx.resume();
    this.state = 'playing';
    this.emit();
    this.pump();
  }

  async toggle() {
    if (this.state === 'playing') await this.pause();
    else if (this.state === 'paused') await this.resume();
  }

  stop() {
    // Stopping playback should also stop the work: the server otherwise keeps
    // rendering the rest of the article into the cache for nobody.
    const jobId = this.job?.job_id;
    const server = this.settings?.server;
    if (jobId && server) {
      fetch(`${server}/v1/speak/cancel`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ job_id: jobId }),
      }).catch(() => {});
    }
    this.generation++;
    this.clearScheduled();
    if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume();
    const had = this.chunks.length > 0;
    this.job = null;
    this.chunks = [];
    this.buffers = new Map();
    this.cursor = 0;
    this.index = 0;
    this.state = 'idle';
    if (had) this.emit();
  }

  async seek(index) {
    if (!this.chunks.length) return;
    const target = Math.max(0, Math.min(index, this.chunks.length - 1));
    this.clearScheduled();
    this.cursor = target;
    this.index = target;
    // Nudge the server's background rendering to follow the listener instead of
    // grinding through chunks that have just been skipped past.
    fetch(this.audioUrl(target)).catch(() => {});
    await this.start();
  }

  next() { return this.seek(this.index + 1); }
  prev() { return this.seek(this.index - 1); }

  async setRate(rate) {
    this.rate = Math.max(0.5, Math.min(3, rate));
    if (this.state === 'idle' || !this.chunks.length) {
      this.emit();
      return;
    }
    // A rate change invalidates every scheduled end time, so restart the chunk
    // being heard rather than trying to patch the existing timeline.
    await this.seek(this.index);
  }

}

const player = new Player();

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== 'offscreen') return false;
  (async () => {
    switch (msg.type) {
      case 'play':   await player.load(msg.text, msg); break;
      case 'toggle': await player.toggle(); break;
      case 'pause':  await player.pause(); break;
      case 'resume': await player.resume(); break;
      case 'stop':   player.stop(); break;
      case 'next':   await player.next(); break;
      case 'prev':   await player.prev(); break;
      case 'rate':   await player.setRate(msg.rate); break;
      case 'status': player.emit(); break;
    }
    sendResponse({ ok: true });
  })().catch((e) => {
    report('handler.threw', { type: msg.type, message: String(e) });
    sendResponse({ ok: false });
  });
  return true;   // response is sent asynchronously
});
