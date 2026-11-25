"""Industry news: feed parsing, article-link previews and the console/admin endpoints.
Network access is replaced by a fake `news.fetch`, so nothing leaves the machine."""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from backend import news, settings
from backend.main import app

client = TestClient(app)
KEY = "test-admin-key"
H = {"X-Admin-Key": KEY}

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>SEA Commerce</title>
<item><title>TikTok Shop expands in Malaysia</title><link>https://news.example.com/a1</link>
<description>&lt;p&gt;Sellers in &lt;b&gt;Malaysia&lt;/b&gt; get new tools.&lt;/p&gt;</description>
<pubDate>Tue, 25 Nov 2026 08:00:00 GMT</pubDate><media:content url="https://img.example.com/a1.jpg" medium="image"/></item>
<item><title>11.11 results across SEA</title><link>https://news.example.com/a2</link>
<content:encoded><![CDATA[<img src="https://img.example.com/a2.png"> GMV records <script>x()</script>]]></content:encoded></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Live commerce playbook</title><link rel="alternate" href="/posts/1"/>
<updated>2026-11-20T10:00:00Z</updated><summary>How to plan a live.</summary></entry></feed>"""

WECHAT = """<html><head><meta property="og:title" content="东南亚大促备战指南" />
<meta property="og:description" content="五国市场 &amp; 选品建议" />
<meta property="og:image" content="https://mmbiz.qpic.cn/cover.jpg" /></head>
<body><strong id="js_name">TikTok Shop东南亚电商</strong>
<script>var ct = "1764057600";</script></body></html>"""


@pytest.fixture(autouse=True)
def _admin_key(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", KEY)


def test_parse_rss_strips_html_and_finds_images():
    items = news.parse_feed(RSS)
    assert [i["title"] for i in items] == ["TikTok Shop expands in Malaysia", "11.11 results across SEA"]
    assert items[0]["summary"] == "Sellers in Malaysia get new tools."
    assert items[0]["image"] == "https://img.example.com/a1.jpg"
    assert items[0]["published_at"].startswith("2026-11-25T08:00:00")
    assert items[1]["image"] == "https://img.example.com/a2.png"
    assert "<" not in items[1]["summary"]


def test_parse_atom_resolves_relative_links():
    items = news.parse_feed(ATOM, "https://blog.example.com/feed.xml")
    assert items[0]["url"] == "https://blog.example.com/posts/1"
    assert items[0]["published_at"].startswith("2026-11-20T10:00:00")


def test_parse_feed_rejects_html_and_entities():
    with pytest.raises(news.FetchError):
        news.parse_feed("<html><body>not a feed</body></html>")
    with pytest.raises(news.FetchError):
        news.parse_feed('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss></rss>')


def test_parse_wechat_article():
    meta = news.parse_article(WECHAT, "https://mp.weixin.qq.com/s/abc")
    assert meta["title"] == "东南亚大促备战指南"
    assert meta["summary"] == "五国市场 & 选品建议"
    assert meta["image"] == "https://mmbiz.qpic.cn/cover.jpg"
    assert meta["source_name"] == "TikTok Shop东南亚电商"
    assert meta["published_at"].startswith("2025-11-25")


def test_private_addresses_are_refused():
    for bad in ("http://127.0.0.1/feed", "http://localhost/x", "file:///etc/passwd", "http://10.0.0.5/"):
        with pytest.raises(news.FetchError):
            news._check_public(bad)


def test_admin_endpoints_need_key():
    assert client.get("/admin/news/sources").status_code == 403
    assert client.post("/admin/news/link", json={"url": "https://x.example.com/a"}).status_code == 403


def test_source_lifecycle_and_console_feed(monkeypatch):
    feed_url = f"https://feeds.example.com/{uuid.uuid4().hex}.xml"
    pages = {feed_url: RSS}
    monkeypatch.setattr(news, "fetch", lambda url: (url, pages[url]))

    r = client.post("/admin/news/sources", headers=H, json={"name": "SEA Commerce", "url": feed_url})
    assert r.status_code == 200, r.text
    sid = r.json()["source"]["id"]
    assert r.json()["new"] == 2 and r.json()["source"]["item_count"] == 2
    assert client.post("/admin/news/sources", headers=H, json={"name": "dup", "url": feed_url}).status_code == 409

    # refreshing again stores nothing twice
    assert client.post("/admin/news/refresh", headers=H).json()["new"] == 0

    d = client.get("/news", params={"q": "Malaysia"}).json()
    assert any(i["url"] == "https://news.example.com/a1" for i in d["items"])
    assert "SEA Commerce" in d["sources"]

    # hiding removes it from the console and a refetch does not bring it back
    item = next(i for i in client.get("/admin/news/items", headers=H).json()["items"] if i["url"].endswith("/a1"))
    client.post(f"/admin/news/items/{item['id']}", headers=H, json={"hidden": True})
    client.post("/admin/news/refresh", headers=H)
    assert all(i["url"] != item["url"] for i in client.get("/news", params={"limit": 60}).json()["items"])

    # a broken feed records the error instead of failing
    pages[feed_url] = "<html>gone</html>"
    res = client.post("/admin/news/refresh", headers=H).json()["results"]
    assert any(x["id"] == sid and x["error"] for x in res)

    assert client.delete(f"/admin/news/sources/{sid}", headers=H).json()["ok"]
    assert all(s["id"] != sid for s in client.get("/admin/news/sources", headers=H).json()["sources"])


def test_add_wechat_link_and_pin(monkeypatch):
    link = f"https://mp.weixin.qq.com/s/{uuid.uuid4().hex}"
    monkeypatch.setattr(news, "fetch", lambda url: (url + "?scene=1", WECHAT))
    r = client.post("/admin/news/link", headers=H, json={"url": link})
    assert r.status_code == 200, r.text
    it = r.json()["item"]
    assert it["url"] == link and it["source"] == "TikTok Shop东南亚电商" and it["lang"] == "zh"
    assert client.post("/admin/news/link", headers=H, json={"url": link}).json()["created"] is False

    client.post(f"/admin/news/items/{it['id']}", headers=H, json={"pinned": True})
    assert client.get("/news").json()["items"][0]["id"] == it["id"]
    client.post(f"/admin/news/items/{it['id']}", headers=H, json={"pinned": False})


def test_add_link_without_title_is_rejected(monkeypatch):
    monkeypatch.setattr(news, "fetch", lambda url: (url, "<html><body>empty</body></html>"))
    assert client.post("/admin/news/link", headers=H, json={"url": "https://x.example.com/blank"}).status_code == 400
