// Service worker: the only part that talks to the tiko server. It sends one
// captured profile at a time to POST /pool/capture using the connection code
// the user pasted into the popup.
'use strict';

async function settings() {
    return chrome.storage.local.get(['api', 'token', 'auto']);
}

function badge(text, color) {
    chrome.action.setBadgeText({ text });
    if (color) chrome.action.setBadgeBackgroundColor({ color });
    if (text) setTimeout(() => chrome.action.setBadgeText({ text: '' }), 4000);
}

async function capture(profile) {
    const { api, token } = await settings();
    if (!api || !token) return { ok: false, error: 'not-connected' };
    let res;
    try {
        res = await fetch(api.replace(/\/+$/, '') + '/pool/capture', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
            body: JSON.stringify(profile),
        });
    } catch (_) {
        return { ok: false, error: 'network' };
    }
    let body = {};
    try { body = await res.json(); } catch (_) { /* not JSON */ }
    if (res.status === 401) return { ok: false, error: 'bad-code' };
    if (!res.ok) return { ok: false, error: 'server', detail: typeof body.detail === 'string' ? body.detail : '' };
    const result = { ok: true, status: body.status, handle: profile.handle, followers: profile.followers, at: Date.now() };
    chrome.storage.local.set({ last: result });
    return result;
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
    if (!msg || msg.type !== 'tiko-capture' || !msg.profile) return;
    capture(msg.profile).then(r => {
        if (r.ok) badge('✓', '#0A8F8A');
        else if (msg.manual || r.error === 'bad-code') badge('!', '#C92A2A');
        respond(r);
    });
    return true;                                         // answer asynchronously
});
