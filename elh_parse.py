"""Page HTML -> one clean wudict article per source language.

The site's markup is a Django template carrying share widgets, analytics
onclicks, ad slots and a JS-driven pair switcher that hides all but one
translation block. An offline article wants none of that: every pair is shown
at once, every behaviour becomes a plain link, and everything the reader cannot
use is dropped.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote

import lxml.html
from lxml import etree

from elh_common import PAIRS

PAIR_LABEL = {
    "eu_es": "eu &rsaquo; es", "eu_fr": "eu &rsaquo; fr", "eu_en": "eu &rsaquo; en",
    "es_eu": "es &rsaquo; eu", "en_eu": "en &rsaquo; eu", "fr_eu": "fr &rsaquo; eu",
}
CREATE_AUDIO = re.compile(r"create_audio\(\s*'([a-z]{2})_[a-z]{2}'\s*,\s*'(.*?)'\s*\)")
# create_audio_adibideak('eu','es','<origin>','<dest>');return false
# Lazily grouped and anchored on the terminator: the site leaves apostrophes
# UNESCAPED inside the dest argument, so quote counting mis-splits and a greedy
# tail swallows every later call on the page. See elh_fetch.ADIB_RE.
CREATE_ADIB = re.compile(
    r"create_audio_adibideak\(\s*'([a-z]{2})'\s*,\s*'([a-z]{2})'\s*,"
    r"\s*'(.*?)'\s*,\s*'(.*?)'\s*\)\s*;\s*return false", re.S)
BWORD_HREF = re.compile(r'href="bword://([^"]*)"')
ENTRY_HREF = re.compile(r"^/([a-z]{2})_[a-z]{2}/(.+)$")
# Attributes that only ever carried behaviour, layout or page-local identity.
DROP_ATTRS = ("onclick", "onload", "style", "target", "id", "name", "data-toggle",
              "data-target", "data-url", "data-text", "data-via", "data-lang",
              "data-related", "data-count", "data-hashtags", "aria-expanded",
              "aria-controls", "aria-hidden", "itemprop", "rel", "align", "width",
              "height", "border", "cellpadding", "cellspacing", "onerror")
# Subtrees with no offline meaning: sharing, feedback forms, ads, scripts.
DROP_XPATH = (
    ".//script", ".//style", ".//noscript", ".//iframe", ".//ins", ".//input",
    ".//img", ".//form",
    ".//*[contains(@class,'partekatuikonoak')]",
    ".//*[contains(@class,'float-right')]",
    ".//*[contains(@class,'loader')]",
    ".//a[starts-with(@href,'/sarrera_iruzkinak')]",
    ".//a[starts-with(@href,'/proposamenak')]",
    ".//a[starts-with(@href,'/hitz-gakoak')]",
)
SPEAKER = "\U0001F50A"      # headword pronunciation
SPEAKER_LOW = "\U0001F509"  # example sentence

# Offline first, online second.  `onerror` rather than a rejected play()
# promise: a rejection also means "autoplay blocked", which must NOT silently
# re-fetch over the network, while onerror fires exactly when the local
# resource is absent or undecodable - which is the case this exists for.
#
# This is an inline handler, not a <script>: wudict renders an article with a
# <script> in a srcdoc iframe instead of the shadow DOM, which would cost the
# host stylesheet and the lookup links.  Inline on* runs fine in a shadow root.
FALLBACK_JS = (
    "var t=this,a=new Audio(t.href);window._a=a;"
    "a.onerror=function(){var u=t.getAttribute('data-u');"
    "if(u){(window._a=new Audio(u)).play()}};a.play();return false"
)


def audio_name(lang: str, word: str, ext: str = "opus") -> str:
    """Stable, filesystem- and URL-safe resource name for one spoken form.

    Hashed rather than slugged because headwords carry spaces, apostrophes,
    slashes and accents; a hash keeps the name wudict resolves byte-identical
    to the name stored in media.db, with no encoding round-trip to get wrong.

    The extension is load-bearing for .opus: wudict's viewer rewrites any
    assetExt href into /res/<dictID>/..., but its capture-phase audio handler
    only claims (mp3|ogg|wav|spx|m4a).  .opus is in the first list and not the
    second, so the click reaches our own onclick and the online fallback works.
    A clip that could not be transcoded keeps .mp3 and is simply played by the
    viewer's handler instead - no fallback, but the bytes are there.
    """
    h = hashlib.sha1(f"{lang}\x00{word}".encode("utf-8")).hexdigest()[:20]
    return f"audio/{lang}/{h}.{ext}"


def _drop(el) -> None:
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail
    if tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(el)


def _unwrap(el) -> None:
    """Replace an element with its own children, keeping text flow intact."""
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    text = el.text or ""
    if text:
        if idx == 0:
            parent.text = (parent.text or "") + text
        else:
            prev = parent[idx - 1]
            prev.tail = (prev.tail or "") + text
    kids = list(el)
    for off, kid in enumerate(kids):
        parent.insert(idx + off, kid)
    tail = el.tail or ""
    if tail:
        if kids:
            kids[-1].tail = (kids[-1].tail or "") + tail
        elif idx == 0:
            parent.text = (parent.text or "") + tail
        else:
            parent[idx - 1].tail = (parent[idx - 1].tail or "") + tail
    parent.remove(el)


def _resolve(media, lang: str, text: str):
    """One spoken form -> (href, online_url) or None when nothing can play it.

    `media` is the build's view of what actually made it into media.db:
    (lang, text) -> (resource_name | None, online_url | None).  None means
    "assume everything is local", which is what a bare parse() wants.
    """
    if media is None:
        return audio_name(lang, text), None
    got = media.get((lang, text))
    if not got:
        return None
    name, url = got
    if name:
        return name, url
    # No bytes, but the synthesizer handed us a permanent URL for this exact
    # string: an online-only icon still beats no icon.
    return (url, None) if url else None


def _clean(node, audio: set[tuple[str, str]], media=None) -> None:
    for xp in DROP_XPATH:
        for el in node.xpath(xp):
            _drop(el)
    for el in node.xpath(".//comment()"):
        _drop(el)
    # A <button> is the accordion toggle; its LABEL ("Lexical items", "Locutions")
    # is the only heading those sections have, so the element goes and the text stays.
    for el in node.xpath(".//button"):
        _unwrap(el)

    # Deferred: the attribute sweep below strips on* and data-* wholesale, so
    # the audio anchors are emptied here and dressed after it has run.
    pending: list[tuple] = []
    for a in node.xpath(".//a"):
        onclick = a.get("onclick", "") or ""
        href = (a.get("href") or "").strip()
        hit = CREATE_AUDIO.search(onclick)
        adib = None if hit else CREATE_ADIB.search(onclick)
        if hit or adib:
            if hit:
                lang, word, kind = hit.group(1), hit.group(2), "w"
            else:
                lang, word, kind = adib.group(1), adib.group(3), "x"
            word = word.replace("\\'", "'").replace("\\\\", "\\").strip()
            got = _resolve(media, lang, word) if word else None
            if got is None:
                _drop(a)
                continue
            audio.add((lang, word))
            for kid in list(a):
                _drop(kid)
            a.attrib.clear()
            pending.append((a, kind, *got))
            continue
        ref = ENTRY_HREF.match(href)
        if ref:  # cross-reference to another entry -> wudict headword lookup
            target = ref.group(2)
            a.attrib.clear()
            a.set("href", "bword://" + target)
            a.set("class", "wu-xref")
            continue
        if href.startswith(("#", "javascript:")) or not href:
            _unwrap(a)
            continue
        if href.startswith("/"):
            a.set("href", "https://hiztegiak.elhuyar.eus" + href)

    for el in node.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            if attr in DROP_ATTRS or attr.startswith(("data-", "aria-", "on")):
                del el.attrib[attr]
        cls = el.get("class")
        if cls is not None:
            keep = " ".join(c for c in cls.split()
                            if not c.startswith(("col-", "order-", "mt-", "mb-", "badge",
                                                 "collapse", "float-", "btn-", "fa", "fab",
                                                 "fas")))
            if keep:
                el.set("class", keep)
            else:
                del el.attrib["class"]
    # <i class="fa ..."> icons lose their font offline; whatever is left is noise.
    for el in node.xpath(".//i[not(node())]"):
        _drop(el)

    for a, kind, href, url in pending:
        a.set("href", href)
        if url:
            a.set("data-u", url)
            a.set("onclick", FALLBACK_JS)
        if kind == "w":
            a.set("class", "wu-audio elh-tts")
            a.set("title", "Ahoskera / pronunciation")
            a.text = SPEAKER
        else:
            a.set("class", "wu-audio elh-ex")
            a.set("title", "Adibidea entzun / play example")
            a.text = SPEAKER_LOW


def _html(node) -> str:
    return etree.tostring(node, encoding="unicode", method="html")


def parse(page_html: str, lang: str,
          media=None) -> tuple[str, str, set[tuple[str, str]]] | None:
    """Return (article_html, plain_text, audio_refs) or None when there is no entry.

    `media` maps (lang, spoken text) -> (resource name | None, online URL | None)
    and decides, per icon, whether it links a local clip (with an online
    fallback), links straight out to the network, or is dropped entirely.
    """
    doc = lxml.html.fromstring(page_html)
    main = doc.xpath("//*[@id='erdiko_zutabea']")
    if not main:
        return None
    main = main[0]
    audio: set[tuple[str, str]] = set()
    sections: list[str] = []

    for pair in PAIRS[lang]:
        nodes = main.xpath(
            "./*[contains(concat(' ', normalize-space(@class), ' '), ' hizkuntzaren_arabera ')]"
            f"[contains(concat(' ', normalize-space(@class), ' '), ' hizkuntza-{pair} ')]")
        if not nodes:
            continue
        body: list[str] = []
        for node in nodes:
            node = lxml.html.fromstring(_html(node))
            _clean(node, audio, media)
            for h1 in node.xpath(".//h1"):
                h1.tag = "div"
                h1.set("class", "wu-k elh-hw")
            if node.tag in ("div", "ul") and node.text_content().strip() == "" and not node.xpath(".//a"):
                continue
            body.append(_html(node))
        if not body:
            continue
        sections.append(
            f'<div class="elh-pair" data-pair="{pair}">'
            f'<div class="elh-pair-label">{PAIR_LABEL[pair]}</div>'
            + "".join(body) + "</div>")

    if not sections:
        return None
    article = ('<link rel="stylesheet" href="elhuyar.css">'
               '<div class="elh">' + "".join(sections) + "</div>")
    # `bword://Some Headword` IS NOT A URL - wudict parses it by string position
    # and never percent-decodes it (server/web/index.html). lxml's serializer
    # escapes spaces and non-ASCII in href, so undo that for lookup targets only.
    article = BWORD_HREF.sub(
        lambda m: 'href="bword://' + unquote(m.group(1)) + '"', article)
    text = lxml.html.fromstring(article).text_content()
    text = re.sub(r"\s+", " ", text).strip()
    return article, text, audio


CSS = """/* Elhuyar hiztegiak - offline article styles (wudict) */
.elh { line-height: 1.45; }
.elh-pair { margin: 0 0 1.2em 0; }
.elh-pair-label { font: 600 0.78em/1.6 system-ui, sans-serif; letter-spacing: .08em;
  text-transform: uppercase; color: #6b7280; border-bottom: 1px solid #e5e7eb;
  margin: .4em 0 .5em; }
.elh .elh-hw { font-size: 1.35em; font-weight: 700; margin: .1em 0; display: inline-block; }
.elh .emaitza-lerroa { margin-top: .2em; }
.elh ul.hizkuntzaren_arabera { list-style: none; margin: 0; padding: 0; }
.elh ul.hizkuntzaren_arabera > li { margin: .35em 0; padding: 0; }
.elh p.lehena { margin: .15em 0; }
.elh .fina { font-weight: 700; color: #b45309; margin-right: .25em; }
.elh em { color: #1f6f6b; font-style: italic; }
.elh a.wu-xref { text-decoration: none; }
.elh a.wu-xref strong { font-weight: 600; }
.elh a.elh-tts, .elh a.elh-ex { text-decoration: none; margin-left: .35em; opacity: .75;
  cursor: pointer; }
.elh a.elh-tts { font-size: .9em; }
.elh a.elh-ex { font-size: .78em; opacity: .55; }
.elh a.elh-tts:hover, .elh a.elh-ex:hover { opacity: 1; }
.elh .padDefn { margin-left: 1.1em; }
.elh .text-muted { color: #6b7280; font-size: .95em; }
.elh .azpisarrera, .elh .adibideak { margin-left: 1em; }
"""
