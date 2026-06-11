"""Parser for .cue files (audioteka.pl flavour and plain cue sheets).

A cue sheet describes chapters inside one big audio file. Chapter start
times come from two possible sources, in priority order:

1. audioteka's REM line: `REM "NNNN - file.mp3" <offset>` where the last
   number is the start offset in MILLISECONDS (most precise),
2. standard `INDEX 01 MM:SS:FF` where FF is CD frames (75 per second),
   NOT hundredths of a second.

A chapter ends where the next one starts. The last chapter has no
declared end (end_ms is None) -- it runs until the physical end of the
audio file. This is deliberate: the splitter must not trust the (often
broken) duration from the source file header.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

_RE_TITLE = re.compile(r'^TITLE\s+"([^"]*)"', re.IGNORECASE)
_RE_PERFORMER = re.compile(r'^PERFORMER\s+"([^"]*)"', re.IGNORECASE)
_RE_FILE = re.compile(r'^FILE\s+"([^"]*)"', re.IGNORECASE)
_RE_TRACK = re.compile(r"^TRACK\s+(\d+)\s+AUDIO", re.IGNORECASE)
# audioteka: REM "0001 - book.mp3" 1731696  -> offset in ms
_RE_REM_OFFSET_MS = re.compile(r'^REM\s+"[^"]*"\s+(\d+)\s*$', re.IGNORECASE)
_RE_INDEX_01 = re.compile(r"^INDEX\s+01\s+(\d+):(\d{1,2}):(\d{1,2})", re.IGNORECASE)


@dataclass
class Chapter:
    number: int  # sequential, 1-based (TRACK numbering in file may start at 00)
    title: str
    performer: str | None  # narrator (track-level PERFORMER)
    start_ms: int
    end_ms: int | None  # None = until end of file (last chapter)

    @property
    def duration_ms(self) -> int | None:
        if self.end_ms is None:
            return None
        return self.end_ms - self.start_ms


@dataclass
class CueSheet:
    album_title: str | None  # book title (global TITLE)
    album_performer: str | None  # author (global PERFORMER)
    source_file: str | None  # FILE "...": the big mp3 this sheet describes
    chapters: list[Chapter] = field(default_factory=list)


@dataclass
class _RawTrack:
    title: str | None = None
    performer: str | None = None
    rem_offset_ms: int | None = None
    index_ms: int | None = None


def _frames_to_ms(minutes: int, seconds: int, frames: int) -> int:
    # FF = CD frames, 75 per second.
    return (minutes * 60 + seconds) * 1000 + round(frames * 1000 / 75)


def parse_cue_text(text: str) -> CueSheet:
    """Parse cue sheet content. Raises ValueError on a sheet we can't use."""
    sheet = CueSheet(album_title=None, album_performer=None, source_file=None)
    raw_tracks: list[_RawTrack] = []
    current: _RawTrack | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if _RE_TRACK.match(line):
            current = _RawTrack()
            raw_tracks.append(current)
            continue

        m = _RE_TITLE.match(line)
        if m:
            if current is None:
                sheet.album_title = m.group(1)
            else:
                current.title = m.group(1)
            continue

        m = _RE_PERFORMER.match(line)
        if m:
            if current is None:
                sheet.album_performer = m.group(1)
            else:
                current.performer = m.group(1)
            continue

        m = _RE_FILE.match(line)
        if m:
            sheet.source_file = m.group(1)
            continue

        if current is not None:
            m = _RE_REM_OFFSET_MS.match(line)
            if m:
                current.rem_offset_ms = int(m.group(1))
                continue
            m = _RE_INDEX_01.match(line)
            if m:
                current.index_ms = _frames_to_ms(
                    int(m.group(1)), int(m.group(2)), int(m.group(3))
                )
                continue
        # Anything else (incl. the top-level REM <base64> token) is ignored.

    if not raw_tracks:
        raise ValueError("No TRACK entries found in cue sheet")

    starts: list[int] = []
    for i, raw in enumerate(raw_tracks):
        start = raw.rem_offset_ms if raw.rem_offset_ms is not None else raw.index_ms
        if start is None:
            raise ValueError(f"Track {i + 1} has neither REM offset nor INDEX 01")
        starts.append(start)

    for prev, nxt in zip(starts, starts[1:]):
        if nxt < prev:
            raise ValueError("Track start times are not in increasing order")

    total = len(raw_tracks)
    for i, raw in enumerate(raw_tracks):
        end = starts[i + 1] if i + 1 < total else None  # last chapter: until EOF
        sheet.chapters.append(
            Chapter(
                number=i + 1,
                title=raw.title or f"Track {i + 1:02d}",
                performer=raw.performer,
                start_ms=starts[i],
                end_ms=end,
            )
        )
    return sheet


def parse_cue_file(path: str | Path) -> CueSheet:
    """Read and parse a .cue file. Tries UTF-8 first, then cp1250."""
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1250")
    return parse_cue_text(text)
