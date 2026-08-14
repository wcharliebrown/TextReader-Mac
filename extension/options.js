import { DEFAULTS, getSettings, setSettings } from './settings.js';

const $ = (id) => document.getElementById(id);
const status = (text, cls) => { $('status').textContent = text; $('status').className = cls || ''; };

async function loadVoices(server, selected) {
  const select = $('voice');
  select.innerHTML = '';
  try {
    const res = await fetch(`${server}/v1/voices`);
    if (!res.ok) throw new Error(String(res.status));
    const { voices } = await res.json();
    // English first: those are the voices this is actually going to be used with.
    voices.sort((a, b) =>
      (b.locale.startsWith('en') - a.locale.startsWith('en')) || a.id.localeCompare(b.id));
    for (const v of voices) {
      const opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = `${v.name} — ${v.gender}, ${v.locale}  (${v.id})`;
      if (v.id === selected) opt.selected = true;
      select.append(opt);
    }
    return true;
  } catch {
    const opt = document.createElement('option');
    opt.value = selected;
    opt.textContent = `${selected} (server unreachable)`;
    select.append(opt);
    return false;
  }
}

async function checkHealth(server) {
  try {
    const res = await fetch(`${server}/healthz`);
    const h = await res.json();
    const mb = (h.cache?.bytes ?? 0) / 1024 ** 2;
    $('health').textContent =
      `connected — ${h.engine}, ${h.voices} voices, cache ${mb.toFixed(1)} MB`;
    $('health').className = 'hint ok';
  } catch {
    $('health').textContent = 'not reachable — is the server running?';
    $('health').className = 'hint bad';
  }
}

async function init() {
  const s = await getSettings();
  $('server').value = s.server;
  $('speed').value = s.speed;
  $('rate').value = s.rate;
  await Promise.all([loadVoices(s.server, s.voice), checkHealth(s.server)]);

  $('server').addEventListener('change', async () => {
    const server = $('server').value.trim().replace(/\/+$/, '');
    await checkHealth(server);
    await loadVoices(server, $('voice').value);
  });

  $('save').addEventListener('click', async () => {
    const server = $('server').value.trim().replace(/\/+$/, '') || DEFAULTS.server;
    // A server outside the manifest's host_permissions needs explicit consent
    // before the offscreen document is allowed to fetch from it.
    if (!/^https?:\/\/(127\.0\.0\.1|localhost):8842$/.test(server)) {
      const granted = await chrome.permissions.request({ origins: [`${server}/*`] })
        .catch(() => false);
      if (!granted) { status('permission for that address was declined', 'bad'); return; }
    }
    await setSettings({
      server,
      voice: $('voice').value,
      speed: parseFloat($('speed').value) || DEFAULTS.speed,
      rate: parseFloat($('rate').value) || DEFAULTS.rate,
    });
    status('saved', 'ok');
    setTimeout(() => status(''), 2000);
  });
}

init();
