// Runs on TikTok pages. When auto-capture is on, sends the creator profile the
// user is viewing to the background worker and shows a small confirmation in
// the corner; the popup can also ask for a capture on demand. Never touches
// anything except the current profile page.
(function () {
    'use strict';
    const REPEAT_AFTER_MS = 6 * 60 * 60 * 1000;      // don't re-send the same profile for 6 hours
    // Same language as the popup: the user's pick, else their Lumio account's, else the browser's.
    let zh = (navigator.language || '').toLowerCase().startsWith('zh');
    try {
        chrome.storage.local.get(['lang', 'siteLang'], v => { const l = (v && (v.lang || v.siteLang)) || ''; if (l) zh = l === 'zh'; });
        chrome.storage.onChanged.addListener(ch => { if (ch.lang || ch.siteLang) chrome.storage.local.get(['lang', 'siteLang'], v => { const l = v.lang || v.siteLang; if (l) zh = l === 'zh'; }); });
    } catch (_) { /* extension reloaded underneath the page */ }
    let lastPath = null, timer = null;

    function read() { return LumioExtract.extractProfile(document, location.pathname); }

    function send(profile, manual) {
        return new Promise(resolve => {
            try { chrome.runtime.sendMessage({ type: 'lumio-capture', profile, manual: !!manual }, r => { void chrome.runtime.lastError; resolve(r || { ok: false }); }); }
            catch (_) { resolve({ ok: false }); }        // extension was reloaded; the page still has the old script
        });
    }

    // A small toast in a shadow root, so the page's styles can't touch it.
    function toast(text, sub) {
        let host = document.getElementById('lumio-capture-toast');
        if (!host) {
            host = document.createElement('div');
            host.id = 'lumio-capture-toast';
            host.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:2147483647;pointer-events:none';
            host.attachShadow({ mode: 'open' });
            document.documentElement.appendChild(host);
        }
        const root = host.shadowRoot;
        root.innerHTML = `<style>
            .t{display:flex;align-items:center;gap:10px;padding:10px 14px 10px 10px;border-radius:14px;background:rgba(8,11,24,.92);color:#EEF1F8;
               font:600 13px/1.35 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;border:1px solid rgba(185,200,240,.28);
               box-shadow:0 14px 40px -12px rgba(0,0,0,.55);backdrop-filter:blur(10px);transform:translateY(12px);opacity:0;transition:all .35s cubic-bezier(.2,.8,.2,1)}
            .t.on{transform:none;opacity:1}.i{width:26px;height:26px;border-radius:8px;background:#0B1530;display:grid;place-items:center;flex-shrink:0}
            small{display:block;font-weight:500;color:#A9B4D0;font-size:11.5px}</style>
            <div class="t"><span class="i"><svg width="16" height="16" viewBox="0 0 40 40"><circle cx="20" cy="20" r="11" fill="none" stroke="#C9D2EA" stroke-width="3"/><path d="M28 7Q28 13 34 13Q28 13 28 19Q28 13 22 13Q28 13 28 7Z" fill="#fff"/></svg></span>
            <div><span class="m"></span><small class="s"></small></div></div>`;
        root.querySelector('.m').textContent = text;
        root.querySelector('.s').textContent = sub || '';
        const t = root.querySelector('.t');
        requestAnimationFrame(() => t.classList.add('on'));
        clearTimeout(host._h);
        host._h = setTimeout(() => t.classList.remove('on'), 2600);
    }

    async function autoCapture() {
        if (!LumioExtract.isProfilePath(location.pathname)) return;
        const { auto = true, toastOn = true, token } = await chrome.storage.local.get(['auto', 'toastOn', 'token']);
        if (!auto || !token) return;
        const profile = read();
        if (!profile) return;
        const key = 'seen:' + profile.handle.toLowerCase();
        const seen = (await chrome.storage.local.get(key))[key];
        if (seen && Date.now() - seen < REPEAT_AFTER_MS) return;
        const res = await send(profile, false);
        if (res.ok) {
            chrome.storage.local.set({ [key]: Date.now() });
            if (toastOn) toast(zh ? `已加入 Lumio 达人池：@${profile.handle}` : `Added to Lumio: @${profile.handle}`,
                               res.status === 'created' ? (zh ? '新达人' : 'New creator') : (zh ? '已更新数据' : 'Stats updated'));
        } else if (res.retry && toastOn) {
            toast(zh ? '暂时连不上 Lumio' : 'Lumio is unreachable right now', zh ? '已保存，稍后自动重试' : 'Saved — it will retry automatically');
        }
    }

    // The site is a single-page app: watch for the address changing, then give
    // the new profile a moment to render before reading it.
    function schedule() {
        if (location.pathname === lastPath) return;
        lastPath = location.pathname;
        clearTimeout(timer);
        timer = setTimeout(autoCapture, 2500);
    }
    setInterval(schedule, 1000);
    schedule();

    // Popup -> "what is on this page?" / "capture it now"
    chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
        if (msg && msg.type === 'lumio-read') { respond({ profile: read() }); return; }
        if (msg && msg.type === 'lumio-capture-now') {
            const profile = read();
            if (!profile) { respond({ ok: false, error: 'no-profile' }); return; }
            send(profile, true).then(r => {
                if (r.ok) chrome.storage.local.set({ ['seen:' + profile.handle.toLowerCase()]: Date.now() });
                respond(r);
            });
            return true;                                 // answer asynchronously
        }
    });
})();
