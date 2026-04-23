// ==UserScript==
// @name         Quick Highlighter
// @namespace    http://local/
// @version      0.1
// @match        https://*/*
// @grant        none
// @run-at       document-end
// ==/UserScript==
(function(){
  'use strict';
  const map = {
    Header: 'header, h1, h2, h3, [role="banner"]',
    Navigation: 'nav, [role="navigation"], .navbar, .nav',
    Main: 'main, [role="main"], .content, #content',
    Sidebar: 'aside, [role="complementary"], .sidebar',
    Actions: 'button, a.button, .btn, [role="button"]',
    Forms: 'form, input, textarea, select'
  };

  const css = `
    .tm-highlight { outline: 3px dashed rgba(255,165,0,0.95); position: relative; box-shadow: 0 0 0 3px rgba(255,165,0,0.06); }
    .tm-badge { position: absolute; left: 6px; top: 6px; background: #ff9800; color: #111; font-weight:700; padding:2px 6px; border-radius:4px; font-size:12px; z-index:2147483647; opacity:0.95; pointer-events:none; }
  `;
  const style = document.createElement('style'); style.textContent = css; document.head.appendChild(style);

  function highlightAll() {
    // remove previous
    document.querySelectorAll('.tm-highlight').forEach(el => {
      el.classList.remove('tm-highlight');
      const b = el.querySelector(':scope > .tm-badge'); if (b) b.remove();
      if (el.dataset.tmOriginalPosition) { el.style.position = el.dataset.tmOriginalPosition; delete el.dataset.tmOriginalPosition; }
    });
    for (const [label, sel] of Object.entries(map)) {
      const nodes = document.querySelectorAll(sel);
      nodes.forEach((el, i) => {
        // avoid overlaying on tiny elements
        if (el.offsetWidth < 20 || el.offsetHeight < 10) return;
        // ensure positioned container for badge
        const pos = getComputedStyle(el).position;
        if (pos === 'static') { el.dataset.tmOriginalPosition = el.style.position || ''; el.style.position = 'relative'; }
        el.classList.add('tm-highlight');
        const badge = document.createElement('div');
        badge.className = 'tm-badge';
        badge.textContent = label + (nodes.length>1?` (${i+1})`:'');
        el.prepend(badge);
      });
    }
  }

  // initial highlight after load + re-run on DOM updates
  highlightAll();
  const obs = new MutationObserver(() => { highlightAll(); });
  obs.observe(document.documentElement, { childList:true, subtree:true, attributes:false });

  // quick toggle via Alt+H
  let enabled = true;
  window.addEventListener('keydown', (e)=>{
    if (e.altKey && e.key.toLowerCase() === 'h') { enabled = !enabled; document.querySelectorAll('.tm-highlight').forEach(el => el.style.display = enabled ? '' : 'none'); }
    if (e.altKey && e.key.toLowerCase() === 's') { // Alt+S: prompt to add selector
      const label = prompt('Label for selector:','Custom');
      const sel = prompt('CSS selector to highlight:','.my-class');
      if (label && sel) { map[label] = sel; highlightAll(); }
    }
  });

  console.log('Quick Highlighter active — Alt+H toggles highlights, Alt+S to add selector.');
})();
