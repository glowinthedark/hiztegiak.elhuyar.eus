"""Cache -> wudict dictionary folders (text.db + media.db + info.txt).

Schema and meta keys mirror internal/store/{ingest,media}.go of wudict at
schemaVersion 1. Article bodies are stored PLAIN: wudict discriminates a
compressed body by a leading NUL byte (store/compress.go), so text that never
starts with NUL is read back verbatim with no flag to set - body_encoding is
recorded as "plain" for the human reading meta.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid as uuidlib

from elh_common import (CACHE, DICT_DIR, DICT_NAME, OUT, PAIRS, cache, ungz)
from elh_parse import CSS, audio_name, parse

SCHEMA_VERSION = 1      # store.schemaVersion
FOLD_VERSION = 1        # dict.FoldVersion
MARKUP_VERSION = 2      # artmark.Version

TEXT_SCHEMA = f"""
PRAGMA user_version = {SCHEMA_VERSION};
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entry(id INTEGER PRIMARY KEY, w TEXT NOT NULL, m TEXT NOT NULL);
CREATE TABLE alias(w TEXT NOT NULL, entry_id INTEGER NOT NULL REFERENCES entry(id));
CREATE VIRTUAL TABLE entry_fts USING fts5(
  w, txt, content='', columnsize=0,
  tokenize='unicode61 remove_diacritics 2');
"""
MEDIA_SCHEMA = f"""
PRAGMA user_version = {SCHEMA_VERSION};
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE resource(name TEXT PRIMARY KEY, mime TEXT, data BLOB);
"""


def _fresh(path: str, schema: str) -> sqlite3.Connection:
    tmp = path + ".tmp"
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    db = sqlite3.connect(tmp)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.executescript(schema)
    return db


def _finish(db: sqlite3.Connection, path: str) -> None:
    db.commit()
    db.close()
    os.replace(path + ".tmp", path)


def build(lang: str, outdir: str | None = None, limit: int = 0) -> None:
    src = cache()
    outdir = outdir or os.path.join(OUT, DICT_DIR[lang])
    os.makedirs(outdir, exist_ok=True)
    text_path = os.path.join(outdir, "text.db")
    media_path = os.path.join(outdir, "media.db")
    dict_uuid = uuidlib.uuid4().hex

    text = _fresh(text_path, TEXT_SCHEMA)
    q = ("SELECT word, gz FROM page WHERE lang = ? AND found = 1 ORDER BY word"
         + (f" LIMIT {int(limit)}" if limit else ""))
    rows = src.execute(q, (lang,))

    entries = 0
    skipped = 0
    wanted: set[tuple[str, str]] = set()
    t0 = time.monotonic()
    ins_e = "INSERT INTO entry(id, w, m) VALUES(?, ?, ?)"
    ins_f = "INSERT INTO entry_fts(rowid, w, txt) VALUES(?, ?, ?)"
    ebuf: list[tuple] = []
    fbuf: list[tuple] = []
    for word, blob in rows:
        try:
            got = parse(ungz(blob), lang)
        except Exception as exc:  # noqa: BLE001 - one malformed page is not a build failure
            print(f"\n  parse failed: {lang}/{word}: {exc!r}", file=sys.stderr)
            got = None
        if got is None:
            skipped += 1
            continue
        article, plain, audio = got
        entries += 1
        wanted |= audio
        ebuf.append((entries, word, article))
        fbuf.append((entries, word, plain))
        if len(ebuf) >= 500:
            text.executemany(ins_e, ebuf)
            text.executemany(ins_f, fbuf)
            ebuf.clear()
            fbuf.clear()
            if entries % 5000 == 0:
                el = time.monotonic() - t0
                sys.stderr.write(f"\r  {lang}: {entries} entries ({entries/max(el,1e-6):.0f}/s)   ")
                sys.stderr.flush()
    text.executemany(ins_e, ebuf)
    text.executemany(ins_f, fbuf)
    sys.stderr.write("\n")

    meta = {
        "dict_uuid": dict_uuid,
        "name": DICT_NAME[lang],
        "format": "html",
        "source_path": "https://hiztegiak.elhuyar.eus/" + lang + "/",
        "description": ("Elhuyar Hiztegiak, scraped from hiztegiak.elhuyar.eus. "
                        "Pairs: " + ", ".join(PAIRS[lang]) + "."),
        "entry_count": str(entries),
        "sub_entries": "0",
        "ingest_level": "text",
        "has_trigram": "0",
        "fold_version": str(FOLD_VERSION),
        "markup_version": str(MARKUP_VERSION),
        "body_encoding": "plain",
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index_lang": lang,
        "contents_lang": ",".join(sorted({p.split("_")[1] for p in PAIRS[lang]})),
    }
    text.executemany("INSERT INTO meta(key, value) VALUES(?, ?)", sorted(meta.items()))
    text.execute("CREATE INDEX idx_entry_w ON entry(w COLLATE NOCASE)")
    text.execute("CREATE INDEX idx_alias_w ON alias(w COLLATE NOCASE)")
    text.execute("INSERT INTO entry_fts(entry_fts) VALUES('optimize')")
    _finish(text, text_path)

    # ---- media -----------------------------------------------------------
    media = _fresh(media_path, MEDIA_SCHEMA)
    media.executemany("INSERT INTO meta(key, value) VALUES(?, ?)", [
        ("dict_uuid", dict_uuid), ("name", DICT_NAME[lang]), ("format", "html")])
    media.execute("INSERT INTO resource(name, mime, data) VALUES(?, ?, ?)",
                  ("elhuyar.css", "text/css; charset=utf-8", CSS.encode("utf-8")))
    clips = 0
    missing = 0
    mbuf: list[tuple] = []
    have = {(l, w) for l, w in src.execute(
        "SELECT lang, word FROM audio WHERE status = 200")}
    for alang, aword in sorted(wanted):
        if (alang, aword) not in have:
            missing += 1
            continue
        row = src.execute("SELECT mime, data FROM audio WHERE lang = ? AND word = ?",
                          (alang, aword)).fetchone()
        mbuf.append((audio_name(alang, aword), row[0] or "audio/mpeg", row[1]))
        clips += 1
        if len(mbuf) >= 400:
            media.executemany(
                "INSERT OR REPLACE INTO resource(name, mime, data) VALUES(?, ?, ?)", mbuf)
            mbuf.clear()
    media.executemany(
        "INSERT OR REPLACE INTO resource(name, mime, data) VALUES(?, ?, ?)", mbuf)
    _finish(media, media_path)

    tsize = os.path.getsize(text_path)
    msize = os.path.getsize(media_path)
    with open(os.path.join(outdir, "info.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"""# wudict - prepared dictionary
# This folder is one dictionary. Copy, move or zip it as a unit;
# drop it into your dictionary folder to use it on another machine.
# Regenerated by elh.py build - edits are overwritten.

name = {DICT_NAME[lang]}
format = html
entries = {entries}
index = full text (headwords + article text)
media = media.db ({msize/1e6:.1f} MB, {clips} pronunciation clips)
source = https://hiztegiak.elhuyar.eus/{lang}/
pairs = {", ".join(PAIRS[lang])}
body_encoding = plain (uncompressed)
imported = {meta["created"]}
uuid = {dict_uuid}

# files: text.db (articles + search index), media.db (audio + css), info.txt
""")
    print(f"{DICT_NAME[lang]}: {entries} entries ({tsize/1e6:.1f} MB), "
          f"{clips} clips ({msize/1e6:.1f} MB), {skipped} pages without an article, "
          f"{missing} clips missing -> {outdir}")
