// Runs on creator profile pages. When auto-capture is on, sends the profile
// the user is viewing to the background worker; the popup can also ask for a
// capture on demand. Never touches anything except the current profile page.
(function () {
    'use strict';
    const REPEAT_AFTER_MS = 6 * 60 * 60 * 1000;      // don't re-send the same profile for 6 hours
    let lastPath = null, timer = null;

    function read() { return TikoExtract.extractProfile(document, location.pathname); }

    function send(profile, manual) {
        return new Promise(resolve => {
            try { chrome.runtime.sendMessage({ type: 'tiko-capture', profile, manual: !!manual }, r => resolve(r || { ok: false })); }
            catch (_) { resolve({ ok: false }); }        // extension was reloaded; the page still has the old script
        });
    }

    async function autoCapture() {
        if (!TikoExtract.isProfilePath(location.pathname)) return;
        const { auto = true, token } = await chrome.storage.local.get(['auto', 'token']);
        if (!auto || !token) return;
        const profile = read();
        if (!profile) return;
        const key = 'seen:' + profile.handle.toLowerCase();
        const seen = (await chrome.storage.local.get(key))[key];
        if (seen && Date.now() - seen < REPEAT_AFTER_MS) return;
        const res = await send(profile, false);
        if (res.ok) chrome.storage.local.set({ [key]: Date.now() });
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
        if (msg && msg.type === 'tiko-read') { respond({ profile: read() }); return; }
        if (msg && msg.type === 'tiko-capture-now') {
            const profile = read();
            if (!profile) { respond({ ok: false, error: 'no-profile' }); return; }
            send(profile, true).then(respond);
            return true;                                 // answer asynchronously
        }
    });
})();
