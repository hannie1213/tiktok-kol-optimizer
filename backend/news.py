"""
Industry news — an e-commerce news feed shown in the console.

Two ways content gets in, both run by the operator (admin key):
  1. RSS / Atom sources. Every source is fetched when added, on demand, and by
     the hourly background job (backend/jobs.py). New articles are stored once
     (unique by URL); articles the operator removed stay hidden.
  2. A pasted article link. The page's title, summary and cover image are read
     from its Open Graph / meta tags. This is how WeChat official-account
     articles get in: WeChat offers no API for third parties to read an
     account's posts, so the operator pastes the public article link instead.
     (A self-hosted RSS bridge for WeChat can also be added as a normal source.)

Only public pages are fetched, with a size cap, a timeout and no access to
private network addresses.

Endpoints
---------
  GET    /news                        Articles for the console (?q=&limit=&offset=)
  GET    /admin/news/sources          Sources with fetch status
  POST   /admin/news/sources          Add a source {name, url, lang} (fetched right away)
  PATCH  /admin/news/sources/{id}     Enable / disable / rename
  DELETE /admin/news/sources/{id}     Remove a source and its articles
  POST   /admin/news/refresh          Fetch every enabled source now
  GET    /admin/news/items            Recent articles, including hidden ones
  POST   /admin/news/link             Add one article from a pasted link {url, lang?}
  POST   /admin/news/items/{id}       Pin / unpin / hide / unhide {pinned?, hidden?}
"""
import html
import ipaddress
import logging
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from backend.admin import _require_admin
from backend.auth import require_tenant
from backend.db import NewsItem, NewsSource, SessionLocal

log = logging.getLogger("kol.news")

MAX_BYTES = 2_000_000
TIMEOUT = 12.0
KEEP_PER_SOURCE = 300
USER_AGENT = "Mozilla/5.0 (compatible; LumioNewsBot/1.0; +https://github.com/hannie1213/tiktok-kol-optimizer)"

# Shown in the operator console as one-click suggestions; nothing is followed
# until the operator adds it.
SUGGESTED_SOURCES = [
    {"name": "KrASIA", "url": "https://kr-asia.com/feed", "lang": "en"},
    {"name": "e27", "url": "https://e27.co/feed/", "lang": "en"},
    {"name": "Tech in Asia", "url": "https://www.techinasia.com/feed", "lang": "en"},
]

router = APIRouter()
admin_router = APIRouter(prefix="/admin/news", dependencies=[Depends(_require_admin)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Fetching ──────────────────────────────────────────────────────────────
class FetchError(Exception):
    pass


def _check_public(url: str) -> None:
    """Refuse non-http(s) URLs and hosts that resolve to private addresses."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError("Only http(s) links are supported")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise FetchError("Could not resolve the address")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise FetchError("That address is not public")


def fetch(url: str) -> tuple:
    """GET a public URL → (final_url, text). Redirects are re-checked."""
    current = url
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False) as c:
        for _ in range(5):
            _check_public(current)
            with c.stream("GET", current) as r:
                if r.is_redirect and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code}")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise FetchError("Page is too large")
                enc = r.encoding or "utf-8"
                try:
                    return str(r.url), bytes(buf).decode(enc, errors="replace")
                except LookupError:
                    return str(r.url), bytes(buf).decode("utf-8", errors="replace")
    raise FetchError("Too many redirects")


# ── Parsing ───────────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]+>")
_IMG = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)


def _text(s: Optional[str], limit: int = 400) -> str:
    """HTML → short plain text."""
    if not s:
        return ""
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _child_text(el, *names) -> str:
    c = _child(el, *names)
    return (c.text or "").strip() if c is not None and c.text else ""


def _iso(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _image_of(el, html_body: str) -> str:
    for c in el.iter():
        name, url = _local(c.tag), c.get("url")
        if not url:
            continue
        if name == "thumbnail":
            return url
        if name == "content" and (c.get("medium") == "image" or (c.get("type") or "image/").startswith("image")):
            return url
        if name == "enclosure" and (c.get("type") or "").startswith("image"):
            return url
    m = _IMG.search(html_body or "")
    return m.group(1) if m else ""


def parse_feed(text: str, base_url: str = "") -> List[dict]:
    """RSS 2.0 / RSS 1.0 / Atom → [{title, url, summary, image, published_at}]."""
    if "<!ENTITY" in text[:4000]:
        raise FetchError("Feed declares XML entities; refused")
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        raise FetchError("Not a valid RSS or Atom feed")
    items = []
    for el in root.iter():
        kind = _local(el.tag)
        if kind not in ("item", "entry"):
            continue
        title = _text(_child_text(el, "title"), 300)
        if kind == "entry":
            link = ""
            for c in el:
                if _local(c.tag) == "link" and c.get("href") and c.get("rel", "alternate") == "alternate":
                    link = c.get("href")
                    break
            body = _child_text(el, "summary") or _child_text(el, "content")
            when = _child_text(el, "published") or _child_text(el, "updated")
        else:
            link = _child_text(el, "link") or (_child(el, "guid").text.strip() if _child(el, "guid") is not None and (_child(el, "guid").text or "").startswith("http") else "")
            body = _child_text(el, "encoded") or _child_text(el, "description")
            when = _child_text(el, "pubdate") or _child_text(el, "date")
        if not title or not link:
            continue
        link = urljoin(base_url, link)
        if urlparse(link).scheme not in ("http", "https"):
            continue
        img = _image_of(el, body)
        items.append({
            "title": title, "url": link[:700], "summary": _text(body),
            "image": urljoin(link, img)[:700] if img else "", "published_at": _iso(when),
        })
    if not items and _local(root.tag) not in ("rss", "feed", "rdf"):
        raise FetchError("Not a valid RSS or Atom feed")
    return items


def _meta(page: str, *keys) -> str:
    for k in keys:
        for pat in (
            rf"""<meta[^>]+(?:property|name)=["']{re.escape(k)}["'][^>]*content=["']([^"']*)["']""",
            rf"""<meta[^>]+content=["']([^"']*)["'][^>]*(?:property|name)=["']{re.escape(k)}["']""",
        ):
            m = re.search(pat, page, re.I)
            if m and m.group(1).strip():
                return html.unescape(m.group(1).strip())
    return ""


def _js_var(page: str, name: str) -> str:
    """WeChat article pages also carry the metadata as JS variables."""
    m = re.search(r"var\s+" + re.escape(name) + r"""\s*=\s*(?:htmlDecode\()?["']([^"']*)["']""", page)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_article(page: str, url: str) -> dict:
    """Read title / summary / cover / site name / date from an article page."""
    title = (_meta(page, "og:title", "twitter:title") or _js_var(page, "msg_title"))
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = html.unescape(m.group(1)).strip() if m else ""
    summary = _meta(page, "og:description", "description", "twitter:description") or _js_var(page, "msg_desc")
    image = _meta(page, "og:image", "twitter:image") or _js_var(page, "msg_cdn_url")
    site = _meta(page, "og:site_name") or _js_var(page, "nickname")
    if not site and "mp.weixin.qq.com" in url:
        m = re.search(r"""id=["']js_name["'][^>]*>\s*([^<]+)<""", page)
        site = m.group(1).strip() if m else "WeChat"
    when = _meta(page, "article:published_time", "og:published_time")
    if not when:
        m = re.search(r"""var\s+ct\s*=\s*["']?(\d{9,11})""", page)
        if m:
            when = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).isoformat()
    return {
        "title": _text(title, 300), "summary": _text(summary),
        "image": urljoin(url, image)[:700] if image else "",
        "source_name": _text(site, 120) or (urlparse(url).hostname or ""),
        "published_at": _iso(when) if when else "",
    }


# ── Storage ───────────────────────────────────────────────────────────────
def _item_dict(it: NewsItem) -> dict:
    return {"id": it.id, "title": it.title, "url": it.url, "summary": it.summary, "image": it.image,
            "source": it.source_name, "lang": it.lang, "published_at": it.published_at or it.added_at,
            "pinned": it.pinned, "hidden": it.hidden}


def _store(s, src: Optional[NewsSource], entries: List[dict], lang: str, source_name: str) -> int:
    """Insert entries not seen before; returns how many were new."""
    if not entries:
        return 0
    urls = [e["url"] for e in entries]
    known = set(s.scalars(select(NewsItem.url).where(NewsItem.url.in_(urls))))
    new = 0
    now = _now()
    for e in entries:
        if e["url"] in known:
            continue
        known.add(e["url"])
        s.add(NewsItem(source_id=src.id if src else None, source_name=source_name[:120], url=e["url"],
                       title=e["title"][:300], summary=e.get("summary", ""), image=e.get("image", "")[:700],
                       lang=lang, published_at=e.get("published_at") or now, added_at=now))
        new += 1
    return new


def _prune(s, source_id: int) -> None:
    ids = list(s.scalars(select(NewsItem.id).where(NewsItem.source_id == source_id, NewsItem.pinned.is_(False))
                         .order_by(NewsItem.published_at.desc()).offset(KEEP_PER_SOURCE)))
    if ids:
        s.query(NewsItem).filter(NewsItem.id.in_(ids)).delete(synchronize_session=False)


def refresh_source(source_id: int) -> dict:
    with SessionLocal() as s:
        src = s.get(NewsSource, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Source not found")
        url, name, lang = src.url, src.name, src.lang
    try:
        final, text = fetch(url)
        entries = parse_feed(text, final)
        err = ""
    except (FetchError, httpx.HTTPError) as e:
        entries, err = [], (str(e) or type(e).__name__)[:300]
    with SessionLocal.begin() as s:
        src = s.get(NewsSource, source_id)
        if not src:
            return {"id": source_id, "new": 0, "error": "removed"}
        new = 0
        if not err:
            try:
                new = _store(s, src, entries, lang, name)
                s.flush()
            except IntegrityError:          # another worker stored the same article first
                s.rollback()
                return {"id": source_id, "new": 0, "error": ""}
            _prune(s, source_id)
        src.last_fetched = _now()
        src.last_error = err
        src.item_count = s.scalar(select(func.count()).select_from(NewsItem).where(NewsItem.source_id == source_id)) or 0
    return {"id": source_id, "new": new, "error": err}


def refresh_all() -> int:
    """Background job: fetch every enabled source; returns new-article count."""
    with SessionLocal() as s:
        ids = list(s.scalars(select(NewsSource.id).where(NewsSource.enabled.is_(True))))
    total = 0
    for sid in ids:
        try:
            total += refresh_source(sid)["new"]
        except Exception:                    # noqa: BLE001 — one bad feed never stops the rest
            log.exception("news source %s failed", sid)
    return total


# ── Console endpoint ──────────────────────────────────────────────────────
@router.get("/news")
def list_news(q: str = Query("", max_length=80), limit: int = Query(24, ge=1, le=60),
              offset: int = Query(0, ge=0), _tenant: str = Depends(require_tenant)):
    with SessionLocal() as s:
        stmt = select(NewsItem).where(NewsItem.hidden.is_(False))
        if q.strip():
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(NewsItem.title.ilike(like), NewsItem.summary.ilike(like), NewsItem.source_name.ilike(like)))
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = s.scalars(stmt.order_by(NewsItem.pinned.desc(), NewsItem.published_at.desc(), NewsItem.id.desc())
                         .offset(offset).limit(limit)).all()
        sources = list(s.scalars(select(NewsItem.source_name).where(NewsItem.hidden.is_(False)).distinct()))
        return {"items": [_item_dict(r) for r in rows], "total": total, "sources": sorted(x for x in sources if x)}


# ── Operator endpoints ────────────────────────────────────────────────────
class SourceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    url: str = Field(..., min_length=8, max_length=500)
    lang: str = Field("en", max_length=8)


class SourcePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    enabled: Optional[bool] = None


class LinkIn(BaseModel):
    url: str = Field(..., min_length=8, max_length=700)
    lang: str = Field("zh", max_length=8)
    source_name: Optional[str] = Field(None, max_length=120)


class ItemPatch(BaseModel):
    pinned: Optional[bool] = None
    hidden: Optional[bool] = None


def _source_dict(x: NewsSource) -> dict:
    return {"id": x.id, "name": x.name, "url": x.url, "lang": x.lang, "enabled": x.enabled,
            "last_fetched": x.last_fetched, "last_error": x.last_error, "item_count": x.item_count}


@admin_router.get("/sources")
def admin_sources():
    with SessionLocal() as s:
        rows = s.scalars(select(NewsSource).order_by(NewsSource.id)).all()
        have = {r.url for r in rows}
        return {"sources": [_source_dict(r) for r in rows],
                "suggested": [x for x in SUGGESTED_SOURCES if x["url"] not in have]}


@admin_router.post("/sources")
def admin_add_source(body: SourceIn):
    url = body.url.strip()
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Feed URL must start with http:// or https://")
    try:
        with SessionLocal.begin() as s:
            src = NewsSource(name=body.name.strip(), url=url, lang=(body.lang or "en")[:8], created_at=_now())
            s.add(src)
            s.flush()
            sid = src.id
    except IntegrityError:
        raise HTTPException(status_code=409, detail="This feed is already added")
    result = refresh_source(sid)
    with SessionLocal() as s:
        return {"source": _source_dict(s.get(NewsSource, sid)), "new": result["new"], "error": result["error"]}


@admin_router.patch("/sources/{source_id}")
def admin_patch_source(source_id: int, body: SourcePatch):
    with SessionLocal.begin() as s:
        src = s.get(NewsSource, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Source not found")
        if body.name is not None:
            src.name = body.name.strip()
        if body.enabled is not None:
            src.enabled = body.enabled
        return {"source": _source_dict(src)}


@admin_router.delete("/sources/{source_id}")
def admin_delete_source(source_id: int):
    with SessionLocal.begin() as s:
        src = s.get(NewsSource, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Source not found")
        s.query(NewsItem).filter(NewsItem.source_id == source_id, NewsItem.pinned.is_(False)).delete(synchronize_session=False)
        s.delete(src)
    return {"ok": True}


@admin_router.post("/refresh")
def admin_refresh():
    with SessionLocal() as s:
        ids = list(s.scalars(select(NewsSource.id).where(NewsSource.enabled.is_(True))))
    results = [refresh_source(i) for i in ids]
    return {"new": sum(r["new"] for r in results), "results": results}


@admin_router.get("/items")
def admin_items(limit: int = Query(50, ge=1, le=200)):
    with SessionLocal() as s:
        rows = s.scalars(select(NewsItem).order_by(NewsItem.added_at.desc(), NewsItem.id.desc()).limit(limit)).all()
        return {"items": [_item_dict(r) for r in rows]}


@admin_router.post("/link")
def admin_add_link(body: LinkIn):
    url = body.url.strip()
    try:
        final, page = fetch(url)
    except (FetchError, httpx.HTTPError) as e:
        raise HTTPException(status_code=400, detail=f"Could not open that link: {e or type(e).__name__}")
    meta = parse_article(page, final)
    if not meta["title"]:
        raise HTTPException(status_code=400, detail="No title found on that page")
    # Keep the link the operator pasted (WeChat's final URL can carry session parameters).
    entry = {"title": meta["title"], "url": url[:700], "summary": meta["summary"], "image": meta["image"],
             "published_at": meta["published_at"]}
    with SessionLocal.begin() as s:
        existing = s.scalar(select(NewsItem).where(NewsItem.url == entry["url"]))
        if existing:
            existing.hidden = False
            return {"item": _item_dict(existing), "created": False}
        _store(s, None, [entry], (body.lang or "zh")[:8], (body.source_name or meta["source_name"] or "")[:120])
        s.flush()
        item = s.scalar(select(NewsItem).where(NewsItem.url == entry["url"]))
        return {"item": _item_dict(item), "created": True}


@admin_router.post("/items/{item_id}")
def admin_patch_item(item_id: int, body: ItemPatch):
    with SessionLocal.begin() as s:
        it = s.get(NewsItem, item_id)
        if not it:
            raise HTTPException(status_code=404, detail="Article not found")
        if body.pinned is not None:
            it.pinned = body.pinned
        if body.hidden is not None:
            it.hidden = body.hidden
        return {"item": _item_dict(it)}
