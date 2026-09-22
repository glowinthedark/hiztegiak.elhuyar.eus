# Elhuyar Hiztegiak -> wudict

Scrape every entry of https://hiztegiak.elhuyar.eus/ and emit **wudict prepared
dictionaries** with **uncompressed** article bodies plus pronunciation audio -
headwords *and* example sentences, opus-encoded - in `media.db`, each icon
falling back to the online endpoint when the local clip is missing.

Delivered pairs: **eu > es,en,fr** · **es > eu** · **en > eu**
(`fr > eu` works but is opt-in: `--langs fr`).

## Run it

```
python3 elh.py sitemap                # enumerate headwords  (~30 s)
python3 elh.py pages   -c 16          # fetch entry pages    (~8 h, resumable)
python3 elh.py audio   -c 16          # plan + fetch TTS mp3 (~15.5 h, resumable)
python3 elh.py transcode              # mp3 -> opus (ffmpeg, cached in cache.db)
python3 elh.py build                  # -> out/<src>-<tgt>-Elhuyar/
python3 elh.py status                 # counts per stage
python3 elh.py all -c 16              # the five above, in order
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
| `elh_transcode.py` | `audio.data` (mp3) -> `audio.opus`, parallel ffmpeg |
| `elh_build.py` | cache -> `text.db` + `media.db` + `info.txt` |
| `work/cache.db` | crawl cache (WAL). ~6 GB when full (mp3 + opus). Never delete casually. |
| `out/{eu-es,es-eu,en-eu}-Elhuyar/` | the deliverable dictionaries |
| `txori.html` | reference dump of `/eu/txori`, drives every parser rule |
| `NOTES.md` | site + wudict format facts. **Read this before changing anything.** |

`work/` and `out/` are derived; everything else is source.

## Facts you would otherwise re-derive (details in NOTES.md)

- Headwords come from `sitemap.{1..4}.xml` — no prefix crawling.
  eu 68,507 · es 66,265 · en 37,192 · fr 24,554 → 171,964 pages for eu+es+en+fr.
- A miss returns **HTTP 200**; detect by absence of `hizkuntzaren_arabera`.
- Server ceiling ≈ 5.7 req/s; concurrency beyond 8 does not help. 16 is the
  chosen setting (user decision).
- **The `tts-api.elhuyar.eus/api/from_hiztegia/` GET endpoint is DEAD** (500 for
  every word, broken on the live site too). TTS now goes through
  `POST https://ttsneuronala.elhuyar.eus/ajax/get_audio_from_box` (Django CSRF:
  cookie + masked form token from `GET /en`, same-origin Referer), which
  answers `{"audio_path": "https://tts-api.elhuyar.eus/audioa_entzun?path=<md5>.mp3"}`.
  Voices: eu 29 · es 37 · en 39 · fr 43. Same call serves headwords *and*
  example sentences. ~4.7 req/s, flat beyond concurrency 16.
- The resolved `audioa_entzun` URL is stored in `audio.url` and baked into each
  anchor as `data-u`: it is the **only** thing a browser can reach cross-origin
  (both POST endpoints fail CORS/CSRF from a foreign origin).
- `.opus` is chosen deliberately: it is in wudict's `assetExt` (so hrefs get
  rewritten to `/res/…`) but *absent* from its capture-phase audio click regex,
  so the article's own inline `onclick` fallback still fires.
- wudict reads a body as compressed only when it starts with `0x00`; plain
  UTF-8 HTML is therefore format-legal and satisfies "no compression".
- `entry://` lookup links are split by string position (wudict takes `bword:`
  and `entry:`, with or without `//`, identically) — `elh_parse` un-escapes
  them after serialization.

## User decisions already taken (do not re-ask)

1. Headword **and** example-sentence TTS audio, offline, with a JS fallback to
   the online URL. (This reverses the original "headwords only" decision.)
2. Three separate dictionaries (`eu-es-Elhuyar`, `es-eu-Elhuyar`,
   `en-eu-Elhuyar`), each its own folder with `text.db` + `media.db` +
   `info.txt`; folder names follow `<SOURCE>-<TARGET>-<dictname>`.
3. 16 concurrent requests, no inter-request delay.
4. Audio ships as 24 kbps 16 kHz mono opus (`make opus`); mp3 is kept only for
   clips ffmpeg could not convert.
