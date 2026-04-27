// ==UserScript==
// @name         Selector Recorder
// @namespace    http://local/
// @version      0.1
// @match        https://*/*
// @grant        none
// @run-at       document-end
// ==/UserScript==
(function(){
  'use strict';
  let recording = false;
  window.__tm_selectors = window.__tm_selectors || [];

  function cssPath(el){
    if(!(el instanceof Element)) return '';
    const path = [];
    while(el && el.nodeType===1 && el.tagName.toLowerCase() !== 'html'){
      let selector = el.tagName.toLowerCase();
      if(el.id) selector += '#'+el.id;
      else{
        const cls = Array.from(el.classList||[]).filter(Boolean);
        if(cls.length) selector += '.'+cls.join('.');
        const sib = el.parentNode ? Array.from(el.parentNode.children).filter(c=>c.tagName===el.tagName) : [];
        if(sib.length>1){ selector += `:nth-of-type(${1+Array.prototype.indexOf.call(el.parentNode.children, el)})`; }
      }
      path.unshift(selector);
      el = el.parentElement;
    }
    return path.join(' > ');
  }

  function onClick(e){
    if(!recording) return;
    e.preventDefault(); e.stopPropagation();
    const sel = cssPath(e.target);
    window.__tm_selectors.push(sel);
    console.log('Selector recorded:', sel);
    // flash highlight
    const orig = e.target.style.outline;
    e.target.style.outline = '3px solid #4caf50';
    setTimeout(()=>{ e.target.style.outline = orig; }, 350);
  }

  window.addEventListener('keydown', (e)=>{
    if(e.altKey && e.key.toLowerCase()==='r'){
      recording = !recording;
      console.log('Selector recorder', recording ? 'enabled' : 'disabled');
      if(recording) document.addEventListener('click', onClick, true); else document.removeEventListener('click', onClick, true);
    }
    if(e.altKey && e.key.toLowerCase()==='p'){
      // print selectors
      console.log('Recorded selectors:', JSON.stringify(window.__tm_selectors, null, 2));
      alert('Recorded selectors copied to console');
    }
  });

  console.log('Selector Recorder loaded — Alt+R to toggle recording, Alt+P to print.');
})();
