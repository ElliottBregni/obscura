// ==UserScript==
// @name         ExtBridge Page Injector (Robust)
// @namespace    http://local.obscura/
// @version      0.2
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
    const allowed = (cmd) => typeof cmd === 'string' && cmd.length < 64;

    // handshake id for ephemeral binding
    const handshake = crypto.randomUUID();

    window.ExtBridge = {
      handshake,
      send(cmd, payload, opts = { timeout: ${DEFAULT_TIMEOUT} }){
        if(!allowed(cmd)) return Promise.reject('invalid-cmd');
        const id = crypto.randomUUID();
        const msg = { __from: 'ExtBridge', cmd, payload, id, handshake };
        try{
          const s = new Blob([JSON.stringify(msg)]);
          if (s.size > ${ALLOWED_SIZE}) return Promise.reject('payload-too-large');
        }catch(e){/* ignore */}
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
