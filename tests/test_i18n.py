"""
Language-switch guard rails: the site must read entirely in Chinese when the
console is in Chinese, and entirely in English when it is in English.

These checks are static (no browser), so they run with the rest of the suite:

* the console's I18N dictionary has every key in both languages, and every
  key the page uses (data-i18n*, T('…')) exists;
* English strings contain no Chinese;
* visible text written straight into the HTML is either translated
  (data-i18n…) or explicitly marked as language-neutral / filled at runtime
  (data-noi18n), and so are placeholder / title / aria-label attributes;
* Chinese literals in the page's JavaScript only appear in Chinese-specific
  code (a `…zh…` branch or a `*_ZH` table), never as a hard-coded label;
* every error message the API raises has a Chinese translation;
* the browser extension's popup has the same keys in both languages.
"""
import ast
import itertools
import pathlib
import re
from html.parser import HTMLParser

from backend import i18n

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CJK = re.compile(r"[㐀-鿿！-～　-〿]")


# ── a tiny reader for the JS object literals that hold the dictionary ─────────
def _skip_ws(src, i):
    while i < len(src):
        if src[i].isspace():
            i += 1
        elif src.startswith("//", i):
            i = src.index("\n", i)
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
        else:
            break
    return i


def _read_string(src, i):
    q = src[i]
    i += 1
    out = []
    while src[i] != q:
        if src[i] == "\\":
            nxt = src[i + 1]
            out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
            i += 2
        else:
            out.append(src[i])
            i += 1
    return "".join(out), i + 1


def _read_value(src, i):
    """A string, a nested object, or anything else (skipped, returned as None)."""
    i = _skip_ws(src, i)
    if src[i] in "'\"`":
        return _read_string(src, i)
    if src[i] == "{":
        return _read_object(src, i)
    depth = 0
    while True:
        c = src[i]
        if c in "'\"`":
            _, i = _read_string(src, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return None, i
            depth -= 1
        elif c == "," and depth == 0:
            return None, i
        i += 1


def _read_object(src, i):
    assert src[i] == "{"
    out = {}
    i += 1
    while True:
        i = _skip_ws(src, i)
        if src[i] == "}":
            return out, i + 1
        if src[i] in "'\"":
            key, i = _read_string(src, i)
        else:
            m = re.compile(r"[\w$]+").match(src, i)
            key, i = m.group(0), m.end()
        i = _skip_ws(src, i)
        assert src[i] == ":", src[i - 40:i + 40]
        value, i = _read_value(src, i + 1)
        out[key] = value
        i = _skip_ws(src, i)
        if src[i] == ",":
            i += 1


def _dictionary():
    """{'en': {...}, 'zh': {...}} merged from `const I18N = {…}` and every
    `Object.assign(I18N.xx, {…})` block; also the character spans they cover."""
    spans = []
    start = INDEX.index("const I18N = {")
    obj_start = INDEX.index("{", start)
    base, end = _read_object(INDEX, obj_start)
    spans.append((start, end))
    langs = {k: dict(v) for k, v in base.items()}
    for m in re.finditer(r"Object\.assign\(I18N\.(\w+),\s*\{", INDEX):
        extra, end = _read_object(INDEX, m.end() - 1)
        langs.setdefault(m.group(1), {}).update(extra)
        spans.append((m.start(), end))
    return langs, spans


LANGS, DICT_SPANS = _dictionary()
EN, ZH = LANGS["en"], LANGS["zh"]


def test_both_languages_have_the_same_keys():
    assert set(EN) - set(ZH) == set(), "keys missing in zh"
    assert set(ZH) - set(EN) == set(), "keys missing in en"
    assert set(EN["pageTitles"]) == set(ZH["pageTitles"])


def test_no_chinese_in_english_strings():
    leaks = {k: v for k, v in EN.items() if isinstance(v, str) and CJK.search(v)}
    assert leaks == {}


def test_every_key_the_page_uses_exists_in_both_languages():
    used = set(re.findall(r'data-i18n(?:-html|-title|-ph|-aria)?="([^"]+)"', INDEX))
    used |= set(re.findall(r"\bT\('([\w.]+)'", INDEX)) - {k for k in re.findall(r"\bT\('([\w.]+\.)'", INDEX)}
    used = {k for k in used if not k.endswith(".")}          # T('act.' + x): a prefix, checked below
    missing = sorted(k for k in used if k not in EN or k not in ZH)
    assert missing == []


def test_dynamic_key_families_are_complete():
    """T('prefix.' + value) families: whatever exists in English exists in Chinese."""
    for prefix in sorted(set(re.findall(r"\bT\('([\w.]+\.)'\s*\+", INDEX))):
        en = {k for k in EN if k.startswith(prefix)}
        zh = {k for k in ZH if k.startswith(prefix)}
        assert en, prefix
        assert en == zh, prefix


# ── visible text in the HTML must be translatable ─────────────────────────────
# Brand / product names, units and codes read the same in both languages.
NEUTRAL = re.compile(
    r"^(Lumio|GMV|ROI|CSV|KOL|AI|RM\d+(?:\.\d+)?|\$?[\d.,]+[KMx%]?|[\d.,]+\s*(?:s|%)|https?://\S*|[\w.]+@[\w.]+|"
    r"\d+[smh]|\d+(?:\.\d+)?x|—|–|-|·|✓|×|\+|/|•)$")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
ATTRS = {"placeholder": "data-i18n-ph", "title": "data-i18n-title", "aria-label": "data-i18n-aria"}


class _TextAudit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems = [], []

    def _covered(self):
        return any(("data-i18n-html" in a or "data-noi18n" in a) for _, a in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in ("script", "style", "svg") and not self._skip():
            for attr, key in ATTRS.items():
                v = a.get(attr)
                if (v and re.search(r"[A-Za-z]{2,}|[㐀-鿿]", v) and key not in a
                        and "data-noi18n" not in a and not self._covered()
                        and not NEUTRAL.match(v.strip()) and "${" not in v):
                    self.problems.append(f"line {self.getpos()[0]}: {attr}={v!r}")
        if tag not in VOID:
            self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def _skip(self):
        return any(t in ("script", "style", "head", "svg", "noscript") for t, _ in self.stack)

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text or self._skip() or not re.search(r"[A-Za-z]{2,}|[㐀-鿿]", text):
            return
        tag, a = self.stack[-1] if self.stack else ("", {})
        if "data-i18n" in a or self._covered() or NEUTRAL.match(text):
            return
        if "material-symbols-outlined" in (a.get("class") or ""):
            return                                  # icon ligature names, not words
        self.problems.append(f"line {self.getpos()[0]}: <{tag}> {text[:80]!r}")


def _audit(html):
    p = _TextAudit()
    p.feed(html)
    return p.problems


def test_console_html_has_no_untranslated_text():
    assert _audit(INDEX) == []


def test_page_javascript_has_no_hardcoded_chinese_labels():
    """Chinese belongs in the I18N dictionary (or a clearly Chinese-only branch)."""
    problems = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", INDEX, re.S):
        base = m.start(1)
        in_zh_table = False
        offset = base
        for line in m.group(1).split("\n"):
            pos, offset = offset, offset + len(line) + 1
            if any(a <= pos < b for a, b in DICT_SPANS):
                continue
            code = re.sub(r"(^|\s)//.*$", "", line)
            code = re.sub(r"\[[^\]]*\\u[0-9a-fA-F]{4}[^\]]*\]", "", code)   # regex character ranges
            if re.search(r"\b\w+_ZH\s*=\s*\{", code):
                in_zh_table = True
            if in_zh_table:
                if code.strip().startswith("}"):
                    in_zh_table = False
                continue
            if CJK.search(code) and not re.search(r"zh|ZH", code):
                problems.append(f"line {INDEX.count(chr(10), 0, pos) + 1}: {line.strip()[:100]}")
    assert problems == []


# ── API error messages ────────────────────────────────────────────────────────
def _error_messages():
    """Every literal message passed as `detail=` (HTTPException, ratelimit) and
    every module-level *_DETAIL constant, with f-string holes as placeholders."""
    for f in sorted((ROOT / "backend").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.endswith("_DETAIL") for t in n.targets):
                yield f.name, n.lineno, n.value
            if isinstance(n, ast.Call):
                for kw in n.keywords:
                    if kw.arg == "detail":
                        yield f.name, n.lineno, kw.value
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                    and n.targets[0].id in ("too_many", "slow"):
                yield f.name, n.lineno, n.value


def _candidates(node):
    """Concrete strings for a message node (f-string holes filled a few ways)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        holes = sum(1 for p in node.values if not isinstance(p, ast.Constant))
        outs = []
        for fills in itertools.product(("1", "s", "", "pending", "Pro", "beauty, fashion"), repeat=min(holes, 3)):
            it = iter(fills + ("1",) * max(0, holes - 3))
            outs.append("".join(p.value if isinstance(p, ast.Constant) else next(it) for p in node.values))
        return outs
    return None   # a variable — its value is checked where it is defined


def test_every_api_error_has_a_chinese_translation():
    missing = []
    for fname, line, node in _error_messages():
        cands = _candidates(node)
        if cands is None:
            continue
        if not any(CJK.search(i18n.localise(c, "zh") or "") for c in cands):
            missing.append(f"{fname}:{line} {cands[0][:90]!r}")
    assert missing == []


def test_validation_errors_are_one_readable_sentence():
    from fastapi.testclient import TestClient
    from backend.main import app
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": 5}, headers={"X-Lang": "zh"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and CJK.search(r.json()["detail"])
    r = client.post("/auth/login", json={"email": 5}, headers={"X-Lang": "en"})
    assert isinstance(r.json()["detail"], str) and not CJK.search(r.json()["detail"])


# ── browser extension ─────────────────────────────────────────────────────────
def test_extension_popup_has_both_languages():
    src = (ROOT / "extension" / "popup.js").read_text(encoding="utf-8")
    start = src.index("const TEXTS = {")
    texts, _ = _read_object(src, src.index("{", start))
    assert set(texts) == {"zh", "en"}
    assert set(texts["zh"]) == set(texts["en"])
    assert not [k for k, v in texts["en"].items() if isinstance(v, str) and CJK.search(v) and k not in ("switchTo", "switchTitle")]
    labels = src[src.index("function labels()"):src.index("async function render()")]
    for el in re.findall(r"\['([\w-]+)', '(\w+)'\]", labels):
        assert el[1] in texts["en"], el


def test_extension_guide_pairs_every_block():
    html = (ROOT / "extension" / "guide.html").read_text(encoding="utf-8")
    assert html.count('data-l="zh"') == html.count('data-l="en"') > 0


def test_legal_page_headings_have_chinese():
    html = (ROOT / "frontend" / "legal.html").read_text(encoding="utf-8")
    heads = re.findall(r"<(h[123])([^>]*)>", html)
    assert heads and all("data-zh=" in attrs for _, attrs in heads)
    assert html.count('class="zh') >= html.count("<h3")


def test_creator_reasons_come_in_both_languages():
    from fastapi.testclient import TestClient
    from backend.main import app
    client = TestClient(app)
    tok = client.post("/auth/demo").json()["token"]
    r = client.post("/optimize", json={"budget": 3000, "countries": [], "category": "beauty"},
                    headers={"Authorization": "Bearer " + tok, "X-Lang": "en"}).json()
    k = r["selected_kols"][0]
    assert k["reasons"] == k["reasons_i18n"]["en"]
    assert all(CJK.search(x) for x in k["reasons_i18n"]["zh"])
    assert not any(CJK.search(x) for x in k["reasons_i18n"]["en"])


def test_console_language_is_saved_on_the_account():
    import uuid
    from fastapi.testclient import TestClient
    from backend import repo
    from backend.main import app
    client = TestClient(app)
    email = f"lang_{uuid.uuid4().hex[:8]}@example.com"
    tok = client.post("/auth/register", json={"email": email, "password": "secret123"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    assert client.post("/auth/lang", json={"lang": "zh"}, headers=h).json() == {"lang": "zh"}
    assert repo.user_find(email)["lang"] == "zh"
    client.post("/auth/lang", json={"lang": "en"}, headers=h)
    assert repo.user_find(email)["lang"] == "en"
