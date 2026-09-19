#!/usr/bin/env python3
"""Elhuyar Hiztegiak -> wudict. One CLI, five resumable stages.

    ./elh.py sitemap                 enumerate headwords into work/cache.db
    ./elh.py pages [--langs eu,es]   fetch entry pages (skips what is cached)
    ./elh.py audio                   plan + fetch headword TTS clips
    ./elh.py build                   cache -> out/Elhuyar-<lang>/{text,media}.db
    ./elh.py status                  counts per stage
    ./elh.py all                     sitemap + pages + audio + build

Every stage is idempotent: the cache is the source of truth and re-running only
does the work that is missing. --retry-failed re-attempts rows whose fetch gave
up (status 0) or 5xx'd.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from elh_common import DEFAULT_LANGS, cache

LANGS_HELP = "comma-separated subset of eu,es,en,fr (default: %s)" % ",".join(DEFAULT_LANGS)


def _langs(s: str | None) -> tuple[str, ...]:
    if not s:
        return DEFAULT_LANGS
    out = tuple(x.strip() for x in s.split(",") if x.strip())
    bad = [x for x in out if x not in ("eu", "es", "en", "fr")]
    if bad:
        sys.exit(f"unknown language(s): {', '.join(bad)}")
    return out


def status(langs: tuple[str, ...]) -> None:
    db = cache()
    print(f"{'lang':<5}{'words':>9}{'fetched':>9}{'found':>9}{'miss':>8}"
          f"{'failed':>8}{'tts':>9}{'clips':>8}{'noaudio':>9}")
    for lang in langs:
        g = lambda q: db.execute(q, (lang,)).fetchone()[0]  # noqa: E731
        print(f"{lang:<5}"
              f"{g('SELECT count(*) FROM word WHERE lang=?'):>9}"
              f"{g('SELECT count(*) FROM page WHERE lang=?'):>9}"
              f"{g('SELECT count(*) FROM page WHERE lang=? AND found=1'):>9}"
              f"{g('SELECT count(*) FROM page WHERE lang=? AND found=0 AND status=200'):>8}"
              f"{g('SELECT count(*) FROM page WHERE lang=? AND status NOT IN (200,404)'):>8}"
              f"{g('SELECT count(*) FROM ttsword WHERE lang=?'):>9}"
              f"{g('SELECT count(*) FROM audio WHERE lang=? AND status=200'):>8}"
              f"{g('SELECT count(*) FROM audio WHERE lang=? AND status<>200'):>9}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="elh.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sitemap", "pages", "audio", "build", "status", "all"):
        p = sub.add_parser(name)
        p.add_argument("--langs", help=LANGS_HELP)
        if name in ("pages", "audio", "all"):
            p.add_argument("-c", "--concurrency", type=int, default=16)
            p.add_argument("--retry-failed", action="store_true",
                           help="re-attempt rows that previously gave up")
        if name in ("build", "all"):
            p.add_argument("--limit", type=int, default=0,
                           help="build only N entries per language (smoke test)")
    a = ap.parse_args()
    langs = _langs(a.langs)

    if a.cmd == "status":
        return status(langs)
    if a.cmd == "build":
        import elh_build
        for lang in langs:
            elh_build.build(lang, limit=a.limit)
        return

    import elh_fetch
    if a.cmd == "sitemap":
        return asyncio.run(elh_fetch.sitemap(langs))
    if a.cmd == "pages":
        return asyncio.run(elh_fetch.pages(langs, a.concurrency, a.retry_failed))
    if a.cmd == "audio":
        elh_fetch.plan_audio(langs)
        return asyncio.run(elh_fetch.audio(langs, a.concurrency, a.retry_failed))
    if a.cmd == "all":
        import elh_build
        asyncio.run(elh_fetch.sitemap(langs))
        asyncio.run(elh_fetch.pages(langs, a.concurrency, a.retry_failed))
        elh_fetch.plan_audio(langs)
        asyncio.run(elh_fetch.audio(langs, a.concurrency, a.retry_failed))
        for lang in langs:
            elh_build.build(lang, limit=a.limit)


if __name__ == "__main__":
    main()
