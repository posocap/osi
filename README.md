# osi — OSI / TOS citation harvester

Builds a citation database *and a plain-text corpus* of everything published by
the **Objective Standard Institute** (`objectivestandard.org`) and ***The
Objective Standard*** (`theobjectivestandard.com`), 2006 to present.

The key finding from reconnaissance: the Substack migration brought the whole
2006–present back catalogue with it, and the paywall withholds only article
*bodies* — every citation field (title, byline, date, canonical URL, word count,
section, tags) comes back in public JSON. See [docs/SOURCES.md](docs/SOURCES.md)
for the endpoint inventory, verified counts, and the blockers.

**Coverage, in one line each.** *Citations:* complete, 2006 to present, no
authentication needed. *Full text:* complete only from ~2024 on — earlier
articles are `only_paid` and yield previews unless you supply a subscriber
session, and a free account does not unlock them. See
[Paywalled bodies](#paywalled-bodies).

## Run it

```bash
pip install -r requirements.txt
python osi_harvest.py
```

That's the whole thing. It tells you what it's about to do, asks once, and then
runs every pass and writes every output. Answer no and it asks about each pass
instead, with a sensible default on every question — holding Enter is fine.

`python osi_harvest.py --all` does the same with no questions, for cron or CI.

Runs anywhere with real outbound internet: your own machine, or GitHub Actions
(see [docs/RUNNING.md](docs/RUNNING.md) — local is recommended for the first
run). It cannot run in Claude's cloud sandbox, whose egress allowlist blocks
both target hosts.

Expect ~2,700 articles, 60–90 minutes at the default 1s delay, and 100–200 MB on
disk including the text corpus. Re-running is safe and cheap — metadata upserts
never overwrite a populated field with `NULL`, and the full-text pass skips
articles whose text is already on disk, so an interrupted run resumes.

<details>
<summary>Partial and repeat runs</summary>

| Command | Does |
|---|---|
| `--only substack` | TOS only |
| `--only wp` | objectivestandard.org only |
| `--fulltext-only` | article bodies alone, resumable |
| `--fulltext-only --text-limit 20` | trial run on 20 articles |
| `--export-only` | no network, re-export from the database |
| `--export-only --d1-include-text` | re-export with bodies embedded in the D1 SQL |
| `--legacy-insecure` | also crawl `archive.theobjectivestandard.com` with TLS verification **off** (its cert doesn't match its hostname) |
| `--delay 2.0` | slow down (Substack 429s on bursts) |
| `--refetch-text` | re-fetch bodies already stored |
| `--cookie-file cookies.txt` | supply a subscriber session for paywalled bodies |
| `--retry-previews` | re-fetch only the articles stored as previews |

</details>

## Outputs

| File | What |
|---|---|
| `texts/*.txt` | **one file per article** — metadata header, then the body |
| `text_manifest.csv` | index of the corpus: path, status, word count, checksum |
| `osi_zotero.csl.json` | CSL-JSON — Zotero: **File → Import** |
| `osi_citations.bib` | BibTeX |
| `osi_citations.csv` | flat export |
| `osi_citations.sqlite` | normalized `items` + the raw JSON per item |
| `d1_schema.sql` / `d1_data.sql` | Cloudflare D1 |

## Paywalled bodies

Metadata needs no login. Article **bodies** for posts marked `only_paid` — most
of the pre-2024 back catalogue — are withheld from anonymous clients, and a free
account does not change that. Those articles are recorded as
`text_status='paywalled-preview'` and their `.txt` holds the preview, clearly
labelled, rather than being passed off as the whole article.

If you have a paid subscription, export `cookies.txt` from a logged-in browser
session and pass it. No password is ever typed into the script:

```bash
python osi_harvest.py --fulltext-only --cookie-file cookies.txt
```

Supplying cookies implies `--retry-previews`, so exactly the articles that came
back short last time are re-fetched and nothing else. `--cookie "name=value; …"`
works too, as do `OSI_COOKIE_FILE` and `OSI_COOKIE`. Cookies are pinned to the
Substack domain, so a subscriber session is never sent to
`objectivestandard.org`.

Without a subscription the corpus is still useful — every title, byline, date,
issue, section and tag is there, plus full bodies from ~2024 on — but a text
mining run over the whole catalogue should filter on `text_status='ok'` and
treat the rest as bibliographic records only.

## The text corpus

Filenames are built to be self-explaining and to sort usefully:

```
texts/2006-05-20_Armstrong-Ari_teaching-values-in-the-classroom_substack-99.txt
      └─ date ──┘ └─ author ──┘ └───── title ─────────────────┘ └─ source+id ┘
```

Date first, so a directory listing is chronological; author next, so one
writer's output groups together; source and id last, which make the name unique
and traceable back to `items.key`. Undated items get `undated_`, unattributed
ones `unattributed_`, three-or-more authors collapse to `First-A+et-al`, and
titles are transliterated to ASCII and truncated at a word boundary.

Each file carries a header block between two `====` rules, so a miner can parse
it or split it off:

```
==============================================================================
Title:                Teaching Values in the Classroom
Author(s):            Ari Armstrong
Published:            2006-05-20T00:00:00Z
Publication:          The Objective Standard
Issue:                Winter 2006-07
Section:              Education
Tags:                 education; values
Access:               only_paid
URL:                  https://www.theobjectivestandard.com/p/teaching-values
Word count (source):  3200
Item key:             substack:teaching-values
Text status:          paywalled-preview
Text strategy:        html-preloads
Text words:           612
Text sha256:          9f2c…
==============================================================================

Article text begins here…
```

**`Text status`** is the field to filter on before mining. Because the archive
API reports a `wordcount` for every post, a body that comes back at under 40% of
the expected length is labelled `paywalled-preview` rather than being stored as
if it were the whole article. `ok` means the length matches; `unavailable` means
nothing usable came back and no file was written. **`Text strategy`** records
which of the body sources actually worked, per article — a clean WordPress REST
body and a scraped page are not the same evidence.

## Zotero

Import `osi_zotero.csl.json` via **File → Import**. Authors are split into
given/family, dates become `date-parts`, tags become keywords, and the subtitle
becomes the abstract.

CSL-JSON has no field for attachments, so the extracted text can't ride along
inside the import. Instead each item's **Extra** field carries the path,
status, word count and checksum of its `.txt`:

```
access: only_paid
fulltext file: texts/2006-05-20_Armstrong-Ari_teaching-values…txt
fulltext status: paywalled-preview
fulltext sha256: 9f2c…
item key: substack:teaching-values
```

So the corpus stays on disk for mining and every Zotero item still points at its
own text file.

## Cloudflare D1

```bash
wrangler d1 create osi
wrangler d1 execute osi --remote --file=d1_schema.sql
wrangler d1 execute osi --remote --file=d1_data.sql
```

Both files are idempotent (`CREATE TABLE IF NOT EXISTS`, `INSERT OR REPLACE`),
so re-running refreshes. Inserts are batched 50 rows per statement to stay well
inside D1's per-query limits.

Four tables, because `'; '`-joined author and tag strings are miserable to query
from a Worker:

| Table | Columns |
|---|---|
| `items` | one row per article, including the `text_*` columns |
| `authors` | `(item_key, position, name)` |
| `tags` | `(item_key, tag)` |
| `texts` | `(item_key, words, sha256, body)` — **only** with `--d1-include-text` |

Bodies are left out by default: the corpus is tens of megabytes, the `.txt`
files already hold it, and D1 is the wrong place for it unless you intend to
query text from a Worker. When they are included, the metadata header is
stripped so only article text lands in the column.

## Sources

| Pass | Source | Coverage |
|---|---|---|
| A | `theobjectivestandard.com/sitemap/<YEAR>` | URL+title enumeration, 2006–2026 |
| B | `/api/v1/archive?sort=new&offset=N&limit=50` | metadata spine, ~2,400 posts |
| C | `objectivestandard.org/wp-json/wp/v2/*` | 315 blog posts (2021→) + podcasts, conferences, courses, fellows |
| D | `archive.theobjectivestandard.com` | optional; pre-Substack site, TLS hostname mismatch |
| E | issue-announcement posts | stamps `issue_label` (e.g. `Fall 2026`) onto each issue's articles |
| F | bodies | WordPress REST content; for Substack, the archive payload, then two candidate post-detail endpoints, then the article page's `window._preloads` blob, then its article container |

Pass F's Substack endpoint shapes are **probed, not assumed** — whichever
answers is recorded per article in `text_strategy`, and the HTML fallback covers
all of them failing.

## Layout

| File | |
|---|---|
| `osi_harvest.py` | entry point: config, HTTP, storage, passes A–E, CLI |
| `osi_text.py` | HTML→text, filenames, the `.txt` files, pass F |
| `osi_export.py` | CSV, Zotero CSL-JSON, BibTeX, D1 SQL, summary |
| `test_offline.py` | 106 offline checks |

Only dependency is `requests`. HTML is reduced to text by a small `HTMLParser`
subclass rather than bs4/lxml, so there's nothing else to install.

## Tests

```bash
python test_offline.py
```

106 checks over parsing, upsert precedence, issue linking, HTML→text, filename
construction, the paywall heuristic, pass F resumability, database migration, and
all four exports — including the real edge cases found in the data (empty
excerpt, corrupt `0002-08-12` date, subtitle-only byline, single-name author,
BibTeX key collision, apostrophes in titles), plus cookie scoping. The D1 export is verified by
executing the generated SQL against a fresh SQLite database and checking the
rows come back: D1 *is* SQLite, so SQL that SQLite accepts and that round-trips
correctly is SQL D1 will accept. No network.

## Politeness

One request at a time, 1s default delay, exponential backoff honouring
`Retry-After`. Set a real contact address in the `UA` constant in
`osi_harvest.py` before a long run. 