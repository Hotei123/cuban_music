#!/usr/bin/env python3
"""Download one YouTube recording per catalog title and save it as MP3.

Reads data/raw/catalogo_songs.json and data/raw/compositor_abbreviations_index.json.
Songs are saved under songs/<artist>/. A markdown index is rewritten after every
successful download. Progress is stored in data/download_state.json, and MP3s already
under songs/ are skipped before any YouTube search. Stopping the
process and running it again continues where it left off.

The catalog is deduplicated locally (fast), then several downloads run at once.
The index grows while later titles are still being searched.

A recording by the author or the catalog performer is preferred. When none of
the results name that person, a recording by another interpreter is kept,
including one titled as a cover. Karaoke, remixes, tutorials, and live or
album uploads are still skipped.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import signal
import sys
import threading
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from queue import Full, Queue
from urllib.parse import quote

from yt_dlp import YoutubeDL

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "data" / "raw" / "catalogo_songs.json"
COMPOSERS_PATH = ROOT / "data" / "raw" / "compositor_abbreviations_index.json"
SONGS_DIR = ROOT / "songs"
INDEX_PATH = SONGS_DIR / "INDEX.md"
STATE_PATH = ROOT / "data" / "download_state.json"

# Díaz-Ayala genre marks that follow a slash in parsed titles.
GENRE_NAMES = {
    "afro": "afro",
    "b": "bolero",
    "bal": "balada",
    "bg": "beguine",
    "c": "cancion",
    "cc": "congo",
    "cg": "conga",
    "ch": "cha cha cha",
    "cl": "clave",
    "cr": "criolla",
    "d": "danza",
    "dia": "dialogo",
    "dz": "danzon",
    "dza": "danzon",
    "dzt": "danzonete",
    "ft": "fox trot",
    "gg": "guaguanco",
    "gj": "guajira",
    "gu": "guaracha",
    "h": "habanera",
    "mb": "mambo",
    "mer": "merengue",
    "mon": "monologo",
    "mt": "montuno",
    "pch": "pachanga",
    "pd": "pasodoble",
    "pl": "plena",
    "prg": "pregon",
    "r": "rumba",
    "s": "son",
    "tg": "tango",
    "v": "vals",
}

REJECT_WORDS = re.compile(
    r"\b("
    r"karaoke|remix|remake|rework|tutorial|lesson|react(?:ion)?|"
    r"sped\s*up|slowed|reverb|8d|nightcore|bass\s*boosted|"
    r"type\s*beat|playback|pista|"
    r"guitar\s+cover|piano\s+cover|ukulele|"
    r"full\s+album|album\s+completo|disco\s+completo|"
    r"megamix|dj\s*mix|mix\s+\d|"
    r"en\s+vivo|en\s+directo|\blive\b|live\s+at|concierto|concert"
    r")\b",
    re.IGNORECASE,
)
# A sung version by someone else is acceptable. Instrumental reductions stay
# in REJECT_WORDS above. The word only lowers the score, so the author's
# recording still wins when both are present.
COVER_WORD = re.compile(r"\bcovers?\b", re.IGNORECASE)
# Half of the significant name tokens is enough to treat the video as the
# catalog performer or the composer rather than another interpreter.
NAMED_PERFORMER = 0.5
ORIGINAL_WORDS = re.compile(
    r"\b(original|oficial|official|grabacion\s+original|version\s+original)\b",
    re.IGNORECASE,
)
REGION_SUFFIX = re.compile(
    r"\s*\((?:cu|me|pr|ar|es|eu|ch|sg|dr|rc|mx|us|br|vz|co|pe)\)\s*\.?$",
    re.IGNORECASE,
)

STOP = threading.Event()
STATE_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()


def log(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def author_sort_key(name: str) -> str:
    """Alphabetical order of the author name as written, accents and case ignored."""
    return strip_accents(name or "").casefold()


def normalize_text(text: str) -> str:
    text = strip_accents(text or "")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_key(title: str) -> str:
    return normalize_text(split_title(title)[0])


def split_title(raw: str) -> tuple[str, str, str]:
    """Return (song title, genre code, leftover credit text)."""
    raw = re.sub(r"\s+", " ", (raw or "")).strip()
    if "/" not in raw:
        return raw, "", ""
    left, right = raw.split("/", 1)
    right = right.strip()
    genre = right.split()[0] if right else ""
    extra = right[len(genre) :].strip(" .;-")
    return left.strip(), genre.lower(), extra


def genre_label(code: str) -> str:
    if not code:
        return ""
    parts = []
    for piece in re.split(r"[-+/]", code):
        piece = piece.strip().lower()
        if not piece:
            continue
        parts.append(GENRE_NAMES.get(piece, piece))
    # Preserve order while dropping repeats.
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return ", ".join(seen)


def clean_artist(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    text = re.sub(r"\s+", " ", str(raw)).strip()
    text = REGION_SUFFIX.sub("", text).strip(" .")
    if text.isupper():
        text = text.title()
    return text or "Unknown"


def safe_segment(text: str, limit: int = 90) -> str:
    text = text.replace("/", " - ").replace("\x00", "")
    text = re.sub(r'[\n\r\t<>:"\\|?*]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text.lstrip(".")
    if len(text) > limit:
        text = text[:limit].rstrip(" .")
    return text or "Unknown"


def recording_key(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    text = str(value).strip()
    matched = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if matched:
        month, day, year = (int(matched.group(i)) for i in range(1, 4))
        if year < 100:
            year += 1900
        return (year, month, day)
    matched = re.search(r"(18|19|20)\d{2}", text)
    if matched:
        return (int(matched.group(0)), 0, 0)
    return None


def load_composers(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for row in payload.get("abbreviations", []):
        code = (row.get("abbreviation") or "").strip()
        name = re.sub(r"\s+", " ", row.get("composer") or "").strip(" .")
        name = re.sub(r"\s*\((?:cu|me|pr|ar|es|eu)\)\s*$", "", name, flags=re.I)
        name = re.sub(r"\s+[A-Z]\.$", "", name).strip()
        if code and name:
            found[code] = name
    return found


def resolve_credit(credit: str, composers: dict[str, str]) -> str:
    credit = re.sub(r"\s+", " ", credit or "").strip(" .")
    if not credit:
        return ""
    if credit in composers:
        return composers[credit]
    folded = {key.casefold(): value for key, value in composers.items()}
    if credit.casefold() in folded:
        return folded[credit.casefold()]
    # Full names parsed into the credit field are usable as-is.
    # Bare unknown abbreviations are not.
    if (" " in credit or len(credit) > 4) and plausible_name(credit):
        return credit
    return ""


def composer_names(song: dict, composers: dict[str, str], extra: str) -> list[str]:
    names: list[str] = []
    for credit in song.get("credits_raw") or []:
        name = resolve_credit(str(credit), composers)
        if name and name not in names:
            names.append(name)
    if extra:
        name = resolve_credit(extra, composers)
        if name and name not in names:
            names.append(name)
    return names


def load_catalog(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    songs = payload.get("songs") or []
    if not isinstance(songs, list):
        raise SystemExit(f"No song list in {path}")
    return songs


def plausible_name(name: str) -> bool:
    letters = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", name or "")
    return len(letters) >= 3 and len(letters) >= len(re.sub(r"\s+", "", name)) / 2


def performer_is_author(song: dict, composers: dict[str, str]) -> bool:
    artist = normalize_text(clean_artist(song.get("artist")))
    _, _, extra = split_title(song.get("title") or "")
    for name in composer_names(song, composers, extra):
        tokens = [token for token in normalize_text(name).split() if len(token) > 3]
        if tokens and all(token in artist.split() for token in tokens[:2]):
            return True
    return False


def better_recording(new: dict, old: dict, composers: dict[str, str]) -> bool:
    """Prefer the author as performer, then the earliest known recording date."""
    new_author = performer_is_author(new, composers)
    old_author = performer_is_author(old, composers)
    if new_author != old_author:
        return new_author
    new_date = recording_key(new.get("recording_date"))
    old_date = recording_key(old.get("recording_date"))
    if new_date is None:
        return False
    if old_date is None or new_date < old_date:
        return True
    return False


def select_unique(songs: list[dict], composers: dict[str, str]) -> list[dict]:
    """One catalog row per title, chosen as soon as the file has been scanned."""
    chosen: dict[str, dict] = {}
    order: list[str] = []
    for song in songs:
        key = title_key(song.get("title") or "")
        if not key:
            continue
        if key not in chosen:
            chosen[key] = song
            order.append(key)
            continue
        if better_recording(song, chosen[key], composers):
            chosen[key] = song
    return [chosen[key] for key in order]


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"items": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log(f"State file {path} is unreadable; starting a new one.")
        return {"items": {}}
    data.setdefault("items", {})
    return data


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def title_similarity(wanted: str, video_title: str) -> float:
    left = normalize_text(wanted)
    right = normalize_text(video_title)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens = left.split()
    right_tokens = right.split()
    window = len(left_tokens)
    for start in range(0, len(right_tokens) - window + 1):
        if right_tokens[start : start + window] == left_tokens:
            extra = len(right_tokens) - window
            # Extra words are often the performer. A very long title is usually a mix.
            return 0.98 if extra <= 6 else 0.84
    if left_tokens and all(token in right_tokens for token in left_tokens):
        return 0.9
    return SequenceMatcher(None, left, right).ratio()


def artist_overlap(artist: str, video_text: str) -> float:
    tokens = [token for token in normalize_text(artist).split() if len(token) > 2][:4]
    if not tokens:
        return 0.0
    haystack = set(normalize_text(video_text).split())
    hits = sum(1 for token in tokens if token in haystack)
    return hits / len(tokens)


def score_entry(entry: dict, title: str, artist: str, composers: list[str]) -> tuple[float, float]:
    video_title = entry.get("title") or ""
    channel = entry.get("channel") or entry.get("uploader") or ""
    blob = f"{video_title} {channel}"
    similarity = title_similarity(title, video_title)
    overlap = artist_overlap(artist, blob)
    composer_overlap = 0.0
    if composers:
        composer_overlap = max(artist_overlap(name, blob) for name in composers)
    score = similarity * 60 + overlap * 25 + composer_overlap * 20
    plain = strip_accents(blob)
    if ORIGINAL_WORDS.search(plain):
        score += 8
    if COVER_WORD.search(plain):
        score -= 8
    if "various artists" in normalize_text(channel):
        score -= 12
    if REJECT_WORDS.search(plain):
        score -= 35
    duration = entry.get("duration") or 0
    if 75 <= duration <= 480:
        score += 8
    elif duration and (duration < 45 or duration > 900):
        score -= 30
    views = entry.get("view_count") or 0
    if views:
        score += min(6.0, math.log10(views))
    return score, similarity


def acceptable(entry: dict, similarity: float, artist: str, title: str, composers: list[str]) -> bool:
    if similarity < 0.72:
        return False
    duration = entry.get("duration") or 0
    if duration and (duration < 40 or duration > 15 * 60):
        return False
    blob = f"{entry.get('title') or ''} {entry.get('channel') or ''}"
    overlap = max(
        [artist_overlap(artist, blob)]
        + [artist_overlap(name, blob) for name in composers]
    )
    # One- and two-word titles must match the video title on their own.
    # A looser match is kept only when the performer or composer is named.
    if len(normalize_text(title).split()) <= 2 and similarity < 0.97 and overlap < 0.34:
        return False
    if REJECT_WORDS.search(strip_accents(blob)) and overlap < 0.5:
        return False
    return True


def search_youtube(query: str, count: int) -> list[dict]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 30,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    return [entry for entry in entries if entry and entry.get("id")]


def performer_overlap(entry: dict, artist: str, composers: list[str]) -> float:
    """How much of the catalog performer or composer name the video carries."""
    channel = entry.get("channel") or entry.get("uploader") or ""
    blob = f"{entry.get('title') or ''} {channel}"
    scores = [artist_overlap(artist, blob)]
    scores.extend(artist_overlap(name, blob) for name in composers)
    return max(scores) if scores else 0.0


def choose_match(entries: list[dict], title: str, artist: str, composers: list[str]) -> dict | None:
    """Prefer the author or catalog performer, then any other interpreter.

    A result titled as a cover stays eligible. The author bonus is withheld
    from it, so a cover is used when no clean result names the author or the
    catalog performer.
    """
    ranked: list[tuple[float, float, dict]] = []
    for entry in entries:
        score, similarity = score_entry(entry, title, artist, composers)
        if not acceptable(entry, similarity, artist, title, composers):
            continue
        channel = entry.get("channel") or entry.get("uploader") or ""
        plain = strip_accents(f"{entry.get('title') or ''} {channel}")
        named = performer_overlap(entry, artist, composers) >= NAMED_PERFORMER
        if named and not COVER_WORD.search(plain) and not REJECT_WORDS.search(plain):
            score += 30
        ranked.append((score, similarity, entry))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][2]


def download_mp3(video_id: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    stem = destination.with_suffix("")
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(stem) + ".%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "overwrites": True,
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(options) as ydl:
        ydl.download([url])
    if not destination.exists():
        raise RuntimeError(f"MP3 was not created at {destination}")
    # Remove leftover source audio if the postprocessor left it behind.
    for extra in destination.parent.iterdir():
        if extra.stem == stem.name and extra.suffix.lower() != ".mp3":
            extra.unlink(missing_ok=True)
    return destination


def song_file(item: dict) -> Path:
    raw = Path(item.get("file") or "")
    if not raw.is_absolute():
        raw = ROOT / raw
    return raw


def relative_link(path: Path) -> str:
    relative = path.relative_to(SONGS_DIR).as_posix()
    return quote(relative, safe="/")


def write_index(state: dict) -> None:
    downloaded = [
        item
        for item in state["items"].values()
        if item.get("status") == "downloaded" and item.get("file") and song_file(item).exists()
    ]

    def author_name(item: dict) -> str:
        return item.get("author") or item.get("artist_clean") or "Unknown"

    downloaded.sort(key=lambda item: (
        author_sort_key(author_name(item)),
        strip_accents(item.get("title_clean") or "").lower(),
    ))
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in downloaded:
        grouped[author_name(item)].append(item)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Downloaded songs",
        "",
        f"_Updated {now}. {len(downloaded)} song{'s' if len(downloaded) != 1 else ''}._",
        "",
        "Each entry links to the local MP3. One file is kept per title.",
        "",
    ]
    if not downloaded:
        lines.append("_No songs downloaded yet._")
        lines.append("")
    for artist in sorted(grouped, key=author_sort_key):
        lines.append(f"## {artist}")
        lines.append("")
        tracks = sorted(
            grouped[artist],
            key=lambda item: strip_accents(item.get("title_clean") or item.get("title") or "").lower(),
        )
        for item in tracks:
            file_link = relative_link(song_file(item))
            title = item.get("title_clean") or item.get("title") or "Untitled"
            bits = [f"- [{title}]({file_link})"]
            performer = item.get("artist_clean") or ""
            if performer and performer != item.get("author"):
                bits.append(performer)
            if item.get("genre"):
                bits.append(item["genre"])
            if item.get("recording_date"):
                bits.append(f"recorded {item['recording_date']}")
            if item.get("composers"):
                bits.append(item["composers"])
            youtube = item.get("youtube_url")
            if youtube:
                label = item.get("youtube_title") or "YouTube"
                label = label.replace("[", "(").replace("]", ")")
                bits.append(f"[{label}]({youtube})")
            lines.append(" · ".join(bits))
        lines.append("")
    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    save_json_text(INDEX_PATH, "\n".join(lines).rstrip() + "\n")


def save_json_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def remember(state: dict, key: str, payload: dict) -> None:
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    with STATE_LOCK:
        state["items"][key] = payload
        save_json(STATE_PATH, state)
        if payload.get("status") == "downloaded":
            write_index(state)


def author_of(artist: str, names: list[str]) -> str:
    for name in names:
        if plausible_name(name):
            return name
    return artist


def destination_for(author: str, title: str) -> Path:
    return SONGS_DIR / safe_segment(author) / f"{safe_segment(title)}.mp3"


def nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def local_mp3s() -> dict[str, Path]:
    """Map a normalized title to an MP3 already stored under songs/."""
    found: dict[str, Path] = {}
    if not SONGS_DIR.is_dir():
        return found
    for path in SONGS_DIR.rglob("*.mp3"):
        if nonempty_file(path):
            found.setdefault(normalize_text(path.stem), path)
    return found


def recorded_mp3(item: dict) -> Path | None:
    if not item.get("file"):
        return None
    path = song_file(item)
    if nonempty_file(path):
        return path
    return None


def find_downloaded(
    state: dict,
    key: str,
    local: dict[str, Path],
    target: Path | None = None,
) -> Path | None:
    """Return the MP3 for this title when one is already on disk."""
    path = recorded_mp3(state["items"].get(key) or {})
    if path is not None:
        return path
    path = local.get(key)
    if path is not None and nonempty_file(path):
        return path
    if target is not None and nonempty_file(target):
        return target
    return None


def note_existing(state: dict, song: dict, composers: dict[str, str], path: Path) -> None:
    """Record an MP3 that is already on disk so the next run skips it too."""
    raw_title = song.get("title") or ""
    key = title_key(raw_title)
    item = state["items"].get(key) or {}
    if item.get("status") == "downloaded" and recorded_mp3(item) == path:
        return
    title, genre_code, extra = split_title(raw_title)
    artist = clean_artist(song.get("artist"))
    names = composer_names(song, composers, extra)
    author = author_of(artist, names)
    record = base_record(
        song, title, artist, author, names, genre_label(genre_code), status="downloaded"
    )
    record["file"] = str(path.relative_to(ROOT))
    for field in ("youtube_id", "youtube_url", "youtube_title", "channel"):
        if item.get(field):
            record[field] = item[field]
    remember(state, key, record)
    log(f"skip    {path.relative_to(ROOT)}")


def should_skip(state: dict, key: str, local: dict[str, Path]) -> bool:
    item = state["items"].get(key) or {}
    if find_downloaded(state, key, local) is not None:
        return True
    return item.get("status") == "not_found"


def process_song(
    song: dict,
    composers: dict[str, str],
    state: dict,
    search_count: int,
    local: dict[str, Path],
) -> str:
    raw_title = song.get("title") or ""
    title, genre_code, extra = split_title(raw_title)
    key = title_key(raw_title)
    artist = clean_artist(song.get("artist"))
    names = composer_names(song, composers, extra)
    author = author_of(artist, names)
    genre = genre_label(genre_code)
    target = destination_for(author, title)
    existing = find_downloaded(state, key, local, target)
    if existing is not None:
        note_existing(state, song, composers, existing)
        return "skipped"
    # The composer identifies the original work. The performer is the hint
    # when the catalog has no composer credit.
    if names:
        query = f"{title} {names[0]}".strip()
    else:
        query = f"{title} {artist}".strip()
    log(f"search  {author} — {title}")
    try:
        entries = search_youtube(query, search_count)
        match = choose_match(entries, title, artist, names)
        if match is None and normalize_text(query) != normalize_text(title):
            entries = search_youtube(title, search_count)
            match = choose_match(entries, title, artist, names)
    except Exception as exc:  # yt-dlp raises many error types
        remember(state, key, base_record(song, title, artist, author, names, genre, status="error", error=str(exc)))
        log(f"error   {artist} — {title}: {exc}")
        return "error"
    if match is None:
        remember(
            state,
            key,
            base_record(song, title, artist, author, names, genre, status="not_found", error="no close match"),
        )
        log(f"miss    {artist} — {title}")
        return "not_found"
    video_id = match["id"]
    # The file may have landed while this title was being searched.
    if nonempty_file(target):
        record = base_record(song, title, artist, author, names, genre, status="downloaded")
        record.update(match_fields(match, target))
        remember(state, key, record)
        log(f"kept    {target.relative_to(ROOT)}")
        return "skipped"
    log(f"fetch   {author} — {title}")
    try:
        download_mp3(video_id, target)
    except Exception as exc:
        remember(state, key, base_record(song, title, artist, author, names, genre, status="error", error=str(exc)))
        log(f"error   {artist} — {title}: {exc}")
        return "error"
    record = base_record(song, title, artist, author, names, genre, status="downloaded")
    record.update(match_fields(match, target))
    remember(state, key, record)
    log(f"saved   {target.relative_to(ROOT)}")
    return "downloaded"


def base_record(
    song: dict,
    title: str,
    artist: str,
    author: str,
    names: list[str],
    genre: str,
    status: str,
    error: str = "",
) -> dict:
    return {
        "status": status,
        "title": song.get("title") or "",
        "title_clean": title,
        "artist": song.get("artist") or "",
        "artist_clean": artist,
        "author": author,
        "composers": ", ".join(names),
        "genre": genre,
        "catalog_number": song.get("catalog_number") or "",
        "recording_date": song.get("recording_date") or "",
        "source_pdf": song.get("source_pdf") or "",
        "error": error,
    }


def match_fields(match: dict, target: Path) -> dict:
    video_id = match.get("id") or ""
    return {
        "file": str(target.relative_to(ROOT)),
        "youtube_id": video_id,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "youtube_title": match.get("title") or "",
        "channel": match.get("channel") or match.get("uploader") or "",
        "error": "",
    }


def handle_stop(signum, _frame) -> None:
    STOP.set()
    log("Stopping after the current searches. Run the same command to resume.")
    # A second signal exits immediately; completed songs are already on disk.
    signal.signal(signum, signal.SIG_DFL)


def run(workers: int, limit: int | None, search_count: int) -> None:
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    composers = load_composers(COMPOSERS_PATH)
    songs = select_unique(load_catalog(CATALOG_PATH), composers)
    state = load_state(STATE_PATH)
    local = local_mp3s()
    # A short queue keeps searches and downloads overlapping instead of
    # collecting every YouTube match before the first file is saved.
    work: Queue = Queue(maxsize=max(2, workers * 2))
    counts = {"downloaded": 0, "not_found": 0, "error": 0, "skipped": 0, "queued": 0}
    counts_lock = threading.Lock()

    def put_item(item: dict | None) -> bool:
        while not STOP.is_set() or item is None:
            try:
                work.put(item, timeout=0.5)
                return True
            except Full:
                if item is None and not any(thread.is_alive() for thread in threads):
                    return False
                continue
        return False

    def producer() -> None:
        queued = 0
        skipped = 0
        for song in songs:
            if STOP.is_set():
                break
            key = title_key(song.get("title") or "")
            found = find_downloaded(state, key, local)
            if should_skip(state, key, local):
                if found is not None:
                    note_existing(state, song, composers, found)
                skipped += 1
                continue
            if not put_item(song):
                break
            queued += 1
            if limit is not None and queued >= limit:
                break
        with counts_lock:
            counts["skipped"] += skipped
            counts["queued"] = queued
        for _ in range(workers):
            put_item(None)

    def worker() -> None:
        while True:
            song = work.get()
            try:
                if song is None:
                    return
                try:
                    status = process_song(song, composers, state, search_count, local)
                except Exception as exc:
                    status = "error"
                    log(f"error   {exc}")
                with counts_lock:
                    counts[status] = counts.get(status, 0) + 1
            finally:
                work.task_done()

    log(
        f"{len(songs)} unique titles. {len(local)} MP3s already on disk. "
        f"Searching and downloading with {workers} workers."
    )
    threads = [threading.Thread(target=worker, name=f"download-{index}", daemon=True) for index in range(workers)]
    for thread in threads:
        thread.start()
    producer_thread = threading.Thread(target=producer, name="catalog", daemon=True)
    producer_thread.start()
    producer_thread.join()
    for thread in threads:
        thread.join()
    with STATE_LOCK:
        write_index(state)
    log(
        "Finished this run: "
        f"{counts['downloaded']} saved, {counts['not_found']} not found, "
        f"{counts['error']} errors, {counts['skipped']} already settled, "
        f"{counts['queued']} queued."
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3, help="parallel YouTube searches (default: 3)")
    parser.add_argument("--limit", type=int, default=None, help="process at most this many new titles")
    parser.add_argument("--search-count", type=int, default=8, help="YouTube results to score per title")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.search_count < 1:
        parser.error("--search-count must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    run(args.workers, args.limit, args.search_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
