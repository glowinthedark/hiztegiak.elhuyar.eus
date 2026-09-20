# Elhuyar Hiztegiak -> wudict.  See CLAUDE.md / NOTES.md.
# Every target is resumable: the crawl cache is the source of truth, so a
# re-run only does the work that is missing.  Ctrl-C and `make` again is safe.

PY      ?= python3
LANGS   ?= eu,es,en          # fr is opt-in: make pages LANGS=fr
CONC    ?= 16
LIMIT   ?= 0                 # build only N entries per language (smoke test)
JOBS    ?=                   # ffmpeg workers for `opus` (default: CPU count)
KBPS    ?= 24                # opus bitrate; 16 is smaller and audibly thinner
HZ      ?= 16000             # opus sample rate; speech has nothing above 8 kHz
ELH      = $(PY) elh.py
WORK     = work
OUT      = out
DICTS    = $(OUT)/eu-es-Elhuyar $(OUT)/es-eu-Elhuyar $(OUT)/en-eu-Elhuyar
RETRY    =                   # make pages RETRY=--retry-failed

.DEFAULT_GOAL := help
.PHONY: help deps sitemap pages audio opus build all status crawl log stop smoke \
        check install clean clean-out clean-all

help:                       ## this list
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
	 | sed 's/:.*##/\t/' | awk -F'\t' '{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'
	@echo "  vars: LANGS=$(LANGS) CONC=$(CONC) LIMIT=$(LIMIT) KBPS=$(KBPS) HZ=$(HZ)"

deps:                       ## install httpx + lxml into the active env
	uv pip install httpx lxml

sitemap:                    ## enumerate headwords into work/cache.db (~30 s)
	$(ELH) sitemap --langs $(LANGS)

pages:                      ## fetch entry pages (~2-8 h, resumable)
	$(ELH) pages --langs $(LANGS) -c $(CONC) $(RETRY)

audio:                      ## plan + fetch TTS mp3, headwords + examples (resumable)
	$(ELH) audio --langs $(LANGS) -c $(CONC) $(RETRY)

# Cached back into work/cache.db, not produced during build: 262k ffmpeg runs
# happen once, however often the dictionaries are rebuilt.  `make opus FORCE=1`
# re-encodes after changing KBPS/HZ.
opus:                       ## transcode the fetched mp3 to opus (16 kHz mono VoIP)
	@command -v ffmpeg >/dev/null || { echo "ffmpeg not in PATH" >&2; exit 1; }
	ELH_OPUS_KBPS=$(KBPS) ELH_OPUS_HZ=$(HZ) $(ELH) transcode --langs $(LANGS) \
	  $(if $(JOBS),-j $(JOBS),) $(if $(FORCE),--force,)

build: | $(OUT)             ## cache -> out/<src>-<tgt>-Elhuyar/{text,media}.db + info.txt
	$(ELH) build --langs $(LANGS) --limit $(LIMIT)

all:                        ## sitemap + pages + audio + opus + build, in order
	ELH_OPUS_KBPS=$(KBPS) ELH_OPUS_HZ=$(HZ) $(ELH) all --langs $(LANGS) -c $(CONC) \
	  $(RETRY) $(if $(JOBS),-j $(JOBS),) --limit $(LIMIT)

status:                     ## per-language counts for every stage
	@$(ELH) status --langs $(LANGS)

crawl:                      ## run pages+audio detached, logging to work/crawl.log
	@mkdir -p $(WORK)
	@nohup sh -c '$(ELH) pages --langs $(LANGS) -c $(CONC) && \
	              $(ELH) audio --langs $(LANGS) -c $(CONC) && \
	              ELH_OPUS_KBPS=$(KBPS) ELH_OPUS_HZ=$(HZ) $(ELH) transcode --langs $(LANGS)' \
	        > $(WORK)/crawl.log 2>&1 & echo "detached: pid $$!  -> make log"

log:                        ## tail the detached crawl log
	@tail -f $(WORK)/crawl.log

stop:                       ## stop a detached crawl (the cache keeps every page)
	-@pkill -f 'elh.py (pages|audio|transcode|all)' && echo stopped || echo "nothing running"

smoke: | $(OUT)             ## 200-entry build, for checking format changes fast
	$(ELH) build --langs $(LANGS) --limit 200

check:                      ## verify the built dictionaries against the wudict format
	@$(PY) elh_check.py

install: check              ## copy the built dictionaries into ~/.wudict/db/
	@mkdir -p $(HOME)/.wudict/db
	@for d in $(DICTS); do \
	  [ -f $$d/text.db ] && cp -R $$d $(HOME)/.wudict/db/ && echo "installed $$d"; \
	done

$(OUT):
	@mkdir -p $(OUT)

clean-out:                  ## delete the built dictionaries (cache untouched)
	rm -rf $(OUT)

clean:                      ## delete build products and logs, KEEP the crawl cache
	rm -rf $(OUT) $(WORK)/*.log

clean-all:                  ## also delete work/cache.db - forces a full ~8 h re-crawl
	@printf 'delete work/cache.db and re-crawl 172k pages? [y/N] ' && read a && [ "$$a" = y ]
	rm -rf $(OUT) $(WORK)
