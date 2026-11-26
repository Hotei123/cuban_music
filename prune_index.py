#!/usr/bin/env python3
"""Remove songs/INDEX.md lines whose linked MP3 is not on disk.

Song entries are markdown links relative to songs/. Author headings with no
remaining entries are removed, and the song count in the header is updated.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
SONGS_DIR = ROOT / "songs"
INDEX_PATH = SONGS_DIR / "INDEX.md"

ENTRY_RE = re.compile(r"^- \[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^## ")


def linked_file(line: str) -> Path | None:
    """Return the local MP3 a song line points at, if it is a local link."""
    match = ENTRY_RE.match(line)
    if not match:
        return None
    href = match.group(1).strip()
    if href.startswith(("http://", "https://", "#")):
        return None
    return SONGS_DIR / unquote(href)


def prune_lines(lines: list[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    removed: list[str] = []
    for line in lines:
        path = linked_file(line)
        if path is not None and not path.is_file():
            removed.append(line)
            continue
        kept.append(line)
    return kept, removed


def drop_empty_headings(lines: list[str]) -> list[str]:
    """Drop an author heading when every song under it was removed."""
    pruned: list[str] = []
    index = 0
    while index < len(lines):
        if not HEADING_RE.match(lines[index]):
            pruned.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("#"):
            end += 1
        block = lines[index:end]
        if any(ENTRY_RE.match(line) for line in block):
            pruned.extend(block)
        index = end
    return pruned


def collapse_blank_lines(lines: list[str]) -> list[str]:
    collapsed: list[str] = []
    for line in lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    return collapsed


def rewrite_count(lines: list[str], song_count: int) -> None:
    word = "song" if song_count == 1 else "songs"
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    replacement = f"_Updated {updated}. {song_count} {word}._"
    for index, line in enumerate(lines):
        if line.startswith("_Updated ") and line.endswith("_"):
            lines[index] = replacement
            return


def prune_index(text: str) -> tuple[str, list[str]]:
    lines, removed = prune_lines(text.splitlines())
    if not removed:
        return text if text.endswith("\n") else text + "\n", removed
    lines = drop_empty_headings(lines)
    lines = collapse_blank_lines(lines)
    song_count = sum(1 for line in lines if ENTRY_RE.match(line))
    rewrite_count(lines, song_count)
    return "\n".join(lines).rstrip() + "\n", removed


def main() -> None:
    text = INDEX_PATH.read_text(encoding="utf-8")
    updated, removed = prune_index(text)
    if not removed:
        print(f"No missing files. {INDEX_PATH} unchanged.")
        return
    temporary = INDEX_PATH.with_suffix(INDEX_PATH.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(INDEX_PATH)
    print(f"Removed {len(removed)} {'entry' if len(removed) == 1 else 'entries'} from {INDEX_PATH}:")
    for line in removed:
        path = linked_file(line)
        print(f"  {path.relative_to(SONGS_DIR) if path else line}")


if __name__ == "__main__":
    main()
