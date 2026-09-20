"""Shared constants, cache schema and HTTP plumbing for the Elhuyar scraper.

Cache is the durable intermediate: every HTTP body ever fetched lands in
work/cache.db and nothing else re-hits the network. Parsing and DB building
read only from it, so a parser bug costs a rebuild, never a re-crawl.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import NamedTuple

BASE = "https://hiztegiak.elhuyar.eus"

# --- TTS -------------------------------------------------------------------
# The dictionary site's own GET endpoint (tts-api.elhuyar.eus/api/from_hiztegia/)
# is DEAD: it answers 500 for every word, including the ones its own pages ask
# for.  The working synthesizer is the one behind ttsneuronala.elhuyar.eus:
#
#   GET  /en                             -> csrftoken cookie + csrfmiddlewaretoken
#   POST /ajax/get_audio_from_box        -> {"audio_path": "https://tts-api…mp3"}
#   GET  <audio_path>                    -> audio/mpeg
#
# Django CSRF is enforced (cookie + form token + a same-origin Referer), which
# is why this can only be driven server-side; see NOTES.md.
TTSN = "https://ttsneuronala.elhuyar.eus"
TTS_PAGE = TTSN + "/en"
TTS_AJAX = TTSN + "/ajax/get_audio_from_box"
# /ajax_get_voice_language_list, first (male) voice per language.
VOICE = {"eu": 29, "es": 37, "en": 39, "fr": 43}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "work")
OUT = os.path.join(ROOT, "out")
CACHE = os.path.join(WORK, "cache.db")

# --- opus transcode --------------------------------------------------------
# 16 kHz mono libopus/VoIP is the standard shape for synthesized speech; the
# sources are 22.05 kHz 56-64 kbps mp3, so nothing audible is above 8 kHz.
# 24 kbps VBR is ~2.4x smaller than the mp3 at no audible cost; 16 kbps is
# another 25% and starts to sound thin.
OPUS_KBPS = int(os.environ.get("ELH_OPUS_KBPS", "24"))
OPUS_HZ = int(os.environ.get("ELH_OPUS_HZ", "16000"))
OPUS_MIME = "audio/ogg"
MP3_MIME = "audio/mpeg"

# A source language maps to the page path (/<lang>/<word>) and to the
# translation blocks that page carries (div.hizkuntzaren_arabera.hizkuntza-<pair>).
PAIRS: dict[str, tuple[str, ...]] = {
    "eu": ("eu_es", "eu_fr", "eu_en"),
    "es": ("es_eu",),
    "en": ("en_eu",),
    "fr": ("fr_eu",),
}

DICT_NAME = {
    "eu": "Elhuyar eu-es,en,fr",
    "es": "Elhuyar es-eu",
    "en": "Elhuyar en-eu",
    "fr": "Elhuyar fr-eu",
}
# <SOURCE>-<TARGET>-<dictname>.  eu is one folder carrying three target
# languages; es is its primary target, so it leads the name.
DICT_DIR = {
    "eu": "eu-es-Elhuyar",
    "es": "es-eu-Elhuyar",
    "en": "en-eu-Elhuyar",
    "fr": "fr-eu-Elhuyar",
}
LANGS = tuple(PAIRS)
# fr_eu exists on the site but was not requested; it stays opt-in via --langs.
DEFAULT_LANGS = ("eu", "es", "en")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS word(
  lang TEXT NOT NULL, word TEXT NOT NULL,
  PRIMARY KEY(lang, word)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS page(
  lang TEXT NOT NULL, word TEXT NOT NULL,
  status INTEGER NOT NULL,   -- HTTP status, or 0 for a transport failure
  found INTEGER NOT NULL,    -- 1 = the page carries an article
  gz BLOB, ts INTEGER NOT NULL,
  PRIMARY KEY(lang, word)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS audio(
  lang TEXT NOT NULL, word TEXT NOT NULL,
  status INTEGER NOT NULL, mime TEXT, data BLOB, ts INTEGER NOT NULL,
  PRIMARY KEY(lang, word)) WITHOUT ROWID;
-- Spoken forms wanted, headwords AND example sentences alike: the synthesizer
-- takes plain text and does not care which it is, and an example whose text
-- equals a headword is legitimately the same clip.  `kind` is bookkeeping only.
CREATE TABLE IF NOT EXISTS ttsword(
  lang TEXT NOT NULL, word TEXT NOT NULL,
  PRIMARY KEY(lang, word)) WITHOUT ROWID;
"""

# Columns added after the first crawl; ALTER is the only way to reach a
# WITHOUT ROWID table without rewriting 3 GB of blobs.
MIGRATIONS = (
    ("audio", "url", "ALTER TABLE audio ADD COLUMN url TEXT"),
    ("audio", "opus", "ALTER TABLE audio ADD COLUMN opus BLOB"),
    ("ttsword", "kind", "ALTER TABLE ttsword ADD COLUMN kind TEXT NOT NULL DEFAULT 'w'"),
)


def cache(path: str = CACHE) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.executescript(SCHEMA)
    for table, col, ddl in MIGRATIONS:
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            db.execute(ddl)
    db.commit()
    return db


def gz(text: str) -> bytes:
    """Cache-side compression only. The wudict output stores bodies plain."""
    return gzip.compress(text.encode("utf-8"), 6, mtime=0)


def ungz(blob: bytes) -> str:
    return gzip.decompress(blob).decode("utf-8")


def client_kwargs() -> dict:
    return dict(
        headers={"User-Agent": UA, "Accept-Language": "eu,es;q=0.8,en;q=0.6"},
        timeout=90.0,
        follow_redirects=True,
        limits=__import__("httpx").Limits(
            max_connections=64, max_keepalive_connections=64
        ),
    )


@dataclass
class Progress:
    total: int
    label: str
    n: int = 0
    ok: int = 0
    bad: int = 0
    t0: float = 0.0
    last: float = 0.0

    def start(self) -> None:
        self.t0 = self.last = time.monotonic()

    def tick(self, ok: bool) -> None:
        self.n += 1
        self.ok += ok
        self.bad += not ok
        if time.monotonic() - self.last >= 2.0:
            self.render()

    def render(self) -> None:
        now = time.monotonic()
        self.last = now
        el = max(now - self.t0, 1e-6)
        rate = self.n / el
        eta = (self.total - self.n) / rate if rate else 0.0
        sys.stderr.write(
            f"\r{self.label}: {self.n}/{self.total} ok={self.ok} bad={self.bad} "
            f"{rate:5.1f}/s eta {eta / 3600:5.2f}h    "
        )
        sys.stderr.flush()

    def done(self) -> None:
        self.render()
        sys.stderr.write("\n")


class _Err(NamedTuple):
    item: object
    exc: BaseException


STATUS = 2  # every cache row is (lang, word, status, ...)


async def pump(items, work, conc: int, sink, prog: Progress):
    """Run `work` over `items` with `conc` workers, handing results to `sink`.

    Back-pressured: the producer blocks on a bounded queue, so an 8-hour crawl
    never materialises 172k coroutines or 172k pending rows.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=conc * 4)
    res: asyncio.Queue = asyncio.Queue(maxsize=conc * 8)
    prog.start()

    async def producer():
        for it in items:
            await q.put(it)
        for _ in range(conc):
            await q.put(None)

    async def worker():
        while True:
            it = await q.get()
            if it is None:
                await res.put(None)
                return
            try:
                out = await work(it)
            except Exception as exc:  # noqa: BLE001 - a crawl must not die on one URL
                # Do not fabricate a row: leaving the item uncached is what makes
                # the next run pick it up again.
                out = _Err(it, exc)
            await res.put(out)

    tasks = [asyncio.create_task(producer())] + [
        asyncio.create_task(worker()) for _ in range(conc)
    ]
    finished = 0
    try:
        while finished < conc:
            out = await res.get()
            if out is None:
                finished += 1
                continue
            if isinstance(out, _Err):
                prog.tick(False)
                if prog.bad <= 20:
                    sys.stderr.write(f"\n  {out.item}: {out.exc!r}\n")
                continue
            sink(out)
            prog.tick(out[STATUS] == 200)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    sink(None)  # flush
    prog.done()
