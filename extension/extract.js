// Reads a creator profile out of the page the user is looking at.
//
// Pure functions over a Document, with no extension APIs, so the same file is
// loaded by the content script and can be exercised in a plain page or Node
// test. It only reads what the public profile page already displays.
//
// Three sources, most reliable first:
//   1. the JSON the page ships for its own rendering
//   2. the visible counters (data-e2e attributes)
//   3. the page's <meta> description as a last resort for the follower count
(function (root) {
    'use strict';

    // "1.2M" / "356.4K" / "9,100" / "2.3B" -> integer (null if not a number)
    function parseCount(text) {
        if (text == null) return null;
        const m = String(text).replace(/,/g, '').trim().match(/^([\d.]+)\s*([KMB])?/i);
        if (!m) return null;
        const n = parseFloat(m[1]);
        if (!isFinite(n)) return null;
        const mult = { K: 1e3, M: 1e6, B: 1e9 }[(m[2] || '').toUpperCase()] || 1;
        return Math.round(n * mult);
    }

    // "/@some.handle" or "/@some.handle/video/123" -> "some.handle"
    function handleFromPath(pathname) {
        const m = String(pathname || '').match(/^\/@([A-Za-z0-9._\-]{2,64})(?:\/|$)/);
        return m ? m[1] : null;
    }

    // Is this a profile page (not a video, live room or search page)?
    function isProfilePath(pathname) {
        return /^\/@[A-Za-z0-9._\-]{2,64}\/?$/.test(String(pathname || ''));
    }

    function fromEmbeddedJson(doc, handle) {
        const el = doc.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__') || doc.getElementById('SIGI_STATE');
        if (!el || !el.textContent) return null;
        let data;
        try { data = JSON.parse(el.textContent); } catch (_) { return null; }
        const scope = data.__DEFAULT_SCOPE__ || {};
        const info = (scope['webapp.user-detail'] || {}).userInfo;
        let user = info && info.user, stats = info && (info.statsV2 || info.stats);
        if (!user && data.UserModule) {                     // older page shape
            const users = data.UserModule.users || {}, st = data.UserModule.stats || {};
            const key = Object.keys(users).find(k => k.toLowerCase() === String(handle).toLowerCase());
            if (key) { user = users[key]; stats = st[key]; }
        }
        if (!user || !stats) return null;
        if (handle && String(user.uniqueId || '').toLowerCase() !== String(handle).toLowerCase()) return null;
        const num = v => (v == null ? null : parseInt(v, 10));
        return {
            handle: user.uniqueId, name: user.nickname || '', bio: user.signature || '',
            verified: !!user.verified, country: user.region || null,
            followers: num(stats.followerCount), likes: num(stats.heartCount != null ? stats.heartCount : stats.heart),
            videos: num(stats.videoCount),
        };
    }

    function fromVisibleCounters(doc, handle) {
        const text = sel => { const e = doc.querySelector(sel); return e ? e.textContent.trim() : ''; };
        const followers = parseCount(text('[data-e2e="followers-count"]'));
        if (followers == null) return null;
        return {
            handle: text('[data-e2e="user-title"]') || handle,
            name: text('[data-e2e="user-subtitle"]'), bio: text('[data-e2e="user-bio"]'),
            verified: false, country: null, followers,
            likes: parseCount(text('[data-e2e="likes-count"]')), videos: null,
        };
    }

    function fromMeta(doc, handle) {
        const meta = doc.querySelector('meta[name="description"], meta[property="og:description"]');
        const m = meta && (meta.getAttribute('content') || '').match(/([\d.,]+[KMB]?)\s+Followers/i);
        const followers = m ? parseCount(m[1]) : null;
        if (followers == null) return null;
        const title = doc.querySelector('meta[property="og:title"]');
        return { handle, name: title ? (title.getAttribute('content') || '').split('(')[0].trim() : '',
                 bio: '', verified: false, country: null, followers, likes: null, videos: null };
    }

    // -> { platform, handle, name, followers, likes, videos, bio, verified, country } or null
    function extractProfile(doc, pathname) {
        if (!isProfilePath(pathname)) return null;
        const handle = handleFromPath(pathname);
        const p = fromEmbeddedJson(doc, handle) || fromVisibleCounters(doc, handle) || fromMeta(doc, handle);
        if (!p || p.followers == null || !p.handle) return null;
        // The page we are on is the source of truth for whose profile this is.
        if (String(p.handle).replace(/^@/, '').toLowerCase() !== handle.toLowerCase()) p.handle = handle;
        return {
            platform: 'tiktok', handle: String(p.handle).replace(/^@/, ''),
            name: String(p.name || '').slice(0, 120), bio: String(p.bio || '').slice(0, 600),
            followers: p.followers, likes: p.likes == null ? null : p.likes, videos: p.videos == null ? null : p.videos,
            verified: !!p.verified, country: p.country ? String(p.country).toUpperCase().slice(0, 2) : null,
        };
    }

    const api = { parseCount, handleFromPath, isProfilePath, extractProfile };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.LumioExtract = root.TikoExtract = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
