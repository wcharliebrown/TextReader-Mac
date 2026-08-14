// Drives extension/background.js with stubbed chrome.* APIs. This covers the
// menu -> offscreen -> stop routing, which is exactly where the silent-failure
// bug lived, and which the offscreen harness cannot reach.
import assert from 'node:assert/strict';

const menus = new Map();
const sentToOffscreen = [];
const sentToTabs = [];
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
  downloads: { download: async () => 1 },
  permissions: { request: async () => true },
};

// Swallow the telemetry POSTs; a live server is not required for this harness.
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });

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

await check('article download exports the extracted text under the page title', async () => {
  offscreenDocs = 1;
  sentToOffscreen.length = 0;
  await onMenuClicked({ menuItemId: 'download-page' }, { id: 7 });
  await settle();
  const exp = sentToOffscreen.find((m) => m.type === 'export');
  assert.ok(exp, 'export was never dispatched');
  assert.equal(exp.text, 'Whole article body text.');
  assert.equal(exp.filename, 'A Title With Slashes.mp3', 'settings spread clobbered the filename');
});

await check('selection download still uses the generic name', async () => {
  sentToOffscreen.length = 0;
  await onMenuClicked({ menuItemId: 'download-selection', selectionText: 'Just this bit.' }, { id: 7 });
  await settle();
  const exp = sentToOffscreen.find((m) => m.type === 'export');
  assert.ok(exp, 'export was never dispatched');
  assert.equal(exp.text, 'Just this bit.');
  assert.equal(exp.filename, 'article.mp3');
});

await check('exporting does not pull Stop speaking out of the menu mid-playback', async () => {
  onMessage({ target: 'background', from: 'offscreen', state: 'playing', index: 0, total: 3 }, {}, () => {});
  await settle();
  assert.equal(stopItem().visible, true, 'precondition: should be speaking');
  onMessage({ target: 'background', from: 'offscreen', state: 'exporting' }, {}, () => {});
  await settle();
  assert.equal(stopItem().visible, true, 'an export hid the stop item while audio was playing');
  assert.ok(!bg.describesPlayback('exporting'));
  assert.ok(bg.describesPlayback('playing'));
});

await check('the exporting status still reaches the page UI', () => {
  assert.ok(sentToTabs.some((t) => t.msg.state === 'exporting'), 'UI never saw the export');
});

console.log(failures ? `\n${failures} FAILED` : '\nall background checks passed');
process.exit(failures ? 1 : 0);
