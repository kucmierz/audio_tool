"""Read and write ID3v2.3 tags (and cover art) for the audiobook tooling.

Two jobs share this module, kept apart the way splitter.py keeps pure logic
from side effects.

Write side (sprint 5) -- stamping freshly-cut chapters:
  build_tags    -- pure: chapter -> frame values, no I/O
  detect_mime   -- pure: image bytes -> MIME, via magic bytes
  find_cover    -- locate a cover image sitting next to the source file
  write_tags    -- side effect: stamp one file, wiping leftovers first
  tag_chapters  -- orchestrate over the splitter's output list

Read / edit side (sprint 6) -- backing the metadata editor over real files:
  AudioMetadata -- the five managed fields + cover presence, as plain data
  read_tags     -- side effect: file -> AudioMetadata (empty model if untagged)
  read_cover    -- side effect: pull embedded cover bytes + MIME, or None
  update_tags   -- side effect: set the five managed frames, keep everything else
  set_cover     -- side effect: replace the embedded cover
  remove_cover  -- side effect: drop the embedded cover

write_tags vs update_tags is the key split. write_tags starts from a blank
tag because a re-encode drags the source's metadata along and we want the
chapter clean. update_tags edits files that already live in someone's
library -- year, genre, comments, lyrics must survive -- so it touches only
the managed frames and leaves the rest (and the cover) alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mutagen.id3 import (
    APIC,
    ID3,
    ID3NoHeaderError,
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

# The managed frames, in editor order: model attribute <-> ID3 frame key.
# Single source of truth for both reading into and writing out of
# AudioMetadata, so the two directions can never drift apart.
_MANAGED = (
    ("album", "TALB"),         # album        = book title
    ("album_artist", "TPE2"),  # album artist = author
    ("artist", "TPE1"),        # artist       = narrator
    ("title", "TIT2"),         # title        = track title
    ("track", "TRCK"),         # track        = position / count
)

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


# --------------------------------------------------------------------------
# Read / edit side (sprint 6)
# --------------------------------------------------------------------------


@dataclass
class AudioMetadata:
    """The editor's view of one file: the five managed tags + cover presence.

    Text fields are plain strings; a missing frame reads as "". Writing an
    empty string back through update_tags removes that frame -- that's how
    the UI clears a field. has_cover / cover_mime are read-only here:
    update_tags ignores them, cover edits go through set_cover/remove_cover.
    """

    album: str = ""
    album_artist: str = ""
    artist: str = ""
    title: str = ""
    track: str = ""
    has_cover: bool = False
    cover_mime: str | None = None


def _text(id3: ID3, key: str) -> str:
    """First value of a text frame, or "" when the frame is absent/empty.

    Frames are treated as single-valued -- true for everything we manage on
    an audiobook (one album, one narrator, ...). A stray multi-value frame
    shows its first entry rather than crashing.
    """
    frame = id3.get(key)
    if frame is None or not frame.text:
        return ""
    return str(frame.text[0])


def _first_apic(id3: ID3) -> APIC | None:
    """The first embedded picture frame, or None."""
    frames = id3.getall("APIC")
    return frames[0] if frames else None


def _parse_tags(id3: ID3) -> AudioMetadata:
    """Build an AudioMetadata from a loaded tag. Pure, no I/O."""
    apic = _first_apic(id3)
    values = {attr: _text(id3, key) for attr, key in _MANAGED}
    return AudioMetadata(
        **values,
        has_cover=apic is not None,
        cover_mime=apic.mime if apic is not None else None,
    )


def _apply_text(id3: ID3, key: str, value: str) -> None:
    """Set a managed text frame, or remove it when the value is empty."""
    if value:
        id3.setall(key, [_FRAME_TYPES[key](encoding=3, text=value)])
    else:
        id3.delall(key)


def read_tags(path: Path) -> AudioMetadata:
    """Read the managed fields + cover presence from a file.

    A file with no ID3 tag yields an all-empty AudioMetadata rather than an
    error -- the editor just shows blank fields, ready to fill.
    """
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        return AudioMetadata()
    return _parse_tags(id3)


def read_cover(path: Path) -> tuple[bytes, str] | None:
    """Return (image_bytes, mime) for the embedded cover, or None.

    The stored MIME is used as-is; if it's empty we sniff the bytes so the
    caller always gets something usable, and give up (None) only when even
    that fails.
    """
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        return None
    apic = _first_apic(id3)
    if apic is None:
        return None
    mime = apic.mime or detect_mime(apic.data)
    if mime is None:
        return None
    return (apic.data, mime)


def update_tags(path: Path, meta: AudioMetadata) -> None:
    """Write the five managed frames onto an existing file, in place.

    Only the managed frames are touched; every other frame (year, genre,
    comments, lyrics, encoder info) and the cover art are left exactly as
    they were. An empty field removes its frame. A file with no tag yet gets
    a fresh one. Saved as ID3v2.3 -- an existing v2.4 tag is down-converted
    to match the rest of the toolchain. v1 is updated only if already present.
    """
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    for attr, key in _MANAGED:
        _apply_text(id3, key, getattr(meta, attr))
    id3.save(path, v2_version=_ID3_VERSION, v1=ID3v1SaveOptions.UPDATE)


def set_cover(path: Path, data: bytes, mime: str | None = None) -> None:
    """Embed `data` as the single front-cover image, replacing any existing.

    MIME is sniffed from the bytes when not given; if sniffing fails (not
    PNG/JPEG) it raises ValueError rather than writing a broken frame. Other
    frames are preserved; a file with no tag yet gets a fresh one. ID3v2.3.
    """
    resolved = mime or detect_mime(data)
    if resolved is None:
        raise ValueError("unrecognised image format (expected PNG or JPEG)")
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    id3.delall("APIC")  # one cover only: drop any existing picture frames
    id3.add(
        APIC(
            encoding=3,
            mime=resolved,
            type=PictureType.COVER_FRONT,
            desc="Cover",
            data=data,
        )
    )
    id3.save(path, v2_version=_ID3_VERSION, v1=ID3v1SaveOptions.UPDATE)


def remove_cover(path: Path) -> None:
    """Drop every embedded picture frame. No-op if there's none (or no tag)."""
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        return
    if id3.getall("APIC"):
        id3.delall("APIC")
        id3.save(path, v2_version=_ID3_VERSION, v1=ID3v1SaveOptions.UPDATE)