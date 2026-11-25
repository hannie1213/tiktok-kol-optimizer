'use strict';
const zh = (navigator.language || '').toLowerCase().startsWith('zh');
const TEXT = zh ? {
    intro: '打开达人主页时，把页面上公开显示的账号、粉丝数、获赞数发送到 Lumio 的「发现达人」，方便团队查找和筛选。',
    codeLabel: '连接码', connect: '连接', connected: '已连接到', capture: '采集当前页面',
    auto: '打开达人主页时自动采集', autoNote: '只读取达人主页上公开显示的信息，不读取其他页面，也不读取你的账号。',
    disconnect: '断开连接', badCode: '连接码无效。请在 Lumio 的「发现达人」页面重新生成并完整粘贴。',
    denied: '需要允许访问你的 Lumio 网址才能连接。', notProfile: '当前页面不是达人主页。打开一位达人的主页后再试。',
    onPage: '当前页面', followers: '粉丝', sent: '已发送到达人池', updated: '已更新达人池里的记录',
    network: '连不上 Lumio 服务器，请检查网络或网址。', expired: '连接码已失效，请重新连接。', failed: '采集失败。',
    last: '上次采集', reload: '请刷新这个页面后再试。',
} : {
    intro: 'When you open a creator\'s profile, sends the handle, follower and like counts shown on that page to Lumio\'s Explore pool, so your team can find and shortlist them.',
    codeLabel: 'Connection code', connect: 'Connect', connected: 'Connected to', capture: 'Capture this page',
    auto: 'Capture automatically when I open a profile', autoNote: 'Only reads what a public creator profile shows. It does not read other pages or your own account.',
    disconnect: 'Disconnect', badCode: 'That code is not valid. Create a new one on Lumio\'s Explore page and paste all of it.',
    denied: 'Permission to reach your Lumio address is needed to connect.', notProfile: 'This page is not a creator profile. Open a creator\'s profile and try again.',
    onPage: 'On this page', followers: 'followers', sent: 'Sent to the pool', updated: 'Pool record updated',
    network: 'Could not reach the Lumio server. Check your connection or the address.', expired: 'The connection code has expired — connect again.', failed: 'Capture failed.',
    last: 'Last capture', reload: 'Reload this page and try again.',
};
const $ = id => document.getElementById(id);
const fmt = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : String(n);

// A connection code is "<server origin>|<token>", base64-encoded by the site.
function parseCode(raw) {
    try {
        const [api, token] = atob(String(raw).replace(/\s+/g, '')).split('|');
        const url = new URL(api);
        if (!/^https?:$/.test(url.protocol) || !token || token.split('.').length !== 3) return null;
        return { api: url.origin, token };
    } catch (_) { return null; }
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

async function render() {
    const { api, token, auto = true, last } = await chrome.storage.local.get(['api', 'token', 'auto', 'last']);
    $('t-intro').textContent = TEXT.intro; $('t-code-label').textContent = TEXT.codeLabel;
    $('connect').textContent = TEXT.connect; $('t-connected').textContent = TEXT.connected;
    $('capture').textContent = TEXT.capture; $('t-auto').textContent = TEXT.auto;
    $('t-auto-note').textContent = TEXT.autoNote; $('disconnect').textContent = TEXT.disconnect;
    const connected = !!(api && token);
    $('setup').classList.toggle('hidden', connected);
    $('main').classList.toggle('hidden', !connected);
    if (!connected) return;
    $('server').textContent = new URL(api).host;
    $('auto').checked = !!auto;
    if (last && last.ok) {
        $('last').classList.remove('hidden');
        $('last').textContent = `${TEXT.last}: @${last.handle} · ${fmt(last.followers)} ${TEXT.followers}`;
    }
    const tab = await activeTab();
    const res = tab && tab.id != null ? await ask(tab.id, { type: 'tiko-read' }) : null;
    const p = res && res.profile;
    $('capture').disabled = !p;
    $('page').innerHTML = '';
    if (p) {
        const b = document.createElement('b'); b.textContent = (p.name || p.handle) + '  @' + p.handle;
        const s = document.createElement('span'); s.textContent = `${fmt(p.followers)} ${TEXT.followers}`;
        $('page').append(b, s);
    } else {
        $('page').textContent = TEXT.notProfile;
    }
}

$('connect').addEventListener('click', async () => {
    const parsed = parseCode($('code').value);
    if (!parsed) { $('setup-msg').textContent = TEXT.badCode; return; }
    // Ask for access to exactly the server in the code, nothing broader.
    const granted = await chrome.permissions.request({ origins: [parsed.api + '/*'] }).catch(() => false);
    if (!granted) { $('setup-msg').textContent = TEXT.denied; return; }
    await chrome.storage.local.set({ api: parsed.api, token: parsed.token, auto: true });
    $('setup-msg').textContent = '';
    render();
});

$('capture').addEventListener('click', async () => {
    const tab = await activeTab();
    $('capture').disabled = true;
    const r = tab && tab.id != null ? await ask(tab.id, { type: 'tiko-capture-now' }) : null;
    $('capture').disabled = false;
    const msg = $('capture-msg');
    if (!r) { msg.className = 'bad'; msg.textContent = TEXT.reload; return; }
    if (r.ok) { msg.className = 'ok'; msg.textContent = r.status === 'created' ? TEXT.sent : TEXT.updated; render(); return; }
    msg.className = 'bad';
    msg.textContent = { 'no-profile': TEXT.notProfile, network: TEXT.network, 'bad-code': TEXT.expired }[r.error] || r.detail || TEXT.failed;
});

$('auto').addEventListener('change', e => chrome.storage.local.set({ auto: e.target.checked }));

$('disconnect').addEventListener('click', async () => {
    const { api } = await chrome.storage.local.get('api');
    await chrome.storage.local.remove(['api', 'token', 'last']);
    if (api) chrome.permissions.remove({ origins: [api + '/*'] }).catch(() => {});
    render();
});

render();
