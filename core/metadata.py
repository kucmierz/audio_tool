"""Write ID3v2.3 tags (and cover art) onto chapter MP3s cut from an audiobook.

Write-only for now. Same split as splitter.py:
  build_tags   -- pure: chapter -> frame values, no I/O
  detect_mime  -- pure: image bytes -> MIME, via magic bytes
  find_cover   -- locate a cover image sitting next to the source file
  write_tags   -- side effect: stamp one file, wiping leftovers first
  tag_chapters -- orchestrate over the splitter's output list

The read side (for the metadata editor) lands in a later sprint.
"""

from __future__ import annotations

from pathlib import Path

from mutagen.id3 import (
    APIC,
    ID3,
    ID3v1SaveOptions,
    PictureType,
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

# cover extensions, checked in this order: .png wins a tie
_COVER_EXTS = (".png", ".jpg", ".jpeg")


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


def detect_mime(data: bytes) -> str | None:
    """Image MIME from magic bytes. Returns None if unrecognised.

    Replaces imghdr, which was removed in Python 3.13. We only need the
    two formats we accept (PNG, JPEG); anything else is treated as "no
    usable cover" rather than guessed.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def find_cover(source_path: Path) -> Path | None:
    """Find a cover image next to the source file, sharing its stem.

    e.g. zbrojni.mp3 -> zbrojni.png / zbrojni.jpg in the same folder.
    .png is preferred. Returns None when no candidate exists.
    """
    for ext in _COVER_EXTS:
        candidate = source_path.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None


def write_tags(
    path: Path,
    tags: dict[str, str],
    cover: tuple[bytes, str] | None = None,
) -> None:
    """Replace the file's tag with exactly `tags` (+ cover), as ID3v2.3.

    Starts from an empty ID3 on purpose. The re-encode copies metadata
    from the source (wrong artist, stray encoder/date frames, maybe an old
    cover), so a fresh tag is the only way to guarantee that anything we
    don't set is gone. `cover` is (image_bytes, mime); pass None to skip.
    encoding=3 (UTF-8) is requested; mutagen down-converts to UTF-16 for
    v2.3, which keeps Polish characters intact.
    """
    id3 = ID3()
    for key, value in tags.items():
        frame_cls = _FRAME_TYPES[key]
        id3.add(frame_cls(encoding=3, text=value))
    if cover is not None:
        data, mime = cover
        id3.add(
            APIC(
                encoding=3,
                mime=mime,
                type=PictureType.COVER_FRONT,
                desc="Cover",
                data=data,
            )
        )
    id3.save(path, v2_version=_ID3_VERSION, v1=ID3v1SaveOptions.REMOVE)


def tag_chapters(
    results: list[tuple[Chapter, Path]],
    cuesheet: CueSheet,
    source_path: Path | None = None,
) -> None:
    """Tag every freshly-cut chapter file, embedding a shared cover.

    `results` is what the splitter returns: (chapter, output_path) pairs
    in book order. Track numbers come from that order, 1-based; the total
    is the book's chapter count. `source_path` is the original input file;
    if given, a sibling cover image is detected once and embedded in every
    chapter. An unreadable or missing cover is silently skipped.
    """
    total = len(cuesheet.chapters)

    cover: tuple[bytes, str] | None = None
    if source_path is not None:
        cover_path = find_cover(source_path)
        if cover_path is not None:
            data = cover_path.read_bytes()
            mime = detect_mime(data)
            if mime is not None:
                cover = (data, mime)

    for track_no, (chapter, path) in enumerate(results, start=1):
        tags = build_tags(chapter, cuesheet, track_no, total)
        write_tags(path, tags, cover)