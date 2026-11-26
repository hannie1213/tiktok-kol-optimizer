'use strict';
// Show the guide in the same language as the popup (the user's pick, else their
// Lumio account's, else the browser's); the button flips it and remembers the choice.
const root = document.documentElement;
root.lang = (navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en';
try {
    chrome.storage.local.get(['lang', 'siteLang'], v => { const l = (v && (v.lang || v.siteLang)) || ''; if (l === 'zh' || l === 'en') root.lang = l; });
} catch (_) { /* opened outside the extension */ }
document.getElementById('switch').addEventListener('click', () => {
    root.lang = root.lang === 'zh' ? 'en' : 'zh';
    try { chrome.storage.local.set({ lang: root.lang }); } catch (_) { /* opened outside the extension */ }
});
