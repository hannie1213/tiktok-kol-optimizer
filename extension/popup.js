'use strict';
const zh = (navigator.language || '').toLowerCase().startsWith('zh');
const TEXT = zh ? {
    sub: '达人采集助手',
    intro: '打开 TikTok 达人主页时，把页面上公开显示的账号、粉丝数、获赞数发送到 Lumio 的「发现达人」，方便团队查找和筛选。',
    codeLabel: '连接码', connect: '连接', connecting: '正在验证…',
    where: '在 Lumio 网站「发现达人」页面点上方的「浏览器扩展」，再点「生成连接码」，粘贴到上面会自动连接。',
    connected: '已连接', capture: '采集当前页面',
    auto: '打开达人主页时自动采集', toast: '采集后在页面右下角提示',
    autoNote: '只读取达人主页上公开显示的信息，不读取其他页面，也不读取你的账号。',
    disconnect: '断开连接', badCode: '连接码格式不对。请在 Lumio 的「发现达人」页面重新生成并完整粘贴。',
    denied: '需要允许访问你的 Lumio 网址才能连接。', notProfile: '当前页面不是 TikTok 达人主页。打开一位达人的主页后再试。',
    followers: '粉丝', sent: '已发送到达人池', updated: '已更新达人池里的记录', queued: '服务器暂时连不上，已保存，稍后自动重试。',
    network: '连不上 Lumio 服务器，请检查网络或网址。', expired: '连接码已失效，采集已暂停。', reconnect: '重新连接',
    rejected: '连接码已失效或不属于这个网站，请重新生成。', failed: '采集失败。',
    last: '上次采集', reload: '请刷新这个页面后再试。',
    today: '今日采集', total: '累计采集', queue: '待重试', flush: n => `立即重试 ${n} 条`, flushed: n => `已补发 ${n} 条`,
    update: v => `插件有新版本 ${v}，请到 Lumio 网站「发现达人」重新下载安装。`, openSite: '打开 Lumio',
} : {
    sub: 'Creator Capture',
    intro: 'When you open a TikTok creator\'s profile, sends the handle, follower and like counts shown on that page to Lumio\'s Explore pool, so your team can find and shortlist them.',
    codeLabel: 'Connection code', connect: 'Connect', connecting: 'Checking…',
    where: 'On the Lumio site, open Explore, click "Browser extension", then "Create connection code". Pasting it here connects automatically.',
    connected: 'Connected', capture: 'Capture this page',
    auto: 'Capture automatically when I open a profile', toast: 'Show a note on the page after a capture',
    autoNote: 'Only reads what a public creator profile shows. It does not read other pages or your own account.',
    disconnect: 'Disconnect', badCode: 'That code is not valid. Create a new one on Lumio\'s Explore page and paste all of it.',
    denied: 'Permission to reach your Lumio address is needed to connect.', notProfile: 'This page is not a TikTok creator profile. Open one and try again.',
    followers: 'followers', sent: 'Sent to the pool', updated: 'Pool record updated', queued: 'Server unreachable. Saved and will retry automatically.',
    network: 'Could not reach the Lumio server. Check your connection or the address.', expired: 'Your connection code expired, so capturing is paused.', reconnect: 'Reconnect',
    rejected: 'That code has expired or belongs to another site. Create a new one.', failed: 'Capture failed.',
    last: 'Last capture', reload: 'Reload this page and try again.',
    today: 'Today', total: 'All time', queue: 'Waiting', flush: n => `Retry ${n} now`, flushed: n => `Sent ${n}`,
    update: v => `Version ${v} is available. Download it again from Lumio's Explore page.`, openSite: 'Open Lumio',
};
const $ = id => document.getElementById(id);
const fmt = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : String(n || 0);
const today = () => new Date().toISOString().slice(0, 10);

// A connection code is "<server origin>|<token>", base64-encoded by the site.
function parseCode(raw) {
    try {
        const [api, token] = atob(String(raw).replace(/\s+/g, '')).split('|');
        const url = new URL(api);
        if (!/^https?:$/.test(url.protocol) || !token || token.split('.').length !== 3) return null;
        return { api: url.origin, token };
    } catch (_) { return null; }
}

// Newer than? Compares dotted versions numerically ("1.10.0" > "1.9.2").
function newer(a, b) {
    const pa = String(a).split('.').map(Number), pb = String(b).split('.').map(Number);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const d = (pa[i] || 0) - (pb[i] || 0);
        if (d) return d > 0;
    }
    return false;
}

// Asks the server who this code belongs to. Returns {ok, email, latest_version} or {ok:false, error}.
async function ping(api, token) {
    try {
        const res = await fetch(api + '/extension/ping', { headers: { Authorization: 'Bearer ' + token } });
        if (res.status === 401 || res.status === 403) return { ok: false, error: 'bad-code' };
        if (!res.ok) return { ok: false, error: 'network' };
        return { ok: true, ...(await res.json()) };
    } catch (_) { return { ok: false, error: 'network' }; }
}

async function activeTab() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab;
}
function ask(tabId, msg) {
    return new Promise(resolve => {
        try { chrome.tabs.sendMessage(tabId, msg, r => { void chrome.runtime.lastError; resolve(r || null); }); }
        catch (_) { resolve(null); }
    });
}
function sendBg(msg) {
    return new Promise(resolve => chrome.runtime.sendMessage(msg, r => { void chrome.runtime.lastError; resolve(r || null); }));
}

function labels() {
    const set = (id, k) => { $(id).textContent = TEXT[k]; };
    [['t-sub', 'sub'], ['t-intro', 'intro'], ['t-code-label', 'codeLabel'], ['connect', 'connect'], ['t-where', 'where'],
     ['t-connected', 'connected'], ['capture', 'capture'], ['t-auto', 'auto'], ['t-toast', 'toast'], ['t-auto-note', 'autoNote'],
     ['disconnect', 'disconnect'], ['t-expired', 'expired'], ['reconnect', 'reconnect'], ['t-today', 'today'],
     ['t-total', 'total'], ['t-queue', 'queue'], ['open-site', 'openSite']].forEach(([id, k]) => set(id, k));
}

async function render() {
    const s = await chrome.storage.local.get(['api', 'token', 'auto', 'toastOn', 'last', 'stats', 'queue', 'authError', 'email', 'latest']);
    const connected = !!(s.api && s.token);
    $('setup').classList.toggle('hidden', connected);
    $('main').classList.toggle('hidden', !connected);
    if (!connected) { $('code').focus(); return; }

    $('server').textContent = new URL(s.api).host;
    $('account').textContent = s.email || '';
    $('open-site').href = s.api;
    $('auto').checked = s.auto !== false;
    $('toast').checked = s.toastOn !== false;
    $('auth-banner').classList.toggle('hidden', !s.authError);
    $('dot').classList.toggle('bad', !!s.authError);

    const stats = s.stats || {};
    $('s-today').textContent = stats.day === today() ? fmt(stats.today) : '0';
    $('s-total').textContent = fmt(stats.total);
    const q = (s.queue || []).length;
    $('s-queue').textContent = String(q);
    $('flush').classList.toggle('hidden', !q);
    $('flush').textContent = TEXT.flush(q);

    const mine = chrome.runtime.getManifest().version;
    const upd = s.latest && newer(s.latest, mine);
    $('update-banner').classList.toggle('hidden', !upd);
    if (upd) $('update-banner').textContent = TEXT.update(s.latest);

    if (s.last && s.last.ok) {
        $('last').classList.remove('hidden');
        $('last').textContent = `${TEXT.last}: @${s.last.handle} · ${fmt(s.last.followers)} ${TEXT.followers}`;
    }
    const tab = await activeTab();
    const res = tab && tab.id != null ? await ask(tab.id, { type: 'lumio-read' }) : null;
    const p = res && res.profile;
    $('capture').disabled = !p || !!s.authError;
    $('page').innerHTML = '';
    if (p) {
        const b = document.createElement('b'); b.textContent = (p.name || p.handle) + '  @' + p.handle;
        const sp = document.createElement('span'); sp.textContent = `${fmt(p.followers)} ${TEXT.followers}`;
        $('page').append(b, sp);
    } else {
        $('page').textContent = TEXT.notProfile;
    }
}

// Refresh the account and latest version quietly each time the popup opens.
async function refresh() {
    const { api, token } = await chrome.storage.local.get(['api', 'token']);
    if (!api || !token) return;
    const r = await ping(api, token);
    if (r.ok) {
        await chrome.storage.local.set({ email: r.email, latest: r.latest_version, authError: false });
        chrome.action.setBadgeText({ text: '' });
    } else if (r.error === 'bad-code') {
        await chrome.storage.local.set({ authError: true });
    }
    render();
}

let busy = false;
async function connect() {
    if (busy) return;
    const msg = $('setup-msg');
    const parsed = parseCode($('code').value);
    if (!parsed) { msg.className = 'bad'; msg.textContent = TEXT.badCode; return; }
    busy = true;
    $('connect').disabled = true; $('connect').textContent = TEXT.connecting; msg.textContent = '';
    try {
        // Ask for access to exactly the server in the code, nothing broader.
        const granted = await chrome.permissions.request({ origins: [parsed.api + '/*'] }).catch(() => false);
        if (!granted) { msg.className = 'bad'; msg.textContent = TEXT.denied; return; }
        const r = await ping(parsed.api, parsed.token);
        if (!r.ok) { msg.className = 'bad'; msg.textContent = r.error === 'bad-code' ? TEXT.rejected : TEXT.network; return; }
        await chrome.storage.local.set({ api: parsed.api, token: parsed.token, email: r.email, latest: r.latest_version, authError: false });
        chrome.action.setBadgeText({ text: '' });
        $('code').value = '';
        sendBg({ type: 'lumio-flush' });                    // anything saved while the old code was expired
        render();
    } finally {
        busy = false;
        $('connect').disabled = false; $('connect').textContent = TEXT.connect;
    }
}

$('connect').addEventListener('click', connect);
// Pasting a whole code connects straight away.
$('code').addEventListener('paste', () => setTimeout(() => { if (parseCode($('code').value)) connect(); }, 0));

$('capture').addEventListener('click', async () => {
    const tab = await activeTab();
    $('capture').disabled = true;
    const r = tab && tab.id != null ? await ask(tab.id, { type: 'lumio-capture-now' }) : null;
    $('capture').disabled = false;
    const msg = $('capture-msg');
    if (!r) { msg.className = 'bad'; msg.textContent = TEXT.reload; return; }
    if (r.ok) { msg.className = 'ok'; msg.textContent = r.status === 'created' ? TEXT.sent : TEXT.updated; render(); return; }
    if (r.retry) { msg.className = 'ok'; msg.textContent = TEXT.queued; render(); return; }
    msg.className = 'bad';
    msg.textContent = { 'no-profile': TEXT.notProfile, network: TEXT.network, 'bad-code': TEXT.expired }[r.error] || r.detail || TEXT.failed;
    render();
});

$('flush').addEventListener('click', async () => {
    $('flush').disabled = true;
    const r = await sendBg({ type: 'lumio-flush' });
    $('flush').disabled = false;
    const msg = $('capture-msg');
    msg.className = 'ok'; msg.textContent = TEXT.flushed((r && r.sent) || 0);
    render();
});

$('auto').addEventListener('change', e => chrome.storage.local.set({ auto: e.target.checked }));
$('toast').addEventListener('change', e => chrome.storage.local.set({ toastOn: e.target.checked }));

$('reconnect').addEventListener('click', async () => {
    await chrome.storage.local.remove(['token', 'authError']);
    render();
});

$('disconnect').addEventListener('click', async () => {
    const { api } = await chrome.storage.local.get('api');
    await chrome.storage.local.remove(['api', 'token', 'last', 'email', 'authError', 'queue']);
    if (api) chrome.permissions.remove({ origins: [api + '/*'] }).catch(() => {});
    chrome.action.setBadgeText({ text: '' });
    render();
});

labels();
render().then(refresh);
