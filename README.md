# Cuban music downloads

This repo turns the Díaz-Ayala catalog in `data/raw/` into one MP3 per song title. Each file is a YouTube recording chosen to stay close to the original, saved under `songs/<author>/`. `songs/INDEX.md` is rewritten after every successful download.

There are two ways to run that work:

- **With an agent.** Point it at [`skills.md`](skills.md). That file is the task, in plain language.
- **On its own.** Run [`download_songs.py`](download_songs.py). The script applies the same rules every time: one title, one file, resume from disk, convert to MP3, update the index while later titles are still being searched.

## What the catalog contains

| File | Role |
| --- | --- |
| `data/raw/catalogo_songs.json` | Recording rows (`title`, `artist`, `credits_raw`, `recording_date`, `catalog_number`, `source_pdf`). Titles often look like `La leyenda del moro / mon`, where the slash marks a genre code. |
| `data/raw/compositor_abbreviations_index.json` | Composer abbreviations expanded into full names and used in the YouTube query. |

Duplicate titles are collapsed before any download. When several catalog rows share a title, the script keeps the row where the performer is the composer, then the earliest known recording date.

## Use it with `skills.md`

`skills.md` is the instruction an agent should follow. It asks for one YouTube match per title, MP3 conversion, a markdown index with links to the files, resume after a manual stop, and downloads that start before the full list is finished. Songs go in `songs/<author>/`.

Open this repo in Cursor and tell the agent to follow `skills.md`. The agent is expected to use `download_songs.py` (or the same inputs and outputs) so the result matches a direct run of the script.

The script is the source of the matching rules. An agent reading `skills.md` should not invent a second downloader or a second index format.

## Use `download_songs.py` alone

The script does not call an agent. Given the two JSON files, it deduplicates titles, searches YouTube, scores results, downloads the winner, converts it to MP3, and updates `songs/INDEX.md` and `data/download_state.json`.

YouTube's search results can change between days. The selection rules do not: the same candidates are scored the same way.

### Setup

Python 3.11+ and `ffmpeg` (used by yt-dlp to write the MP3). From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
python download_songs.py
```

Useful limits while you check that matches look right:

```bash
python download_songs.py --limit 20 --workers 3 --search-count 8
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--workers` | `3` | Parallel YouTube searches and downloads. Must be at least 1. |
| `--limit` | none | Stop after this many *new* titles. Already settled titles do not count. |
| `--search-count` | `8` | How many YouTube results to score for each query. Must be at least 1. |

Search and download overlap. The index grows as files land; the script does not wait for every title to be matched first.

### Resume

Stop with Ctrl+C (or SIGTERM). The current searches finish, then the process exits. Completed MP3s and `data/download_state.json` are already on disk. Run the same command again to continue.

| Status in `data/download_state.json` | Next run |
| --- | --- |
| `downloaded` and the MP3 is still there | Skipped, before any YouTube search |
| `downloaded` but the MP3 is missing | Tried again |
| MP3 already in `songs/` for that title | Skipped, before any YouTube search. The state file is filled in when it has no entry. |
| `not_found` | Skipped. Delete that entry from the state file if you want another search. |
| `error` | Tried again |

### Where files go

```text
songs/
  INDEX.md
  Joseito Fernández/
    Guantanamera.mp3
data/
  download_state.json    # resume ledger; not committed
  raw/
    catalogo_songs.json
    compositor_abbreviations_index.json
```

`songs/INDEX.md` groups tracks by author, in alphabetical order of the author name (accents and case ignored). Each line links to the local MP3 and, when known, to the YouTube video, plus performer, genre, recording date, and composers.

Author folders prefer a resolved composer name. If the catalog has no usable credit, the folder is the cleaned performer name. Region tags such as `(cu)` are stripped, and all-caps names are title-cased.

### How a YouTube video is chosen

For each unique title the script searches `title + first composer`, or `title + performer` when there is no composer. If that query has no acceptable hit, it searches the title alone.

A result is kept only when the title is close (short titles must match almost exactly unless the performer or composer is named), the duration looks like a song, and karaoke, covers, remixes, tutorials, live sets, and full-album uploads are penalized. Original or official wording, a matching performer or composer, a typical song length, and view count raise the score. The highest-scoring acceptable result is downloaded and converted to a 192 kbps MP3.

One file is kept per title. If that MP3 is already on disk and non-empty, the title is skipped before any YouTube search.

## Prune `songs/INDEX.md`

[`download_songs.py`](download_songs.py) rewrites the index from the download ledger after each successful save. If an MP3 is removed from `songs/` by hand, its line stays until the next rewrite. [`prune_index.py`](prune_index.py) drops those stale lines immediately:

```bash
python prune_index.py
```

Each song line begins with a markdown link relative to `songs/`. The script URL-decodes that path and deletes the line when the file is missing. An author heading is removed when no songs remain under it, and the count and timestamp in the header are updated. When every linked file is still on disk, `songs/INDEX.md` is left unchanged.
