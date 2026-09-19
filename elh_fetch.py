"""Network stages: sitemap harvest, entry pages, headword TTS audio.

Every stage is resumable and idempotent - it computes its work list as
(wanted - already in cache) on each run, so interrupting with ^C costs at most
the uncommitted batch.
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import quote, unquote

import httpx

from elh_common import (BASE, PAIRS, Progress, TTS, TTS_DEST, cache, client_kwargs,
                        gz, pump, ungz)

BATCH = 300
RETRY_SLEEP = (2.0, 6.0, 15.0)
# A page that exists always carries at least one translation block; a miss
# renders "Bilatu duzun emaitza ez dago hiztegian" with HTTP 200, so the status
# code cannot be the test.
FOUND_RE = re.compile(r"hizkuntzaren_arabera")
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
ENTRY_RE = re.compile(r"^https?://hiztegiak\.elhuyar\.eus/([a-z]{2})/(.+)$")
# create_audio('eu_es','txori') - the pair and the exact string the site speaks.
AUDIO_RE = re.compile(r"create_audio\(\s*'([a-z]{2})_[a-z]{2}'\s*,\s*'(.*?)'\s*\)")


def _flusher(db, sql: str, batch: int = BATCH):
    """Return a sink that buffers rows and commits them in batches."""
    buf: list[tuple] = []

    def sink(row):
        if row is not None:
            buf.append(row)
            if len(buf) < batch:
                return
        if buf:
            db.executemany(sql, buf)
            db.commit()
            buf.clear()

    return sink


async def _get(client: httpx.AsyncClient, url: str, **kw) -> tuple[int, bytes, str]:
    """GET with bounded retries. Returns (status, body, note); status 0 = gave up."""
    note = ""
    for i, delay in enumerate((*RETRY_SLEEP, None)):
        try:
            r = await client.get(url, **kw)
            if r.status_code < 500 and r.status_code != 429:
                return r.status_code, r.content, ""
            note = f"http {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            note = repr(exc)
        if delay is None:
            break
        await asyncio.sleep(delay)
    return 0, b"", note


# --------------------------------------------------------------------------- sitemap

async def sitemap(langs: tuple[str, ...], conc: int = 4) -> None:
    db = cache()
    async with httpx.AsyncClient(**client_kwargs()) as client:
        st, body, note = await _get(client, f"{BASE}/sitemap.xml")
        if st != 200:
            raise SystemExit(f"sitemap index: {st} {note}")
        maps = LOC_RE.findall(body.decode("utf-8", "replace"))
        rows: list[tuple[str, str]] = []
        for m in maps:
            st, body, note = await _get(client, m)
            if st != 200:
                raise SystemExit(f"{m}: {st} {note}")
            for loc in LOC_RE.findall(body.decode("utf-8", "replace")):
                hit = ENTRY_RE.match(loc)
                if not hit:
                    continue
                lang, word = hit.group(1), unquote(hit.group(2)).strip()
                if lang in langs and word:
                    rows.append((lang, word))
            print(f"{m}: {len(rows)} headwords so far", flush=True)
    db.executemany("INSERT OR IGNORE INTO word(lang, word) VALUES(?, ?)", rows)
    db.commit()
    for lang, n in db.execute("SELECT lang, count(*) FROM word GROUP BY lang"):
        print(f"  {lang}: {n}")


# --------------------------------------------------------------------------- pages

def _page_todo(db, langs, retry_failed: bool) -> list[tuple[str, str]]:
    """Wanted headwords minus the ones already cached (failures optionally retried)."""
    cond = "(p.word IS NULL OR p.status = 0)" if retry_failed else "p.word IS NULL"
    return list(db.execute(
        f"""SELECT w.lang, w.word FROM word w
            LEFT JOIN page p ON p.lang = w.lang AND p.word = w.word
            WHERE w.lang IN ({','.join('?' * len(langs))}) AND {cond}
            ORDER BY w.lang, w.word""", langs))


async def pages(langs: tuple[str, ...], conc: int, retry_failed: bool) -> None:
    db = cache()
    todo = _page_todo(db, langs, retry_failed)
    if not todo:
        print("pages: nothing to do")
        return
    print(f"pages: {len(todo)} to fetch at concurrency {conc}")
    sink = _flusher(db, "INSERT OR REPLACE INTO page(lang, word, status, found, gz, ts) "
                        "VALUES(?, ?, ?, ?, ?, ?)")

    async with httpx.AsyncClient(**client_kwargs()) as client:
        async def work(item):
            lang, word = item
            url = f"{BASE}/{lang}/{quote(word, safe='')}"
            st, body, _ = await _get(client, url)
            if st != 200:
                return (lang, word, st, 0, None, int(time.time()))
            text = body.decode("utf-8", "replace")
            found = 1 if FOUND_RE.search(text) else 0
            return (lang, word, st, found, gz(text) if found else None, int(time.time()))

        await pump(todo, work, conc, sink, Progress(len(todo), "pages"))

    for lang, n, f in db.execute(
            "SELECT lang, count(*), sum(found) FROM page GROUP BY lang"):
        print(f"  {lang}: {n} fetched, {f} with an article")


# --------------------------------------------------------------------------- audio

def plan_audio(langs: tuple[str, ...]) -> None:
    """Scan cached pages for create_audio() calls -> the exact TTS work list."""
    db = cache()
    rows: list[tuple[str, str]] = []
    n = 0
    for lang, blob in db.execute(
            f"SELECT lang, gz FROM page WHERE found = 1 AND lang IN "
            f"({','.join('?' * len(langs))})", langs):
        n += 1
        html = ungz(blob)
        for src, word in AUDIO_RE.findall(html):
            word = word.replace("\\'", "'").replace("\\\\", "\\").strip()
            if word:
                rows.append((src, word))
        if len(rows) > 20000:
            db.executemany("INSERT OR IGNORE INTO ttsword(lang, word) VALUES(?, ?)", rows)
            db.commit()
            rows.clear()
    db.executemany("INSERT OR IGNORE INTO ttsword(lang, word) VALUES(?, ?)", rows)
    db.commit()
    print(f"scanned {n} pages")
    for lang, c in db.execute("SELECT lang, count(*) FROM ttsword GROUP BY lang"):
        print(f"  {lang}: {c} distinct spoken forms")


async def audio(langs: tuple[str, ...], conc: int, retry_failed: bool) -> None:
    db = cache()
    if not db.execute("SELECT 1 FROM ttsword LIMIT 1").fetchone():
        plan_audio(langs)
    cond = "(a.word IS NULL OR a.status = 0)" if retry_failed else "a.word IS NULL"
    todo = list(db.execute(
        f"""SELECT t.lang, t.word FROM ttsword t
            LEFT JOIN audio a ON a.lang = t.lang AND a.word = t.word
            WHERE t.lang IN ({','.join('?' * len(langs))}) AND {cond}
            ORDER BY t.lang, t.word""", langs))
    if not todo:
        print("audio: nothing to do")
        return
    print(f"audio: {len(todo)} to fetch at concurrency {conc}")
    sink = _flusher(db, "INSERT OR REPLACE INTO audio(lang, word, status, mime, data, ts) "
                        "VALUES(?, ?, ?, ?, ?, ?)", batch=100)

    async with httpx.AsyncClient(**client_kwargs()) as client:
        async def work(item):
            lang, word = item
            st, body, _ = await _get(client, TTS, params={
                "word": word, "origin_language": lang,
                "dest_language": TTS_DEST.get(lang, "eu")})
            mime = "audio/mpeg"
            # A synthesizer that fails still answers 200 with an HTML error page.
            if st == 200 and not (body[:3] == b"ID3" or body[:2] in (b"\xff\xfb", b"\xff\xf3",
                                                                    b"\xff\xf2", b"\xff\xe3")):
                st, body = 415, b""
            return (lang, word, st, mime if st == 200 else None,
                    body if st == 200 else None, int(time.time()))

        await pump(todo, work, conc, sink, Progress(len(todo), "audio"))

    for lang, n, b in db.execute(
            "SELECT lang, count(*), sum(length(data)) FROM audio WHERE status=200 GROUP BY lang"):
        print(f"  {lang}: {n} clips, {(b or 0)/1e6:.1f} MB")
