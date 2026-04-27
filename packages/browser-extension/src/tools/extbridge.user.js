// ==UserScript==
// @name         ExtBridge Page Injector
// @namespace    http://local.obscura/
// @version      0.4
// @match        https://*/*
// @grant        none
// @run-at       document-start
// ==/UserScript==
//
// This userscript is a FALLBACK for pages where the Obscura extension's
// content script can't reach (chrome://, some restricted PDFs, etc.).  On
// every normal https:// page the extension installs `window.ExtBridge`
// itself via a MAIN-world content script, and this userscript no-ops.
//
// If you have the Obscura extension installed, the userscript is optional
// and you can disable it in Tampermonkey.  If you keep it installed, make
// sure you've imported v0.4+ — older revisions used a `<script>`-injection
// trick that fires CSP violations on strict sites (GitHub, Office, AWS).
(function () {
  'use strict';

  // If the extension already installed the bridge, get out of its way.
  if (window.ExtBridge) return;

  const ALLOWED_SIZE = 200_000; // bytes
  const DEFAULT_TIMEOUT = 15_000; // ms

  const pending = new Map();
  let _hmacKey = null;
  const allowed = (cmd) => typeof cmd === 'string' && cmd.length < 64;

  const handshake = crypto.randomUUID();

  async function initHandshake() {
    const pair = await crypto.subtle.generateKey(
      { name: 'ECDH', namedCurve: 'P-256' },
      true,
      ['deriveBits'],
    );
    const rawPub = await crypto.subtle.exportKey('raw', pair.publicKey);
    const pubB64 = btoa(String.fromCharCode(...new Uint8Array(rawPub)));

    const resp = await window.ExtBridge.send(
      'handshake-init',
      { pubkey: pubB64 },
      { timeout: 15_000 },
    );
    if (!resp || !resp.backgroundPubKey) throw new Error('no-background-pub');

    const otherBuf = Uint8Array.from(atob(resp.backgroundPubKey), (c) => c.charCodeAt(0)).buffer;
    const otherPub = await crypto.subtle.importKey(
      'raw',
      otherBuf,
      { name: 'ECDH', namedCurve: 'P-256' },
      true,
      [],
    );
    const bits = await crypto.subtle.deriveBits(
      { name: 'ECDH', public: otherPub },
      pair.privateKey,
      256,
    );
    _hmacKey = await crypto.subtle.importKey(
      'raw',
      bits,
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign'],
    );
    return true;
  }

  async function signMessage(id, cmd, payload) {
    if (!_hmacKey) return null;
    const canon = id + '|' + cmd + '|' + JSON.stringify(payload || {});
    const sig = await crypto.subtle.sign(
      'HMAC',
      _hmacKey,
      new TextEncoder().encode(canon),
    );
    return btoa(String.fromCharCode(...new Uint8Array(sig)));
  }

  window.ExtBridge = {
    handshake,
    initHandshake,
    async send(cmd, payload, opts) {
      const timeout = (opts && opts.timeout) || DEFAULT_TIMEOUT;
      if (!allowed(cmd)) return Promise.reject(new Error('invalid-cmd'));

      try {
        const size = new Blob([JSON.stringify({ cmd, payload })]).size;
        if (size > ALLOWED_SIZE) return Promise.reject(new Error('payload-too-large'));
      } catch { /* ignore */ }

      const id = crypto.randomUUID();
      let signature = null;
      try { signature = await signMessage(id, cmd, payload); } catch { /* ignore */ }

      window.postMessage(
        { __from: 'ExtBridge', cmd, payload, id, handshake, signature },
        '*',
      );
      return new Promise((resolve, reject) => {
        const to = setTimeout(() => {
          pending.delete(id);
          reject(new Error('timeout'));
        }, timeout);
        pending.set(id, { resolve, reject, to });
      });
    },
    on(cb) {
      if (typeof cb !== 'function') return;
      window.addEventListener('message', (e) => {
        const d = e.data;
        if (!d || d.__to !== 'ExtBridge' || !d.stream) return;
        cb(d);
      });
    },
  };

  window.addEventListener('message', (ev) => {
    const d = ev.data;
    if (!d || d.__to !== 'ExtBridge' || d.stream) return;
    const p = pending.get(d.id);
    if (!p) return;
    clearTimeout(p.to);
    pending.delete(d.id);
    if (d.error) p.reject(new Error(d.error));
    else p.resolve(d.result);
  });
})();
