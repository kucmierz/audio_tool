"""Split one big audiobook MP3 into per-chapter MP3 files via ffmpeg.

Cutting is done with re-encoding (CBR libmp3lame); cut points come from a
parsed CueSheet. The last chapter has end_ms is None and is cut "to end of
file": ffmpeg gets no -to and decodes to the real EOF, so the fresh output
header carries the correct duration. This kills the audioteka "last chapter
= whole book" bug at the root, because we never read the broken source
duration.

The decisions live in small pure helpers (snap_bitrate, output_path,
build_command) that are unit-tested without running ffmpeg. The side effects
(probe_bitrate, split) are a thin wrapper on top.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from core.cue import Chapter, CueSheet
from core.ffmpeg_locator import ffmpeg_path, ffprobe_path

# Used when probing the source bitrate fails. If config.py grows a single
# source of truth for this, point here at that constant instead.
FALLBACK_BITRATE_KBPS = 128

# Standard CBR rungs we snap the (usually VBR) source average onto. Includes
# low speech rungs (64/96) because audiobooks are often < 128k and we don't
# want to inflate them. Ties snap to the lower rung (smaller file).
_BITRATE_LADDER = (64, 96, 128, 160, 192, 256, 320)

# Characters Windows forbids in filenames, plus control chars.
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# On Windows, stop ffmpeg from flashing a console window per chapter.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class SplitError(RuntimeError):
    """ffmpeg failed on a chapter. Carries ffmpeg's stderr in the message."""


def snap_bitrate(kbps: int) -> int:
    """Snap a raw (VBR-average) kbps onto the nearest standard CBR rung."""
    return min(_BITRATE_LADDER, key=lambda rung: (abs(rung - kbps), rung))


def _sanitize_filename(name: str) -> str:
    """Make a string safe as a Windows filename. Falls back to 'audiobook'."""
    cleaned = _ILLEGAL_FS.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned or "audiobook"


def output_path(chapter: Chapter, book_title: str, outdir: Path, pad: int) -> Path:
    """Build the chapter's output path, e.g. '01 - Zbrojni.mp3'."""
    stem = f"{chapter.number:0{pad}d} - {_sanitize_filename(book_title)}"
    return Path(outdir) / f"{stem}.mp3"


def _ms_to_seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def build_command(
    ffmpeg: Path, source: Path, chapter: Chapter, kbps: int, out_path: Path
) -> list[str]:
    """ffmpeg argv for one chapter.

    -ss/-to go AFTER -i: accurate, decode-based seek, immune to the broken
    source header. The last chapter (end_ms is None) gets no -to and runs to
    EOF.
    """
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i", str(source),
        "-ss", _ms_to_seconds(chapter.start_ms),
    ]
    if chapter.end_ms is not None:
        cmd += ["-to", _ms_to_seconds(chapter.end_ms)]
    cmd += ["-c:a", "libmp3lame", "-b:a", f"{kbps}k", str(out_path)]
    return cmd


def probe_bitrate(source: Path) -> int | None:
    """Average source bitrate in kbps via ffprobe, or None if undetectable."""
    cmd = [
        str(ffprobe_path()),
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-select_streams", "a:0",
        str(source),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=_NO_WINDOW
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or []
    raw = streams[0].get("bit_rate") if streams else None
    if not raw:
        raw = data.get("format", {}).get("bit_rate")
    try:
        return round(int(raw) / 1000)
    except (TypeError, ValueError):
        return None


def split(
    cue: CueSheet,
    source: str | Path,
    outdir: str | Path,
    *,
    on_progress=None,
) -> list[tuple[Chapter, Path]]:
    """Cut `source` into per-chapter MP3s in `outdir`.

    Returns [(chapter, output_path), ...] in order. Raises SplitError on the
    first ffmpeg failure (fail fast -- a gap in the middle of an audiobook is
    useless). If given, on_progress(done, total, chapter) is called after each
    chapter finishes.
    """
    source = Path(source)
    outdir = Path(outdir)
    if not cue.chapters:
        raise SplitError("Cue sheet has no chapters")

    outdir.mkdir(parents=True, exist_ok=True)
    book_title = cue.album_title or source.stem
    total = len(cue.chapters)
    pad = max(2, len(str(total)))

    raw = probe_bitrate(source)
    kbps = snap_bitrate(raw) if raw else FALLBACK_BITRATE_KBPS

    ffmpeg = ffmpeg_path()
    results: list[tuple[Chapter, Path]] = []
    for chapter in cue.chapters:
        out = output_path(chapter, book_title, outdir, pad)
        cmd = build_command(ffmpeg, source, chapter, kbps, out)
        proc = subprocess.run(
            cmd, capture_output=True, text=True, creationflags=_NO_WINDOW
        )
        if proc.returncode != 0:
            raise SplitError(
                f"ffmpeg failed on chapter {chapter.number} "
                f"({chapter.title!r}):\n{proc.stderr.strip()}"
            )
        results.append((chapter, out))
        if on_progress is not None:
            on_progress(len(results), total, chapter)
    return results


def _main(argv: list[str]) -> int:
    # Dry run: python -m core.splitter <cue> <source.mp3> <outdir>
    if len(argv) != 3:
        print("usage: python -m core.splitter <cue> <source.mp3> <outdir>")
        return 2
    from core.cue import parse_cue_file

    cue_path, source, outdir = argv
    cue = parse_cue_file(cue_path)
    width = len(str(len(cue.chapters)))

    def progress(done, total, chapter):
        print(f"[{done:>{width}}/{total}] {chapter.title}")

    results = split(cue, source, outdir, on_progress=progress)
    print(f"done: {len(results)} chapters -> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))