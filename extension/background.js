// Service worker: the only part that talks to the Lumio server. It sends one
// captured profile at a time to POST /pool/capture using the connection code
// the user pasted into the popup.
//
// Reliability: when the server can't be reached (offline, server restarting,
// rate limit), the profile goes into a small retry queue that is sent again
// every few minutes and right after the next successful capture. An expired
// code is remembered so the popup can ask the user to reconnect.
'use strict';

const QUEUE_MAX = 200;
const RETRY_MINUTES = 5;
const NAVY = '#1B2B5C', RED = '#B42318';

async function settings() {
    return chrome.storage.local.get(['api', 'token', 'auto']);
}

function badge(text, color) {
    chrome.action.setBadgeText({ text });
    if (color) chrome.action.setBadgeBackgroundColor({ color });
    if (text && text !== '!') setTimeout(() => chrome.action.setBadgeText({ text: '' }), 4000);
}

function today() { return new Date().toISOString().slice(0, 10); }

async function bumpStats() {
    const { stats = {} } = await chrome.storage.local.get('stats');
    const day = today();
    const next = { total: (stats.total || 0) + 1, day, today: stats.day === day ? (stats.today || 0) + 1 : 1 };
    await chrome.storage.local.set({ stats: next });
}

async function enqueue(profile) {
    const { queue = [] } = await chrome.storage.local.get('queue');
    const key = String(profile.handle).toLowerCase();
    const rest = queue.filter(p => String(p.handle).toLowerCase() !== key);   // keep only the newest copy
    rest.push(profile);
    await chrome.storage.local.set({ queue: rest.slice(-QUEUE_MAX) });
}

async function post(profile) {
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
        return { ok: false, error: 'network', retry: true };
    }
    let body = {};
    try { body = await res.json(); } catch (_) { /* not JSON */ }
    if (res.status === 401) return { ok: false, error: 'bad-code' };
    if (res.status === 429 || res.status >= 500) return { ok: false, error: 'busy', retry: true };
    if (!res.ok) return { ok: false, error: 'server', detail: typeof body.detail === 'string' ? body.detail : '' };
    return { ok: true, status: body.status, handle: profile.handle, name: profile.name, followers: profile.followers, at: Date.now() };
}

async function capture(profile) {
    const r = await post(profile);
    if (r.ok) {
        await chrome.storage.local.set({ last: r, authError: false });
        await bumpStats();
        flushQueue();                                     // the server is reachable again
    } else if (r.retry) {
        await enqueue(profile);
    } else if (r.error === 'bad-code') {
        await chrome.storage.local.set({ authError: true });
        badge('!', RED);
    }
    return r;
}

let flushing = false;
async function flushQueue() {
    if (flushing) return { sent: 0 };
    flushing = true;
    let sent = 0;
    try {
        const { queue = [] } = await chrome.storage.local.get('queue');
        const left = [];
        for (let i = 0; i < queue.length; i++) {
            const r = await post(queue[i]);
            if (r.ok) { sent++; await bumpStats(); await chrome.storage.local.set({ last: r }); continue; }
            if (r.error === 'bad-code') { await chrome.storage.local.set({ authError: true }); left.push(...queue.slice(i)); break; }
            if (r.retry) { left.push(...queue.slice(i)); break; }   // still unreachable: try later
            // anything else (e.g. the server rejected the profile) is dropped
        }
        await chrome.storage.local.set({ queue: left });
    } finally {
        flushing = false;
    }
    return { sent };
}

chrome.runtime.onInstalled.addListener(details => {
    chrome.alarms.create('lumio-retry', { periodInMinutes: RETRY_MINUTES });
    // First install: open the how-to guide that ships with the extension.
    if (details && details.reason === 'install') chrome.tabs.create({ url: chrome.runtime.getURL('guide.html') });
});
chrome.runtime.onStartup.addListener(() => chrome.alarms.create('lumio-retry', { periodInMinutes: RETRY_MINUTES }));
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'lumio-retry') flushQueue(); });

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
    if (!msg) return;
    // 'tiko-capture' is the name older content scripts (still open in a tab after an update) use
    if ((msg.type === 'lumio-capture' || msg.type === 'tiko-capture') && msg.profile) {
        capture(msg.profile).then(r => {
            if (r.ok) badge('✓', NAVY);
            else if (msg.manual && r.error !== 'bad-code') badge('!', RED);
            respond(r);
        });
        return true;                                      // answer asynchronously
    }
    if (msg.type === 'lumio-flush') { flushQueue().then(respond); return true; }
});
