"""Write ID3v2.3 tags onto chapter MP3s cut from an audiobook.

Write-only for now. Three layers, same split as splitter.py:
  build_tags   -- pure: chapter -> frame values, no I/O
  write_tags   -- side effect: stamp one file, wiping leftovers first
  tag_chapters -- orchestrate over the splitter's output list

The read side (for the metadata editor) lands in a later sprint.
"""

from __future__ import annotations

from pathlib import Path

from mutagen.id3 import (
    ID3,
    ID3v1SaveOptions,
    TALB,
    TIT2,
    TPE1,
    TPE2,
    TRCK,
)

from core.cue import Chapter, CueSheet

# ID3v2.3, not 2.4: broader audiobook-player support.
_ID3_VERSION = 3

# frame name -> mutagen frame class, used when materialising the tag set
_FRAME_TYPES = {
    "TALB": TALB,
    "TPE2": TPE2,
    "TPE1": TPE1,
    "TIT2": TIT2,
    "TRCK": TRCK,
}


def build_tags(
    chapter: Chapter, cuesheet: CueSheet, track_no: int, total: int
) -> dict[str, str]:
    """Map one chapter to its ID3 text-frame values. Pure, no I/O.

    track_no is the chapter's 1-based position in the book, NOT the raw
    .cue TRACK number (audioteka numbers from 00). Using the position
    keeps the tag in step with the output filename (01, 02, ...) and
    avoids ever writing a nonsense "0/N".
    """
    return {
        "TALB": cuesheet.album_title,      # album        = book title
        "TPE2": cuesheet.album_performer,  # album artist = author
        "TPE1": chapter.performer,         # artist       = narrator
        "TIT2": chapter.title,             # title        = track title from .cue
        "TRCK": f"{track_no}/{total}",     # track        = position / count
    }


def write_tags(path: Path, tags: dict[str, str]) -> None:
    """Replace the file's tag with exactly `tags`, written as ID3v2.3.

    Starts from an empty ID3 on purpose. The re-encode copies metadata
    from the source (wrong artist, stray encoder/date frames), so a fresh
    tag is the only way to guarantee that anything we don't set is gone.
    encoding=3 (UTF-8) is requested; mutagen down-converts to UTF-16 for
    v2.3, which is correct and keeps Polish characters intact.
    """
    id3 = ID3()
    for key, value in tags.items():
        frame_cls = _FRAME_TYPES[key]
        id3.add(frame_cls(encoding=3, text=value))
    id3.save(path, v2_version=_ID3_VERSION, v1=ID3v1SaveOptions.REMOVE)


def tag_chapters(results: list[tuple[Chapter, Path]], cuesheet: CueSheet) -> None:
    """Tag every freshly-cut chapter file.

    `results` is what the splitter returns: (chapter, output_path) pairs
    in book order. Track numbers come from that order, 1-based; the total
    is the book's chapter count.
    """
    total = len(cuesheet.chapters)
    for track_no, (chapter, path) in enumerate(results, start=1):
        tags = build_tags(chapter, cuesheet, track_no, total)
        write_tags(path, tags)