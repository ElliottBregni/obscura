// Content script: relays page messages to the extension background and returns responses
// Also injects a page-level bootstrap so sites don't need Tampermonkey.
(function injectPageBridge(){
  try{
    const src = chrome.runtime.getURL('src/tools/extbridge.user.js');
    const s = document.createElement('script');
    s.src = src;
    s.onload = () => s.remove();
    (document.head || document.documentElement).appendChild(s);
  }catch(e){
    // Best-effort; if injection fails, the userscript can be installed via Tampermonkey.
    console.debug('extbridge injection failed', e);
  }
})();

const PAGE_MARK = 'ExtBridge';
const RESPONSE_MARK = 'ExtBridgeResponse';
const WHITELIST = new Set(['ping','getToken','batch','stream','handshake-init']);
const MAX_PAYLOAD = 200_000; // bytes

window.addEventListener('message', (e) => {
  const d = e.data;
  if (!d || d.__from !== PAGE_MARK) return;

  // Basic validation
  if (typeof d.cmd !== 'string' || d.cmd.length > 64) {
    window.postMessage({ __to: PAGE_MARK, id: d.id, error: 'invalid-cmd' }, '*');
    return;
  }
  if (!WHITELIST.has(d.cmd)) {
    window.postMessage({ __to: PAGE_MARK, id: d.id, error: 'cmd-not-allowed' }, '*');
    return;
  }

  try {
    const size = new Blob([JSON.stringify(d.payload || {})]).size;
    if (size > MAX_PAYLOAD) {
      window.postMessage({ __to: PAGE_MARK, id: d.id, error: 'payload-too-large' }, '*');
      return;
    }
  } catch (err) {
    // ignore size check failure
  }

  // Forward to background and send response back to page when ready
  // Forward signature (if present) and handshake id
  chrome.runtime.sendMessage({ from: 'page-bridge', cmd: d.cmd, id: d.id, payload: d.payload, handshake: d.handshake, signature: d.signature }, (resp) => {
    const out = { __to: PAGE_MARK, id: d.id };
    if (chrome.runtime.lastError) out.error = chrome.runtime.lastError.message;
    else if (!resp) out.error = 'no-response';
    else if (resp.error) out.error = resp.error;
    else out.result = resp.result;
    window.postMessage(out, '*');
  });
});

// Listen for stream messages from background and forward them into the page.
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || msg.from !== 'background-stream') return;
  // Forward incremental chunks into the page; page can listen with ExtBridge.on()
  const out = { __to: PAGE_MARK, id: msg.id, stream: true, chunk: msg.chunk, done: !!msg.done };
  window.postMessage(out, '*');
});
