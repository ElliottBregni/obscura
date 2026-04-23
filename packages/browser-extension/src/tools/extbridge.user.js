// ==UserScript==
// @name         ExtBridge Page Injector (Robust)
// @namespace    http://local.obscura/
// @version      0.3
// @match        https://*/*
// @grant        none
// @run-at       document-start
// ==/UserScript==
(function(){
  'use strict';
  const ALLOWED_SIZE = 200_000; // bytes
  const DEFAULT_TIMEOUT = 15_000; // ms

  const bootstrap = `(() => {
    if (window.ExtBridge) return;
    const pending = new Map();
    let _hmacKey = null; // CryptoKey for HMAC when handshake completes
    const allowed = (cmd) => typeof cmd === 'string' && cmd.length < 64;

    // handshake id for ephemeral binding
    const handshake = crypto.randomUUID();

    async function initHandshake() {
      // create ECDH pair
      const pair = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
      const rawPub = await crypto.subtle.exportKey('raw', pair.publicKey);
      const pubB64 = btoa(String.fromCharCode(...new Uint8Array(rawPub)));
      // send handshake-init to background via content script
      const resp = await window.ExtBridge.send('handshake-init', { pubkey: pubB64 }, { timeout: 15000 }).catch(e => { throw e; });
      if (!resp || !resp.backgroundPubKey) throw new Error('no-background-pub');
      const otherB64 = resp.backgroundPubKey;
      const otherBuf = Uint8Array.from(atob(otherB64), c => c.charCodeAt(0)).buffer;
      const otherPub = await crypto.subtle.importKey('raw', otherBuf, { name: 'ECDH', namedCurve: 'P-256' }, true, []);
      const bits = await crypto.subtle.deriveBits({ name: 'ECDH', public: otherPub }, pair.privateKey, 256);
      _hmacKey = await crypto.subtle.importKey('raw', bits, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
      return true;
    }

    async function signMessage(id, cmd, payload) {
      if (!_hmacKey) return null;
      const canon = id + '|' + cmd + '|' + JSON.stringify(payload || {});
      const sig = await crypto.subtle.sign('HMAC', _hmacKey, new TextEncoder().encode(canon));
      return btoa(String.fromCharCode(...new Uint8Array(sig)));
    }

    window.ExtBridge = {
      handshake,
      initHandshake,
      async send(cmd, payload, opts = { timeout: ${DEFAULT_TIMEOUT} }){
        if(!allowed(cmd)) return Promise.reject('invalid-cmd');
        const id = crypto.randomUUID();
        let signature = null;
        try{
          const s = new Blob([JSON.stringify({cmd,payload})]);
          if (s.size > ${ALLOWED_SIZE}) return Promise.reject('payload-too-large');
        }catch(e){/* ignore */}

        // sign if we have a key
        try { signature = await signMessage(id, cmd, payload); } catch (e) { /* ignore signing errors */ }

        const msg = { __from: 'ExtBridge', cmd, payload, id, handshake, signature };
        window.postMessage(msg,'*');
        return new Promise((resolve,reject)=>{
          const to = setTimeout(()=>{ pending.delete(id); reject('timeout'); }, opts.timeout);
          pending.set(id, { resolve, reject, to });
        });
      },
      on(cb){ if(typeof cb!=='function') return; window.addEventListener('message', e => { const d = e.data; if(!d||d.__to!=='ExtBridge') return; cb(d); }); }
    };

    window.addEventListener('message', ev => {
      const d = ev.data;
      if(!d || d.__from !== 'ExtBridgeResponse') return;
      const p = pending.get(d.id);
      if(!p) return;
      clearTimeout(p.to);
      pending.delete(d.id);
      if(d.error) p.reject(d.error); else p.resolve(d.result);
    });

    console.log('ExtBridge injected (handshake=' + handshake + ')');
  })();`;

  const s = document.createElement('script');
  s.textContent = bootstrap;
  (document.head || document.documentElement).appendChild(s);
  s.remove();
})();
