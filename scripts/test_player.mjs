// Drives extension/offscreen.js against the live server with stubbed browser
// APIs. The scheduling state machine (generation guards, prefetch, seek,
// pause/resume) is the riskiest code in the extension and cannot be reached by
// the Python tests, so it gets exercised here.
import assert from 'node:assert/strict';

// offscreen.js runs in a document, where `self` is the global and carries the
// EventTarget interface. Node has neither, so both are provided before the
// module under test is imported.
const globalListeners = [];
globalThis.self = {
  addEventListener: (type, fn) => globalListeners.push({ type, fn }),
};

const SERVER = process.env.SERVER || 'http://127.0.0.1:8842';
const statuses = [];
let listener = null;

// --- stub chrome.* ---------------------------------------------------------
globalThis.chrome = {
  runtime: {
    onMessage: { addListener: (fn) => { listener = fn; } },
    sendMessage: async (msg) => { statuses.push(msg); },
  },
  downloads: { download: async () => 1 },
};

// --- stub Web Audio --------------------------------------------------------
// A manual clock: nothing plays in real time, we just advance it and assert on
// what got scheduled.
let now = 0;
const started = [];
class FakeAudioContext {
  constructor() { this.state = 'running'; this.destination = {}; }
  get currentTime() { return now; }
  async suspend() { this.state = 'suspended'; }
  async resume() { this.state = 'running'; }
  createBufferSource() {
    const node = {
      buffer: null, playbackRate: { value: 1 }, onended: null,
      connect() {}, stop() { node.stopped = true; }, stopped: false,
      start(at) { started.push({ at, node }); node.startedAt = at; },
    };
    return node;
  }
  async decodeAudioData(arrayBuffer) {
    const view = new DataView(arrayBuffer);
    const dataLen = view.getUint32(40, true);
    return { duration: dataLen / 2 / 24000, length: dataLen / 2, sampleRate: 24000 };
  }
}
globalThis.AudioContext = FakeAudioContext;

// --- helpers ---------------------------------------------------------------
const send = (msg) => new Promise((resolve) => {
  const handled = listener({ target: 'offscreen', ...msg }, {}, resolve);
  if (!handled) resolve({});
});
const settle = (ms = 400) => new Promise((r) => setTimeout(r, ms));
const last = () => statuses[statuses.length - 1];

// Record outbound calls so we can assert on the ones that have no other
// observable effect, such as cancelling server-side rendering.
const calls = [];
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) => { calls.push(String(url)); return realFetch(url, init); };
const called = (fragment) => calls.some((u) => u.includes(fragment));
const seen = (state) => statuses.some((s) => s.state === state);

// Finish every scheduled chunk in order, as real playback would.
function drainScheduled() {
  let guard = 0;
  while (started.length && guard++ < 500) {
    const { node } = started.shift();
    now = Math.max(now, node.startedAt) + (node.buffer?.duration ?? 0);
    if (node.onended && !node.stopped) node.onended();
  }
}

const TEXT = [
  'The first paragraph of a short test article runs long enough to be its own chunk.',
  'A second sentence follows it, also comfortably past the merge threshold used by the splitter.',
  'A third sentence gives the prefetch logic something to run ahead into while the first plays.',
  'A fourth sentence exists so that seeking forward has somewhere to land.',
  'And a fifth sentence closes the passage out with enough words to matter.',
].join(' ');

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failures++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}

// --- run -------------------------------------------------------------------
await import('../extension/offscreen.js');
assert.ok(listener, 'offscreen.js registered no message listener');

console.log('=== offscreen player ===');

await send({ type: 'play', text: TEXT, server: SERVER, voice: 'af_heart', speed: 1.0, rate: 1.0 });
await settle();

await check('reports preparing then playing', () => {
  assert.ok(seen('preparing'), 'never reported preparing');
  assert.ok(seen('playing'), 'never reported playing');
});

await check('split the passage into multiple chunks', () => {
  assert.ok(last().total >= 3, `expected >=3 chunks, got ${last().total}`);
});

await check('scheduled chunks up to the horizon, not the whole article', () => {
  assert.ok(started.length > 0, 'nothing was scheduled');
  assert.ok(started.length <= 3, `scheduled ${started.length}, horizon is 3`);
});

await check('chunks are scheduled back to back, in order', () => {
  for (let i = 1; i < started.length; i++) {
    assert.ok(started[i].at >= started[i - 1].at, 'chunk scheduled out of order');
  }
});

const total = last().total;

await check('advances through the passage to the end', async () => {
  for (let i = 0; i < total + 6 && last().state !== 'ended'; i++) {
    drainScheduled();
    await settle(250);
  }
  assert.equal(last().state, 'ended', `stalled at chunk ${last().index} of ${total}`);
});

await check('pause suspends the clock, resume restarts it', async () => {
  statuses.length = 0; started.length = 0;
  await send({ type: 'play', text: TEXT, server: SERVER, voice: 'af_heart', speed: 1.0, rate: 1.0 });
  await settle();
  await send({ type: 'pause' });
  assert.equal(last().state, 'paused', 'pause did not report paused');
  await send({ type: 'resume' });
  assert.equal(last().state, 'playing', 'resume did not report playing');
});

await check('toggle flips between playing and paused', async () => {
  await send({ type: 'toggle' });
  assert.equal(last().state, 'paused');
  await send({ type: 'toggle' });
  assert.equal(last().state, 'playing');
});

await check('seek jumps to the requested chunk', async () => {
  await send({ type: 'next' });
  await settle();
  assert.ok(last().index >= 1, `expected index >=1 after next, got ${last().index}`);
  const afterNext = last().index;
  await send({ type: 'prev' });
  await settle();
  assert.ok(last().index < afterNext, 'prev did not move backwards');
});

await check('seek is clamped at the ends', async () => {
  for (let i = 0; i < 3; i++) { await send({ type: 'prev' }); await settle(120); }
  assert.equal(last().index, 0, 'prev ran past the start');
});

await check('rate change is applied and reported', async () => {
  await send({ type: 'rate', rate: 1.5 });
  await settle();
  assert.equal(last().rate, 1.5);
  const node = started[started.length - 1]?.node;
  assert.equal(node?.playbackRate.value, 1.5, 'playbackRate not applied to the source node');
});

await check('rate is clamped to a sane range', async () => {
  await send({ type: 'rate', rate: 99 });
  await settle();
  assert.equal(last().rate, 3);
});

await check('stop clears the passage and stops scheduled nodes', async () => {
  const live = started.map((s) => s.node).filter((n) => !n.stopped);
  await send({ type: 'stop' });
  assert.equal(last().state, 'idle', 'stop did not report idle');
  assert.ok(live.every((n) => n.stopped), 'stop left source nodes running');
});

await check('stop tells the server to cancel background rendering', async () => {
  calls.length = 0; statuses.length = 0;
  await send({ type: 'play', text: TEXT, server: SERVER, voice: 'af_heart', speed: 1.0, rate: 1.0 });
  await settle();
  calls.length = 0;
  await send({ type: 'stop' });
  await settle(200);
  assert.ok(called('/v1/speak/cancel'), 'stop did not cancel the job on the server');
});

await check('stopping with nothing playing is harmless', async () => {
  calls.length = 0;
  await send({ type: 'stop' });
  await settle(150);
  assert.ok(!called('/v1/speak/cancel'), 'cancelled a job that did not exist');
});

await check('a new passage cancels the previous one', async () => {
  started.length = 0;
  const first = send({ type: 'play', text: TEXT, server: SERVER, voice: 'af_heart', speed: 1.0, rate: 1.0 });
  const second = send({ type: 'play', text: 'A completely different and shorter passage entirely.', server: SERVER, voice: 'af_heart', speed: 1.0, rate: 1.0 });
  await Promise.all([first, second]);
  await settle(600);
  assert.equal(last().state, 'playing', `expected playing, got ${last().state}`);
  assert.ok(last().total <= 2, `stale job won the race: ${last().total} chunks`);
});

await check('an unreachable server reports an error rather than hanging', async () => {
  await send({ type: 'play', text: TEXT, server: 'http://127.0.0.1:9', voice: 'af_heart', speed: 1.0, rate: 1.0 });
  await settle(600);
  assert.equal(last().state, 'error', `expected error, got ${last().state}`);
  assert.match(last().message ?? '', /no server/);
});

await check('the pause after each chunk is the one the server published', async () => {
  // Chunk audio is trimmed server-side, so the scheduled gap IS the whole
  // pause a listener hears - and a paragraph break must outlast a sentence one.
  const S = 'This sentence is a fairly ordinary length for prose in an article. ';
  const text = `${S.repeat(8).trim()}\n\n${S.repeat(4).trim()}`;

  const job = await (await fetch(`${SERVER}/v1/speak/prepare`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text, voice: 'af_heart', speed: 1.0 }),
  })).json();
  const published = job.sentences.map((c) => c.gap);
  assert.ok(published.length >= 3, `need several chunks, got ${published.length}`);
  assert.ok(new Set(published).size > 1, 'server published a uniform gap; nothing to check');

  started.length = 0;
  await send({ type: 'play', text, server: SERVER, voice: 'af_heart', speed: 1.0, rate: 1.0 });
  // Fresh text, so every chunk is synthesized rather than served from cache.
  await settle(3000);
  // drainScheduled() empties `started`, so copy each batch out before draining.
  const scheduled = [];
  for (let i = 0; i < 10 && scheduled.length < published.length; i++) {
    scheduled.push(...started);
    drainScheduled();
    await settle(1200);
  }
  scheduled.push(...started);
  scheduled.sort((a, b) => a.at - b.at);
  assert.ok(scheduled.length >= 3, `only ${scheduled.length} scheduled; state=${last()?.state} total=${last()?.total} msg=${last()?.message}`);
  for (let i = 1; i < scheduled.length; i++) {
    const prev = scheduled[i - 1];
    const measured = scheduled[i].at - (prev.at + prev.node.buffer.duration);
    assert.ok(
      Math.abs(measured - published[i - 1]) < 0.02,
      `gap after chunk ${i - 1}: scheduled ${measured.toFixed(3)}s, published ${published[i - 1]}s`,
    );
  }
});

console.log(failures ? `\n${failures} FAILED` : '\nall player checks passed');
process.exit(failures ? 1 : 0);
