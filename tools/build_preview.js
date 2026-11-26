/*
 * Builds the offline design preview: frontend/index.html with every API answer
 * it needs recorded from a running server, in BOTH languages, so the preview
 * switches between English and Chinese exactly like the real site.
 *
 *   uvicorn backend.main:app --port 8000          # in another terminal
 *   node tools/build_preview.js [http://localhost:8000] [out.html]
 *
 * Needs Playwright (require('playwright')). The output is one HTML file; publish
 * it next to frontend/dist/output.css and frontend/og.jpg.
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const BASE = process.argv[2] || 'http://localhost:8000';
const OUT = process.argv[3] || 'preview.html';
const PAGES = ['start', 'dashboard', 'creators', 'explore', 'campaigns', 'pipeline', 'analytics', 'templates',
    'contracts', 'messages', 'scripts', 'news', 'guide', 'activity', 'billing', 'settings'];

async function record(browser, lang) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await ctx.addInitScript(l => { try { localStorage.setItem('kol_lang', l); } catch (e) { /* ignore */ } }, lang);
    const page = await ctx.newPage();
    const rec = {};
    page.on('response', async r => {
        const req = r.request();
        const u = new URL(r.url());
        if (u.origin !== new URL(BASE).origin || !['fetch', 'xhr'].includes(req.resourceType())) return;
        try { rec[req.method() + ' ' + u.pathname + u.search] = { s: r.status(), b: await r.json() }; } catch (e) { /* not JSON */ }
    });
    const settle = ms => page.waitForTimeout(ms);
    await page.goto(BASE + '/'); await settle(1200);
    await page.evaluate(() => enterDemo()); await settle(2500);
    await page.click('#run-btn'); await settle(6000);
    for (const p of PAGES) { await page.evaluate(p => showPage(p), p); await settle(1300); }
    await page.evaluate(() => showPage('dashboard')); await settle(500);
    await page.evaluate(() => { const k = (lastOptimizeData.selected_kols || [])[0]; if (k) openKolDetail(k.id); }); await settle(1500);
    await page.evaluate(() => { closeKolDetail(); openBudgetAdvisor(); }); await settle(1500);
    await page.evaluate(() => { showPage('templates'); }); await settle(800);
    await page.evaluate(() => { const b = document.getElementById('tp-k-contract'); if (b) b.click(); }); await settle(1200);
    await ctx.close();
    return rec;
}

(async () => {
    const browser = await chromium.launch();
    const REC = { en: await record(browser, 'en'), zh: await record(browser, 'zh') };
    await browser.close();

    const shim = `<script>
/* Design preview only: in-memory storage fallback + canned API responses recorded
   from a demo account in English and Chinese (tools/build_preview.js). */
(function(){
  var mem={}, ls=null; try{ ls=window.localStorage; ls.setItem('__t','1'); ls.removeItem('__t'); }catch(e){ ls=null; }
  if(!ls){ var fake={getItem:function(k){return k in mem?mem[k]:null},setItem:function(k,v){mem[k]=String(v)},removeItem:function(k){delete mem[k]},clear:function(){mem={}},key:function(){return null},length:0};
    try{ Object.defineProperty(window,'localStorage',{value:fake,configurable:true}); }catch(e){}
    try{ Object.defineProperty(window,'sessionStorage',{value:fake,configurable:true}); }catch(e){} }
  try{ (ls||window.localStorage).removeItem('kol_token'); }catch(e){}
  var REC=${JSON.stringify(REC)};
  function find(lang,m,path,search){ var R=REC[lang]||REC.en; var k=m+' '+path+search; if(R[k]) return R[k];
    for(var key in R){ var q=key.indexOf('?'); var base=q<0?key:key.slice(0,q); if(base===m+' '+path) return R[key]; }
    return lang==='en'?null:find('en',m,path,search); }
  var DENIED={en:'This is a design preview. Saving and editing work on the live site.',zh:'这是设计预览，保存和编辑请在正式网站上操作。'};
  window.fetch=function(url,opts){ opts=opts||{}; var u; try{ u=new URL(typeof url==='string'?url:url.url, location.href);}catch(e){ return Promise.reject(e); }
    if(u.origin!==location.origin) return Promise.reject(new Error('offline preview'));
    var h=opts.headers||{}; var lang=String(h['X-Lang']||(typeof LANG!=='undefined'?LANG:'en')).indexOf('zh')===0?'zh':'en';
    var m=(opts.method||'GET').toUpperCase(); var hit=find(lang,m,u.pathname,u.search); var body, status=200;
    if(hit){ body=hit.b; status=hit.s; } else if(m==='GET'){ body={}; }
    else { status=403; body={detail:DENIED[lang]}; }
    return new Promise(function(res){ setTimeout(function(){ res(new Response(JSON.stringify(body),{status:status,headers:{'Content-Type':'application/json'}})); }, m==='GET'?120:700); });
  };
  // In the preview, signing in = opening the demo account.
  window.addEventListener('DOMContentLoaded', function(){ var orig=window.enterApp;
    if(typeof orig==='function') window.enterApp=function(){ if(!window.AUTH_TOKEN && typeof AUTH_TOKEN!=='undefined' && !AUTH_TOKEN && typeof enterDemo==='function') return enterDemo(); return orig.apply(this,arguments); }; });
})();
</script>`;
    const html = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'index.html'), 'utf8');
    const at = html.indexOf('<head>') + '<head>'.length;
    fs.writeFileSync(OUT, html.slice(0, at) + '\n' + shim + html.slice(at));
    console.log(`wrote ${OUT}: ${Object.keys(REC.en).length} en + ${Object.keys(REC.zh).length} zh responses`);
})();
