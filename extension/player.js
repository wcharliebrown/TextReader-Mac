// On-page transport controls. Injected on demand, and everything visible lives
// in a shadow root so the host page's stylesheet cannot reach in and break it.
// Re-injection is a no-op: the guard below keeps a single instance per tab.
(() => {
  if (window.__textreaderPlayer) {
    window.__textreaderPlayer.show();
    return;
  }

  const HTML = `
    <style>
      :host { all: initial; }
      .box {
        font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
        display: flex; align-items: center; gap: 6px;
        background: #1c1c1e; color: #f2f2f7;
        border: 1px solid rgba(255,255,255,.14); border-radius: 12px;
        padding: 8px 10px; box-shadow: 0 8px 28px rgba(0,0,0,.38);
        user-select: none;
      }
      button {
        font: inherit; color: inherit; background: transparent; cursor: pointer;
        border: 0; border-radius: 8px; padding: 5px 7px; line-height: 1;
        min-width: 30px; display: grid; place-items: center;
      }
      button:hover { background: rgba(255,255,255,.12); }
      button:active { background: rgba(255,255,255,.2); }
      button:disabled { opacity: .35; cursor: default; background: transparent; }
      .play { font-size: 15px; }
      .pos { font-variant-numeric: tabular-nums; opacity: .65; padding: 0 4px; white-space: nowrap; }
      .rate { font-size: 12px; opacity: .8; min-width: 40px; }
      .sep { width: 1px; align-self: stretch; background: rgba(255,255,255,.14); margin: 2px 2px; }
      .msg { color: #ff9f9f; max-width: 15rem; }
      .hidden { display: none; }
      @media (prefers-color-scheme: light) {
        .box { background: #fff; color: #1c1c1e; border-color: rgba(0,0,0,.12);
               box-shadow: 0 8px 28px rgba(0,0,0,.16); }
        button:hover { background: rgba(0,0,0,.07); }
        button:active { background: rgba(0,0,0,.12); }
        .sep { background: rgba(0,0,0,.12); }
        .msg { color: #c00; }
      }
    </style>
    <div class="box">
      <button class="prev" title="Previous sentence">&#9664;&#9664;</button>
      <button class="play" title="Play / pause">&#9654;</button>
      <button class="next" title="Next sentence">&#9654;&#9654;</button>
      <button class="stop" title="Stop speaking">&#9632;</button>
      <span class="pos">-</span>
      <span class="sep"></span>
      <button class="rate" title="Playback speed">1.0&times;</button>
      <span class="sep"></span>
      <button class="close" title="Stop and close">&times;</button>
      <span class="msg hidden"></span>
    </div>`;

  const RATES = [0.8, 1.0, 1.25, 1.5, 1.75, 2.0];

  class PlayerUI {
    constructor() {
      this.host = document.createElement('div');
      this.host.id = 'textreader-player';
      this.root = this.host.attachShadow({ mode: 'closed' });
      this.root.innerHTML = HTML;
      // Start hidden: injection happens before the player reports a state, and
      // an empty control bar flashing up on every right-click looks broken.
      this.host.style.display = 'none';
      document.documentElement.append(this.host);

      this.$ = (sel) => this.root.querySelector(sel);
      this.state = 'idle';
      this.rate = 1;

      this.$('.play').onclick = () => this.send({ type: 'toggle' });
      this.$('.prev').onclick = () => this.send({ type: 'prev' });
      this.$('.next').onclick = () => this.send({ type: 'next' });
      this.$('.close').onclick = () => { this.send({ type: 'stop' }); this.hide(); };
      this.$('.stop').onclick = () => { this.send({ type: 'stop' }); this.hide(); };
      this.$('.rate').onclick = () => {
        const next = RATES[(RATES.indexOf(this.rate) + 1) % RATES.length] ?? 1;
        this.send({ type: 'rate', rate: next });
      };

      chrome.runtime.onMessage.addListener((msg) => {
        if (msg?.target === 'ui') this.render(msg);
        return false;
      });

      // The service worker may have been restarted since this UI was injected.
      this.send({ type: 'status' });
    }

    send(msg) {
      chrome.runtime.sendMessage({ ...msg, target: 'background' }).catch(() => {});
    }

    show() { this.host.style.display = ''; }
    hide() { this.host.style.display = 'none'; }

    render(s) {
      this.state = s.state ?? this.state;
      // Idle means stopped or never started - there is nothing to control, so
      // the bar gets out of the way. Every other state keeps it up, including
      // 'ended', where stepping back to re-hear a sentence is still useful.
      if (this.state === 'idle') {
        this.hide();
        return;
      }
      this.show();
      if (typeof s.rate === 'number') this.rate = s.rate;

      const playing = this.state === 'playing';
      this.$('.play').innerHTML = playing ? '&#10073;&#10073;' : '&#9654;';
      this.$('.rate').innerHTML = `${this.rate.toFixed(2).replace(/0$/, '')}&times;`;

      const msg = this.$('.msg');
      if (this.state === 'error') {
        msg.textContent = s.message || 'error';
        msg.classList.remove('hidden');
      } else {
        msg.classList.add('hidden');
      }

      const pos = this.$('.pos');
      if (this.state === 'preparing') pos.textContent = 'preparing...';
      else if (this.state === 'exporting') pos.textContent = 'encoding mp3...';
      else if (s.total) pos.textContent = `${(s.index ?? 0) + 1} / ${s.total}`;
      else pos.textContent = '-';

      // Exporting drives no transport - the whole file is rendered server-side.
      const idle = ['idle', 'error', 'exporting'].includes(this.state);
      for (const sel of ['.prev', '.next', '.play']) this.$(sel).disabled = idle;
    }
  }

  window.__textreaderPlayer = new PlayerUI();
})();
