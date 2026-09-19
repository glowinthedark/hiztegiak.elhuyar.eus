# Elhuyar Hiztegiak -> wudict

Scrape every entry of https://hiztegiak.elhuyar.eus/ and emit **wudict prepared
dictionaries** with **uncompressed** article bodies plus headword pronunciation
audio in `media.db`.

Delivered pairs: **eu > es,en,fr** · **es > eu** · **en > eu**
(`fr > eu` works but is opt-in: `--langs fr`).

## Run it

```
python3 elh.py sitemap                # enumerate headwords  (~30 s)
python3 elh.py pages   -c 16          # fetch entry pages    (~8 h, resumable)
python3 elh.py audio   -c 16          # plan + fetch TTS mp3 (resumable)
python3 elh.py build                  # -> out/Elhuyar-<lang>/
python3 elh.py status                 # counts per stage
python3 elh.py all -c 16              # the four above, in order
```

Every stage is **idempotent and resumable**: `work/cache.db` is the source of
truth, a re-run only does what is missing. Kill and restart freely. Add
`--retry-failed` to re-attempt rows whose fetch gave up (`status = 0`).
`build --limit N` makes a small smoke-test dictionary.
Install once: `uv pip install httpx lxml`.

## Layout

| path | role |
|---|---|
| `elh.py` | CLI dispatcher (the only entry point) |
| `elh_common.py` | constants, cache schema, `pump()` worker pool, `Progress` |
| `elh_fetch.py` | network stages: `sitemap` / `pages` / `plan_audio` / `audio` |
| `elh_parse.py` | page HTML -> clean article + FTS text + audio refs; `CSS` |
| `elh_build.py` | cache -> `text.db` + `media.db` + `info.txt` |
| `work/cache.db` | crawl cache (WAL). ~4 GB when full. Never delete casually. |
| `out/Elhuyar-{eu,es,en}/` | the deliverable dictionaries |
| `txori.html` | reference dump of `/eu/txori`, drives every parser rule |
| `NOTES.md` | site + wudict format facts. **Read this before changing anything.** |

`work/` and `out/` are derived; everything else is source.

## Facts you would otherwise re-derive (details in NOTES.md)

- Headwords come from `sitemap.{1..4}.xml` — no prefix crawling.
  eu 68,507 · es 66,265 · en 37,192 · fr 24,554 → 171,964 pages for eu+es+en+fr.
- A miss returns **HTTP 200**; detect by absence of `hizkuntzaren_arabera`.
- Server ceiling ≈ 5.7 req/s; concurrency beyond 8 does not help. 16 is the
  chosen setting (user decision).
- Headword TTS: `GET https://tts-api.elhuyar.eus/api/from_hiztegia/?word=&origin_language=&dest_language=`
  (recovered from the obfuscated `/static/js/base.js`). Plain HTTP, no browser.
  Example-sentence audio needs a CSRF POST — **deliberately out of scope**.
- wudict reads a body as compressed only when it starts with `0x00`; plain
  UTF-8 HTML is therefore format-legal and satisfies "no compression".
- `bword://` lookup links are parsed by string position and are **never
  percent-decoded** — `elh_parse` un-escapes them after serialization.

## User decisions already taken (do not re-ask)

1. Headword TTS audio only, no example-sentence audio.
2. Three separate dictionaries (`Elhuyar-eu`, `Elhuyar-es`, `Elhuyar-en`), each
   its own folder with `text.db` + `media.db` + `info.txt`.
3. 16 concurrent requests, no inter-request delay.
