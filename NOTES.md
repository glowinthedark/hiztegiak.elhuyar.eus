# NOTES — site mechanics + wudict format

Reference material so a future session does not re-derive any of this.
Companion to `CLAUDE.md`. Verified 2026-09.

## 1. hiztegiak.elhuyar.eus

### Enumeration
`GET /sitemap.xml` is an index pointing at `/sitemap.1.xml` … `/sitemap.4.xml`.
Each `<loc>` is `https://hiztegiak.elhuyar.eus/<lang>/<url-encoded word>`.
Distinct headwords: **eu 68,507 · es 66,265 · en 37,192 · fr 24,554**.
No autocomplete prefix-crawling is needed.

### Entry page
`GET /<lang>/<quote(word, safe='')>`.
One page carries **all** translation blocks for that source language, as
`div|ul.hizkuntzaren_arabera.hizkuntza-<pair>` (`eu_es`, `eu_fr`, `eu_en`, …).

A **miss returns HTTP 200** with a "not found" page — detect a hit by the
presence of the literal `hizkuntzaren_arabera`. Do not trust status codes.

Structure reference: `txori.html` (dump of `/eu/txori`, 59,884 B). Every
selector in `elh_parse.py` is justified by that file; re-check against it
before touching the parser.

### Pronunciation audio
The page calls `create_audio('eu_es', 'txori')` in an inline `onclick`.
`/static/js/base.js` is obfuscated; deobfuscating it yields:

```
GET https://tts-api.elhuyar.eus/api/from_hiztegia/
    ?word=<word>&origin_language=<eu|es|en|fr>&dest_language=<eu|es>
```

Plain HTTP GET, no cookies, no CSRF, no browser. Returns `audio/mpeg`.
Only `origin_language` shapes the waveform (the synthesizer speaks the
headword); `dest_language` is required by the API but inert — see `TTS_DEST`.
Responses are validated by MPEG magic (`ID3` or `\xff[\xfb\xf3\xf2\xe3]`);
anything else is recorded as status 415 rather than stored.

**Example-sentence audio** (`create_audio_adibideak`) is only reachable via
`POST /api/hiztegiko_adibideak` with a CSRF token. Out of scope by user
decision; those anchors are dropped by the parser.

### Throughput
Measured ceiling ≈5.7 req/s sustained at concurrency 16 (flat beyond 8); short
bursts reach ~23 req/s. No rate-limiting or ban observed at 16. `_get()` retries
5xx / 429 / transport errors with 2 s, 6 s, 15 s backoff, then records status 0
(which `--retry-failed` re-queues).

## 2. Crawl cache — `work/cache.db`

WAL, all tables `WITHOUT ROWID`:

| table | columns | meaning |
|---|---|---|
| `word` | `lang, word` | wanted set, from the sitemaps |
| `page` | `lang, word, status, found, gz, ts` | fetched page, gzipped **only** when `found = 1` |
| `ttsword` | `lang, word` | headwords whose page requests TTS |
| `audio` | `lang, word, status, mime, data, ts` | the mp3 bytes |

gzip here is cache-side only, to keep the file at a few GB. **The wudict output
stores bodies plain.**

## 3. wudict prepared-dictionary format

Source of truth: `/Users/bio/projects/golang/dict-go-web/gonow-dict`, files
`internal/store/{store,ingest,compress,media}.go`, `internal/dict/fold.go`,
`internal/artmark/artmark.go`, `internal/server/rewrite.go`,
`internal/server/web/index.html`. Live example: `~/.wudict/db/AHD5/`.

A dictionary is a **folder**: `text.db` + `media.db` + `info.txt`.
Both DBs carry `PRAGMA user_version = 1` and are paired by `meta.dict_uuid`.

```sql
-- text.db
meta(key TEXT PRIMARY KEY, value TEXT)
entry(id INTEGER PRIMARY KEY, w TEXT NOT NULL, m TEXT NOT NULL)   -- m = article
alias(w TEXT NOT NULL, entry_id INTEGER REFERENCES entry(id))
CREATE VIRTUAL TABLE entry_fts USING fts5(w, txt, content='', columnsize=0,
  tokenize='unicode61 remove_diacritics 2');
CREATE INDEX idx_entry_w ON entry(w COLLATE NOCASE);
CREATE INDEX idx_alias_w ON alias(w COLLATE NOCASE);
-- media.db
meta(key TEXT PRIMARY KEY, value TEXT)          -- dict_uuid, name, format
resource(name TEXT PRIMARY KEY, mime TEXT, data BLOB)
```

### Compression — the point of this project
`store/compress.go`: a body is DEFLATE-compressed **iff its first byte is
`0x00`** (a sentinel, not a header). A body that does not start with `0x00` is
returned verbatim. Plain UTF-8 HTML therefore needs no flag and no shim: it is
format-legal and every wudict reader handles it. `meta.body_encoding = "plain"`
is recorded for humans only.

### meta keys written
`dict_uuid, name, format=html, source_path, description, entry_count,
sub_entries, ingest_level=text, has_trigram=0, fold_version=1,
markup_version=2, body_encoding=plain, created, index_lang, contents_lang`.
`fold_version` = `dict.FoldVersion` (1), `markup_version` = `artmark.Version`
(2), `schemaVersion` = 1 = `user_version`. `entry_fts(rowid, w, txt)` is filled
with `StripHTML(body)` — required at `ingest_level = "text"`.

### Link semantics (`server/rewrite.go`, `web/index.html`)
- A **relative** `href` naming an asset (`.mp3`, `.css`, …) is a resource:
  rewritten to `/res/<dictID>/<name>` and looked up in `media.db.resource.name`.
  Hence articles reference `audio/<lang>/<sha1[:20]>.mp3` and `elhuyar.css`, and
  those exact strings are the resource keys.
- `bword://<headword>` is a lookup link, **parsed by string position and never
  percent-decoded**. lxml escapes spaces/non-ASCII when serializing `href`, so
  `elh_parse.parse()` un-escapes `bword://` targets afterwards. Regression
  check: `bword://pajaro tejedor` must appear literally, not `%20`.
- Role classes are `wu-`-prefixed: `wu-k` (headword), `wu-audio`, `wu-xref`,
  `wu-ex`.

Audio resource names are hashed — `audio/<lang>/<sha1(lang\0word)[:20]>.mp3` —
so no headword ever round-trips through URL or filename encoding.

## 4. Parser contract (`elh_parse.py`)

`parse(page_html, lang) -> (article_html, plain_text, {(lang, word), …}) | None`
(`None` = no article on the page).

Dropped: `script style noscript iframe ins input img form`, `.partekatuikonoak`,
`.float-right`, `.loader`, `/sarrera_iruzkinak`, `/proposamenak`,
`/hitz-gakoak`, comments. `<button>` is **unwrapped, not dropped** — it carries
the sub-entry section labels ("Lexical items"). Attributes: an explicit drop
list plus every `data-*`, `aria-*` and `on*`; Bootstrap/FontAwesome class noise
(`col- order- mt- badge collapse float- btn- fa fab fas`) is filtered out.

Output shape:
```html
<link rel="stylesheet" href="elhuyar.css">
<div class="elh">
  <div class="elh-pair" data-pair="eu_es">
    <div class="elh-pair-label">eu &rsaquo; es</div> …
```
