// Shared defaults. The service worker, offscreen document and options page all
// read settings through here so there is one place a default can be wrong.
export const DEFAULTS = {
  server: 'http://127.0.0.1:8842',
  voice: 'af_heart',
  speed: 1.0,
  rate: 1.0,          // playback rate applied client-side, independent of `speed`
  autoScroll: true,
};

export async function getSettings() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

export async function setSettings(patch) {
  await chrome.storage.sync.set(patch);
}
