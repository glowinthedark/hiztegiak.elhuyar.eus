"""Verify built dictionaries against the wudict format. `make check`."""
from __future__ import annotations

import glob
import os
import sqlite3
import sys

from elh_common import OUT


def check(pattern: str = os.path.join(OUT, "Elhuyar-*")) -> int:
    dirs = sorted(d for d in glob.glob(pattern)
                  if os.path.exists(os.path.join(d, "text.db")))
    if not dirs:
        print("no dictionaries in " + OUT + " - run `make build`", file=sys.stderr)
        return 1
    bad = 0
    for d in dirs:
        t = sqlite3.connect(os.path.join(d, "text.db"))
        m = sqlite3.connect(os.path.join(d, "media.db"))
        one = lambda db, q: db.execute(q).fetchone()[0]  # noqa: E731
        meta = dict(t.execute("SELECT key, value FROM meta"))
        mmeta = dict(m.execute("SELECT key, value FROM meta"))
        n = one(t, "SELECT count(*) FROM entry")
        # contentless fts5 cannot be scanned; probe it with a real headword instead
        probe = t.execute("SELECT id, w FROM entry ORDER BY id LIMIT 1").fetchone()
        hit = t.execute(
            "SELECT count(*) FROM entry_fts WHERE entry_fts MATCH ? AND rowid = ?",
            ('"' + probe[1].replace('"', '""') + '"', probe[0])).fetchone()[0] if probe else 0
        res = one(m, "SELECT count(*) FROM resource")
        nul = one(t, "SELECT count(*) FROM entry WHERE substr(m, 1, 1) = char(0)")
        empty = one(t, "SELECT count(*) FROM entry WHERE m = '' OR w = ''")
        css = one(m, "SELECT count(*) FROM resource WHERE name = 'elhuyar.css'")
        mp3 = one(m, "SELECT count(*) FROM resource WHERE name LIKE 'audio/%'")
        for ok, msg in (
            (one(t, "PRAGMA user_version") == 1, "text.db user_version != 1"),
            (one(m, "PRAGMA user_version") == 1, "media.db user_version != 1"),
            (hit == 1, f"entry_fts does not index entry {probe!r}"),
            (nul == 0, f"{nul} bodies start with NUL - wudict would inflate them"),
            (empty == 0, f"{empty} empty headwords or bodies"),
            (meta.get("dict_uuid") == mmeta.get("dict_uuid"), "media.db dict_uuid mismatch"),
            (meta.get("entry_count") == str(n), "meta.entry_count != entry rows"),
            (meta.get("body_encoding") == "plain", "meta.body_encoding != plain"),
            (meta.get("format") == "html", "meta.format != html"),
            (css == 1, "elhuyar.css missing from media.db"),
        ):
            if not ok:
                print(f"  FAIL {os.path.basename(d)}: {msg}")
                bad += 1
        size = sum(os.path.getsize(os.path.join(d, f))
                   for f in ("text.db", "media.db"))
        print(f"{os.path.basename(d):<16} {n:>7} entries  {mp3:>7} clips  "
              f"{res:>7} resources  {size/1e6:8.1f} MB  {meta.get('body_encoding')}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(check(*sys.argv[1:]))
