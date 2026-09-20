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

**The documented GET endpoint is dead.** `/static/js/base.js` (obfuscated;
deobfuscated) has the page call

```
GET https://tts-api.elhuyar.eus/api/from_hiztegia/
    ?word=<word>&origin_language=<eu|es|en|fr>&dest_language=<eu|es>
```

which now answers **HTTP 500 for every word**, including the ones the live site
asks for — the green speaker icons are broken on hiztegiak.elhuyar.eus itself.
A full run against it produced 3 clips out of 9,700. Do not reinstate it.

The working synthesizer is the public one behind **ttsneuronala.elhuyar.eus**:

```
GET  /en                          -> csrftoken cookie + csrfmiddlewaretoken
POST /ajax/get_audio_from_box      csrfmiddlewaretoken, language, voice, text
     -> {"audio_path": "https://tts-api.elhuyar.eus/audioa_entzun?path=<md5>.mp3"}
GET  <audio_path>                 -> audio/mpeg
```

- Django CSRF: the form token is a **masked** form of the cookie, so cookie and
  token must come from the same `GET /en`, and a same-origin `Referer` is
  required. One token lasts a whole run; a 403 means refresh it once.
- `voice` is mandatory. From `/ajax_get_voice_language_list`, first (male)
  voice per language: **eu 29 · es 37 · en 39 · fr 43**.
- It takes arbitrary text, so **headwords and example sentences go through the
  same call** — there is no separate example endpoint to maintain.
- `<md5>` is a hash of (text, voice) server-side: stable across sessions, but
  not derivable offline. It is persisted in `audio.url` because it is the only
  thing an offline article can fall back to.
- Responses validated by MPEG magic (`ID3` or `\xff[\xfb\xf3\xf2\xe3]`);
  a synthesizer failure answers 200 with HTML and is recorded as 415.

**Example-sentence audio is now in scope** (reversing the original decision).
The site's own `POST /api/hiztegiko_adibideak` works, but is not used: it
concatenates origin+dest into a single ~7 s clip and returns no reusable URL,
whereas ttsneuronala returns origin-only plus the permanent URL.

#### Why the online fallback is a URL and not an API call
Neither POST can be invoked from a browser at the wudict origin:

| endpoint | cross-origin verdict |
|---|---|
| `hiztegiak…/api/hiztegiko_adibideak` | no `access-control-allow-origin` on preflight **or** POST; the no-preflight `text/plain` variant 415s |
| `ttsneuronala…/ajax/get_audio_from_box` | `ACAO: *`, but Django rejects a foreign `Origin` with **403 CSRF** |
| `tts-api…/audioa_entzun?path=<md5>.mp3` | plain GET, `ACAO: *`, **works** |

So the fallback baked into an article is the resolved `audioa_entzun` URL, not
a synthesis request — which is why `audio.url` must be populated for every clip
(rows lacking it are re-queued even when their bytes are fine).

### Opus transcode
`ffmpeg -f mp3 -i pipe:0 -c:a libopus -b:a 24k -vbr on -application voip -ac 1
-ar 16000 -f ogg pipe:1`. Sources are 22.05 kHz mp3 of synthesized speech, so
nothing audible sits above 8 kHz; 24 kbps is ~2.4x smaller than the mp3 (word
5,907 -> 2,440 B; sentence 21,939 -> 8,977 B) with no audible loss. 16 kbps
saves another 25 % and sounds thin. Output is validated by the `OggS` magic and
cached in `audio.opus`, so rebuilds never re-run ffmpeg.

**`.opus` is a load-bearing extension.** wudict rewrites any `assetExt` href
(which includes `.opus`) to `/res/<dictID>/…`, but its capture-phase audio
click handler only claims `(mp3|ogg|wav|spx|m4a)` and calls
`stopImmediatePropagation()`. `.opus` is in the first list and not the second,
so the click reaches the article's own `onclick` and the online fallback runs.
A clip that failed to transcode keeps `.mp3` and is played by wudict's handler
with no fallback.

### Throughput
Pages: measured ceiling ≈5.7 req/s sustained at concurrency 16 (flat beyond 8);
short bursts reach ~23 req/s. TTS: ≈4.7 req/s, flat beyond concurrency 16 (2.45
at c=8, 4.70 at c=16, 4.73 at c=32); 160/160 requests succeeded in the probe.
151,387 headwords + 111,030 distinct example sentences = **262,417 clips ≈
15.5 h**. No rate-limiting or ban observed at 16. `_get()` retries
5xx / 429 / transport errors with 2 s, 6 s, 15 s backoff, then records status 0
(which `--retry-failed` re-queues).

## 2. Crawl cache — `work/cache.db`

WAL, all tables `WITHOUT ROWID`:

| table | columns | meaning |
|---|---|---|
| `word` | `lang, word` | wanted set, from the sitemaps |
| `page` | `lang, word, status, found, gz, ts` | fetched page, gzipped **only** when `found = 1` |
| `ttsword` | `lang, word, kind` | every spoken form a page requests; `kind` is `w` (headword) or `x` (example). Keyed by text alone — the synthesizer takes text and an example equal to a headword is legitimately the same clip |
| `audio` | `lang, word, status, mime, data, ts, url, opus` | mp3 bytes, the permanent `audioa_entzun` URL for the online fallback, and the transcoded opus |

`url` and `opus` were added after the first crawl, via the guarded
`elh_common.MIGRATIONS` (`ALTER TABLE … ADD COLUMN` is the only way into a
`WITHOUT ROWID` table without rewriting 3 GB of blobs).

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

Audio resource names are hashed — `audio/<lang>/<sha1(lang\0word)[:20]>.opus`
(`.mp3` when transcoding failed) — so no headword or sentence ever round-trips
through URL or filename encoding.

An audio anchor carries its own fallback, as an **inline `onclick`, never a
`<script>`**: wudict renders any article containing `<script>` in a `srcdoc`
iframe instead of the shadow DOM, which would cost the host stylesheet and the
lookup links. Inline `on*` runs fine inside a shadow root.

```html
<a href="audio/eu/<sha1>.opus" data-u="https://tts-api.elhuyar.eus/audioa_entzun?path=<md5>.mp3"
   class="wu-audio elh-tts" onclick="var t=this,a=new Audio(t.href);window._a=a;
   a.onerror=function(){var u=t.getAttribute('data-u');if(u){(window._a=new Audio(u)).play()}};
   a.play();return false">🔊</a>
```

`onerror`, not a rejected `play()` promise: a rejection also means "autoplay
blocked", which must not silently hit the network. When a clip is absent at
build time the `href` points straight at the remote URL and no `onclick` is
emitted; when neither exists the icon is dropped. ~215 B per anchor.

## 4. Parser contract (`elh_parse.py`)

`parse(page_html, lang, media=None) -> (article_html, plain_text, {(lang, text), …}) | None`
(`None` = no article on the page). `media` maps `(lang, spoken text)` to
`(resource name | None, online URL | None)` and decides per icon whether it
links a local clip with a fallback, links straight out to the network, or is
dropped; `None` means "assume everything is local".

`create_audio_adibideak('eu','es','<origin>','<dest>')` is matched with **every
group lazy**, anchored on the trailing `);return false`, `re.S`. This is not
cosmetic: the site emits the *dest* argument with apostrophes **unescaped**
(`'he\'s going out with someone'` is written `'he's going out with someone'` —
those buttons are broken on the live site too), so a quote-counting pattern
mis-splits ~4,000 calls and a greedy tail collapses every later call on the page
into one. Only the origin text is used; it was verified byte-identical to the
rendered `<em>` across 4,000 `eu` pages.

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
