// Drives extension/background.js with stubbed chrome.* APIs. This covers the
// menu -> offscreen -> stop routing, which is exactly where the silent-failure
// bug lived, and which the offscreen harness cannot reach.
import assert from 'node:assert/strict';

const menus = new Map();
const sentToOffscreen = [];
const sentToTabs = [];
const downloaded = [];
const fetched = [];   // telemetry bodies, so reports can be asserted on
let onDownloadChanged;
let onInstalled, onMenuClicked, onCommand, onMessage;
let offscreenDocs = 0;
let createCalls = 0;
let failNextCreate = false;

globalThis.chrome = {
  runtime: {
    onInstalled: { addListener: (fn) => { onInstalled = fn; } },
    onMessage: { addListener: (fn) => { onMessage = fn; } },
    getURL: (p) => `chrome-extension://test/${p}`,
    getContexts: async () => (offscreenDocs ? [{ contextType: 'OFFSCREEN_DOCUMENT' }] : []),
    sendMessage: async (msg) => {
      if (msg.target === 'offscreen') {
        if (!offscreenDocs) throw new Error('Could not establish connection.');
        sentToOffscreen.push(msg);
      }
      return { ok: true };
    },
  },
  offscreen: {
    createDocument: async () => {
      createCalls++;
      if (failNextCreate) { failNextCreate = false; throw new Error('Only a single offscreen document may be created.'); }
      // Creation is not instantaneous; this is where the original race lived.
      await new Promise((r) => setTimeout(r, 20));
      offscreenDocs++;
    },
  },
  contextMenus: {
    removeAll: (cb) => { menus.clear(); cb && cb(); },
    create: (o) => menus.set(o.id, { ...o }),
    update: async (id, patch) => {
      if (!menus.has(id)) throw new Error('no such menu item');
      Object.assign(menus.get(id), patch);
    },
    onClicked: { addListener: (fn) => { onMenuClicked = fn; } },
  },
  commands: { onCommand: { addListener: (fn) => { onCommand = fn; } } },
  action: { onClicked: { addListener: () => {} }, openOptionsPage: () => {} },
  scripting: {
    insertCSS: async () => {},
    // The injected functions touch document/window, so they cannot be run here.
    // Distinguish them by source and hand back the shape each one returns.
    executeScript: async ({ func }) => {
      const src = String(func ?? '');
      if (src.includes('getSelection')) return [{ result: 'selected text from the page' }];
      return [{ result: { title: 'A Title: With / Slashes?', text: 'Whole article body text.' } }];
    },
  },
  tabs: {
    query: async () => [{ id: 7 }],
    sendMessage: async (tabId, msg) => { sentToTabs.push({ tabId, msg }); },
  },
  storage: { sync: { get: async (d) => d, set: async () => {} } },
  downloads: {
    download: async (opts) => { downloaded.push(opts); return downloaded.length; },
    onChanged: { addListener: (fn) => { onDownloadChanged = fn; } },
  },
  permissions: { request: async () => true },
};

// Swallow the telemetry POSTs; a live server is not required for this harness.
globalThis.fetch = async (_url, init) => {
  try { fetched.push(JSON.parse(init?.body ?? '{}')); } catch { fetched.push({}); }
  return { ok: true, json: async () => ({}) };
};

const bg = await import('../extension/background.js');
onInstalled();

const settle = (ms = 80) => new Promise((r) => setTimeout(r, ms));
const stopItem = () => menus.get('stop-speaking');

let failures = 0;
async function check(name, fn) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failures++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}

console.log('=== background: stop speaking ===');

await check('menu registers a hidden Stop speaking item', () => {
  assert.ok(stopItem(), 'stop-speaking menu item was never created');
  assert.equal(stopItem().visible, false, 'stop item should start hidden');
  assert.equal(stopItem().title, 'Stop speaking');
});

await check('isSpeaking covers exactly the active states', () => {
  for (const s of ['preparing', 'playing', 'paused']) assert.ok(bg.isSpeaking(s), s);
  for (const s of ['idle', 'ended', 'error', undefined]) assert.ok(!bg.isSpeaking(s), String(s));
});

await check('speaking reveals the Stop item', async () => {
  await onMenuClicked({ menuItemId: 'speak-selection', selectionText: 'Hello there world.' }, { id: 7 });
  await settle();
  assert.equal(stopItem().visible, true, 'stop item stayed hidden while speaking');
  assert.ok(sentToOffscreen.some((m) => m.type === 'play'), 'play was never dispatched');
});

await check('concurrent offscreen creation does not double-create', () => {
  // The original bug: injecting the UI triggers a status message whose
  // ensureOffscreen raced the play path's, and the second createDocument threw.
  assert.equal(createCalls, 1, `createDocument called ${createCalls} times, expected 1`);
});

await check('Stop menu item dispatches stop', async () => {
  sentToOffscreen.length = 0;
  await onMenuClicked({ menuItemId: 'stop-speaking' }, { id: 7 });
  await settle();
  assert.ok(sentToOffscreen.some((m) => m.type === 'stop'), 'stop was never dispatched');
});

await check('stopping hides the Stop item again', () => {
  assert.equal(stopItem().visible, false, 'stop item remained visible after stopping');
});

await check('stopping tells the page UI to go idle', () => {
  assert.ok(sentToTabs.some((t) => t.msg.state === 'idle'), 'UI was never told it went idle');
});

await check('the keyboard shortcut stops too', async () => {
  await onMenuClicked({ menuItemId: 'speak-selection', selectionText: 'More text to speak.' }, { id: 7 });
  await settle();
  sentToOffscreen.length = 0;
  await onCommand('stop-speaking');
  await settle();
  assert.ok(sentToOffscreen.some((m) => m.type === 'stop'), 'shortcut did not dispatch stop');
  assert.equal(stopItem().visible, false);
});

await check('stop with nothing playing does not create a player', async () => {
  offscreenDocs = 0;
  const before = createCalls;
  sentToOffscreen.length = 0;
  await onCommand('stop-speaking');
  await settle();
  assert.equal(createCalls, before, 'stop spun up an offscreen document for nothing');
  assert.equal(sentToOffscreen.length, 0, 'stop dispatched a message with no player');
});

await check('player status updates drive the menu item', async () => {
  onMessage({ target: 'background', from: 'offscreen', state: 'playing', index: 0, total: 3 }, {}, () => {});
  await settle();
  assert.equal(stopItem().visible, true, 'playing status did not reveal the stop item');
  onMessage({ target: 'background', from: 'offscreen', state: 'ended', index: 2, total: 3 }, {}, () => {});
  await settle();
  assert.equal(stopItem().visible, false, 'ended status did not hide the stop item');
});

console.log('=== background: download the whole article ===');

await check('menu offers an article download beside Speak this article', () => {
  const item = menus.get('download-page');
  assert.ok(item, 'download-page menu item was never created');
  assert.equal(item.title, 'Download this article as MP3');
  assert.deepEqual(item.contexts, ['page'], 'article download must show without a selection');
  assert.deepEqual(menus.get('speak-page').contexts, ['page'], 'peer item changed context');
});

await check('mp3Filename strips characters chrome.downloads rejects', () => {
  assert.equal(bg.mp3Filename('A Title: With / Slashes?'), 'A Title With Slashes.mp3');
  assert.equal(bg.mp3Filename('  ..trailing dots.. '), 'trailing dots.mp3');
  assert.equal(bg.mp3Filename('tab\there'), 'tab here.mp3');
  assert.ok(bg.mp3Filename('x'.repeat(400)).length <= 124, 'filename not truncated');
});

await check('mp3Filename falls back when there is no usable title', () => {
  for (const t of ['', '   ', '///', null, undefined]) {
    assert.equal(bg.mp3Filename(t), 'article.mp3', `bad fallback for ${JSON.stringify(t)}`);
  }
});

await check('article download is handed to Chrome, not fetched into the extension', async () => {
  offscreenDocs = 1;
  downloaded.length = 0;
  sentToOffscreen.length = 0;
  await onMenuClicked({ menuItemId: 'download-page' }, { id: 7 });
  await settle();
  assert.equal(downloaded.length, 1, 'chrome.downloads.download was never called');
  const d = downloaded[0];
  assert.equal(d.filename, 'A Title With Slashes.mp3', 'settings spread clobbered the filename');
  assert.equal(d.saveAs, false, 'a Save dialog looks like nothing happening');
  assert.match(d.url, /\/v1\/export$/, `posted to ${d.url}`);
  assert.equal(d.method, 'POST');
  assert.equal(JSON.parse(d.body).text, 'Whole article body text.');
  assert.deepEqual(d.headers, [{ name: 'Content-Type', value: 'application/json' }]);
  // The whole point of the change: the offscreen document, which Chrome closes
  // after 30s without audio, is not involved in an export at all.
  assert.ok(!sentToOffscreen.some((m) => m.type === 'export'), 'export still went via offscreen');
});

await check('selection download uses the same path and the generic name', async () => {
  downloaded.length = 0;
  await onMenuClicked({ menuItemId: 'download-selection', selectionText: 'Just this bit.' }, { id: 7 });
  await settle();
  assert.equal(downloaded.length, 1, 'chrome.downloads.download was never called');
  assert.equal(downloaded[0].filename, 'article.mp3');
  assert.equal(JSON.parse(downloaded[0].body).text, 'Just this bit.');
});

await check('a download never shows the player bar', () => {
  // It would have nothing to clear it: the export completes in the browser,
  // possibly after this worker has been terminated.
  assert.ok(!sentToTabs.some((t) => t.msg.state === 'exporting'), 'stale exporting status');
});

await check('download outcomes are reported from the onChanged event', async () => {
  assert.ok(onDownloadChanged, 'no downloads.onChanged listener was registered');
  const before = fetched.length;
  onDownloadChanged({ id: 1, state: { current: 'interrupted' }, error: { current: 'SERVER_FAILED' } });
  onDownloadChanged({ id: 2, state: { current: 'complete' } });
  await settle();
  assert.ok(fetched.length > before, 'neither outcome was reported');
});

console.log(failures ? `\n${failures} FAILED` : '\nall background checks passed');
process.exit(failures ? 1 : 0);
