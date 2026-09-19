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
SPEAKER = "\U0001F50A"


def audio_name(lang: str, word: str) -> str:
    """Stable, filesystem- and URL-safe resource name for one spoken form.

    Hashed rather than slugged because headwords carry spaces, apostrophes,
    slashes and accents; a hash keeps the name wudict resolves byte-identical
    to the name stored in media.db, with no encoding round-trip to get wrong.
    """
    h = hashlib.sha1(f"{lang}\x00{word}".encode("utf-8")).hexdigest()[:20]
    return f"audio/{lang}/{h}.mp3"


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


def _clean(node, audio: set[tuple[str, str]]) -> None:
    for xp in DROP_XPATH:
        for el in node.xpath(xp):
            _drop(el)
    for el in node.xpath(".//comment()"):
        _drop(el)
    # A <button> is the accordion toggle; its LABEL ("Lexical items", "Locutions")
    # is the only heading those sections have, so the element goes and the text stays.
    for el in node.xpath(".//button"):
        _unwrap(el)

    for a in node.xpath(".//a"):
        onclick = a.get("onclick", "") or ""
        href = (a.get("href") or "").strip()
        hit = CREATE_AUDIO.search(onclick)
        if hit:  # headword pronunciation -> a real link into media.db
            lang, word = hit.group(1), hit.group(2).replace("\\'", "'").replace("\\\\", "\\")
            word = word.strip()
            if not word:
                _drop(a)
                continue
            audio.add((lang, word))
            for kid in list(a):
                _drop(kid)
            a.attrib.clear()
            a.set("href", audio_name(lang, word))
            a.set("class", "wu-audio elh-tts")
            a.set("title", "Ahoskera / pronunciation")
            a.text = SPEAKER
            continue
        if "create_audio_adibideak" in onclick:
            _drop(a)  # example-sentence TTS is a POST API; not captured
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


def _html(node) -> str:
    return etree.tostring(node, encoding="unicode", method="html")


def parse(page_html: str, lang: str) -> tuple[str, str, set[tuple[str, str]]] | None:
    """Return (article_html, plain_text, audio_refs) or None when there is no entry."""
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
            _clean(node, audio)
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
.elh a.elh-tts { text-decoration: none; font-size: .9em; margin-left: .35em; opacity: .75; }
.elh a.elh-tts:hover { opacity: 1; }
.elh .padDefn { margin-left: 1.1em; }
.elh .text-muted { color: #6b7280; font-size: .95em; }
.elh .azpisarrera, .elh .adibideak { margin-left: 1em; }
"""
