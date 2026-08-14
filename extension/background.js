// Service worker: menus, hotkeys, and routing. It deliberately owns no playback
// state - MV3 terminates this worker whenever it feels like it, so the audio and
// the position live in the offscreen document instead.
import { getSettings } from './settings.js';

const OFFSCREEN = 'offscreen.html';

// The service worker's console is only visible on chrome://extensions, so the
// whole chain reports to the server instead, where it can be read alongside the
// requests it explains.
async function report(event, extra = {}) {
  try {
    const { server } = await getSettings();
    await fetch(`${server}/v1/diag`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ where: 'background', event, ...extra }),
    });
  } catch { /* telemetry must never break the feature */ }
}

// Which tab is showing the player UI. Re-derived on demand; losing it to a
// worker restart costs nothing worse than a missing progress update.
let uiTabId = null;

// States in which "Stop speaking" is a meaningful thing to offer.
const SPEAKING_STATES = new Set(['preparing', 'playing', 'paused']);
export const isSpeaking = (state) => SPEAKING_STATES.has(state);

async function setStopVisible(visible) {
  try {
    await chrome.contextMenus.update('stop-speaking', { visible });
  } catch {
    // The menu item does not exist yet on a freshly installed worker.
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: 'speak-selection',
      title: 'Speak selection',
      contexts: ['selection'],
    });
    chrome.contextMenus.create({
      id: 'speak-page',
      title: 'Speak this article',
      contexts: ['page'],
    });
    chrome.contextMenus.create({
      id: 'download-selection',
      title: 'Download selection as MP3',
      contexts: ['selection'],
    });
    chrome.contextMenus.create({
      id: 'stop-speaking',
      title: 'Stop speaking',
      contexts: ['all'],
      visible: false,
    });
  });
});

let creating = null;   // de-duplicates concurrent createDocument calls

async function offscreenExists() {
  const existing = await chrome.runtime.getContexts({
    contextTypes: ['OFFSCREEN_DOCUMENT'],
    documentUrls: [chrome.runtime.getURL(OFFSCREEN)],
  });
  return existing.length > 0;
}

async function ensureOffscreen() {
  if (await offscreenExists()) return 'existed';
  if (creating) { await creating; return 'awaited'; }
  creating = chrome.offscreen.createDocument({
    url: OFFSCREEN,
    reasons: ['AUDIO_PLAYBACK'],
    justification: 'Plays synthesized speech so it survives page navigation.',
  });
  try {
    await creating;
    return 'created';
  } finally {
    creating = null;
  }
}

async function toOffscreen(message) {
  let how;
  try {
    how = await ensureOffscreen();
  } catch (e) {
    report('offscreen.create.failed', { message: String(e) });
    throw e;
  }
  try {
    const reply = await chrome.runtime.sendMessage({ ...message, target: 'offscreen' });
    report('sent', { type: message.type, offscreen: how, reply: JSON.stringify(reply ?? null) });
    return reply;
  } catch (e) {
    // Almost always "receiving end does not exist": the offscreen document was
    // created but its module had not registered its listener yet.
    report('send.failed', { type: message.type, offscreen: how, message: String(e) });
    throw e;
  }
}

async function showPlayer(tabId) {
  uiTabId = tabId;
  try {
    await chrome.scripting.insertCSS({ target: { tabId }, files: ['player.css'] });
    await chrome.scripting.executeScript({ target: { tabId }, files: ['player.js'] });
  } catch (e) {
    // Injection is blocked on chrome:// pages and the Web Store. Audio still
    // plays; the user just does not get the on-page controls.
    console.warn('TextReader: could not inject player UI', e);
  }
}

function toUi(message) {
  if (uiTabId == null) return;
  chrome.tabs.sendMessage(uiTabId, { ...message, target: 'ui' }).catch(() => {
    // Tab closed or navigated before the content script re-attached.
    uiTabId = null;
  });
}

async function speak(text, tabId) {
  report('speak', { chars: text?.length ?? 0 });
  if (!text || !text.trim()) return;
  try {
    const settings = await getSettings();
    await setStopVisible(true);
    await showPlayer(tabId);
    toUi({ type: 'status', state: 'preparing' });
    await toOffscreen({ type: 'play', text, ...settings });
  } catch (e) {
    report('speak.failed', { message: String(e) });
    toUi({ type: 'status', state: 'error', message: String(e) });
  }
}

async function stopSpeaking() {
  report('stop');
  // Nothing to stop, and no reason to spin up a player just to shut it down.
  if (!(await offscreenExists())) {
    await setStopVisible(false);
    return;
  }
  try {
    await toOffscreen({ type: 'stop' });
  } catch (e) {
    report('stop.failed', { message: String(e) });
  }
  await setStopVisible(false);
  toUi({ type: 'status', state: 'idle' });
}

async function getSelectionFromTab(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => String(window.getSelection() || '').trim(),
  });
  return result || '';
}

async function extractArticle(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      // Pick the subtree with the most paragraph text - a small stand-in for
      // Readability that avoids bundling a library into the extension.
      const candidates = [
        ...document.querySelectorAll('article, main, [role="main"], .post, .article, #content'),
        document.body,
      ];
      const score = (el) =>
        [...el.querySelectorAll('p')].reduce((n, p) => n + p.innerText.trim().length, 0);
      const best = candidates.filter(Boolean).sort((a, b) => score(b) - score(a))[0];
      if (!best) return '';
      const skip = 'nav,header,footer,aside,script,style,form,button,figure figcaption,.ad,.advert';
      const clone = best.cloneNode(true);
      clone.querySelectorAll(skip).forEach((n) => n.remove());
      const heading = document.querySelector('h1');
      const body = [...clone.querySelectorAll('h2, h3, p, li')]
        .map((n) => n.innerText.trim())
        .filter((t) => t.length > 2)
        .join('\n\n');
      return [heading ? heading.innerText.trim() : '', body].filter(Boolean).join('\n\n');
    },
  });
  return result || '';
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  report('menu.clicked', { item: info.menuItemId, hasTab: !!tab?.id });
  if (!tab?.id) return;
  if (info.menuItemId === 'speak-selection') {
    await speak(info.selectionText || (await getSelectionFromTab(tab.id)), tab.id);
  } else if (info.menuItemId === 'speak-page') {
    await speak(await extractArticle(tab.id), tab.id);
  } else if (info.menuItemId === 'stop-speaking') {
    await stopSpeaking();
  } else if (info.menuItemId === 'download-selection') {
    await download(info.selectionText || (await getSelectionFromTab(tab.id)));
  }
});

chrome.commands.onCommand.addListener(async (command) => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (command === 'speak-selection') {
    if (!tab?.id) return;
    await speak(await getSelectionFromTab(tab.id), tab.id);
  } else if (command === 'toggle-playback') {
    await toOffscreen({ type: 'toggle' });
  } else if (command === 'stop-speaking') {
    await stopSpeaking();
  }
});

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());

async function download(text) {
  report('download.requested', { chars: text?.length ?? 0 });
  if (!text?.trim()) return;
  const settings = await getSettings();
  // The blob URL has to be minted in a DOM context; a service worker cannot
  // create one, and chrome.downloads rejects data: URLs from extensions.
  await toOffscreen({ type: 'export', text, ...settings });
}

// Messages from the offscreen player (status) and the on-page UI (commands).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== 'background') return false;
  if (msg.from === 'offscreen') {
    toUi({ type: 'status', ...msg });
    setStopVisible(isSpeaking(msg.state));
    return false;
  }
  // From the page UI. Re-learn which tab it is in: this worker may have been
  // restarted since the UI was injected, which would otherwise leave uiTabId
  // null and silently drop every status update.
  if (sender.tab?.id != null) uiTabId = sender.tab.id;
  toOffscreen(msg).then((r) => sendResponse(r ?? {})).catch(() => sendResponse({}));
  return true;
});
