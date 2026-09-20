"""mp3 -> opus, cached back into work/cache.db.

The synthesizer only speaks mp3, but the deliverable wants the smallest thing a
browser can still play: 16 kHz mono libopus in VoIP mode is ~2.4x smaller than
the 22.05 kHz mp3 the API returns, and nothing in synthesized speech lives above
8 kHz anyway.

The result is cached (audio.opus) rather than produced during `build`, so the
262k ffmpeg invocations happen exactly once no matter how often the dictionaries
are rebuilt.  Resumable and idempotent: the work list is rows that have mp3 but
no opus.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from elh_common import OPUS_HZ, OPUS_KBPS, Progress, cache

BATCH = 200


def _args() -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "mp3", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", f"{OPUS_KBPS}k", "-vbr", "on",
        "-application", "voip", "-ac", "1", "-ar", str(OPUS_HZ),
        "-map_metadata", "-1", "-f", "ogg", "pipe:1",
    ]


def _one(item: tuple[str, str, bytes]) -> tuple[str, str, bytes | None]:
    lang, word, mp3 = item
    try:
        p = subprocess.run(_args(), input=mp3, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return lang, word, None
    out = p.stdout
    # OggS magic: a non-zero exit or a truncated pipe must not poison the cache
    # with a file the viewer would fail to decode.
    if p.returncode != 0 or out[:4] != b"OggS":
        return lang, word, None
    return lang, word, out


def transcode(langs: tuple[str, ...], jobs: int = 0, force: bool = False) -> None:
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found in $PATH")
    jobs = jobs or (os.cpu_count() or 4)
    db = cache()
    ph = ",".join("?" * len(langs))
    cond = "" if force else " AND opus IS NULL"
    n = db.execute(f"SELECT count(*) FROM audio WHERE status=200 AND data IS NOT NULL "
                   f"AND lang IN ({ph}){cond}", langs).fetchone()[0]
    if not n:
        print("transcode: nothing to do")
        return
    print(f"transcode: {n} clips at {OPUS_KBPS} kbps / {OPUS_HZ} Hz, {jobs} workers")

    prog = Progress(n, "opus")
    prog.start()
    buf: list[tuple[bytes, str, str]] = []
    bad = 0
    # A separate read cursor: the write connection commits underneath it, and
    # streaming keeps peak memory at (jobs + BATCH) clips instead of 6 GB.
    rd = cache()
    rows = rd.execute(
        f"SELECT lang, word, data FROM audio WHERE status=200 AND data IS NOT NULL "
        f"AND lang IN ({ph}){cond} ORDER BY lang, word", langs)

    def flush():
        if buf:
            db.executemany("UPDATE audio SET opus = ? WHERE lang = ? AND word = ?", buf)
            db.commit()
            buf.clear()

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for lang, word, opus in pool.map(_one, rows, chunksize=8):
            prog.tick(opus is not None)
            if opus is None:
                bad += 1
                continue
            buf.append((opus, lang, word))
            if len(buf) >= BATCH:
                flush()
    flush()
    prog.done()
    rd.close()
    if bad:
        print(f"  {bad} clips failed to transcode (left as mp3)", file=sys.stderr)
    for lang, c, m, o in db.execute(
            f"SELECT lang, count(*), sum(length(data)), sum(length(opus)) FROM audio "
            f"WHERE status=200 AND lang IN ({ph}) GROUP BY lang", langs):
        print(f"  {lang}: {c} clips, mp3 {(m or 0)/1e6:.1f} MB -> opus {(o or 0)/1e6:.1f} MB")
