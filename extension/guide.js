'use strict';
// Show the guide in the browser's language; the button flips between Chinese and English.
const root = document.documentElement;
root.lang = (navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en';
document.getElementById('switch').addEventListener('click', () => { root.lang = root.lang === 'zh' ? 'en' : 'zh'; });
