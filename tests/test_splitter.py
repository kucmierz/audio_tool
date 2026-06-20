"""Tests for core.splitter.

Pure-logic tests (no ffmpeg needed) cover the decisions: bitrate snapping,
output filenames, and ffmpeg argv construction -- including the last-chapter
"to EOF" rule (no -to). One integration test actually runs ffmpeg; it is
skipped wherever the bundled binaries aren't present.
"""

import subprocess
from pathlib import Path

import pytest

from core.cue import Chapter, CueSheet
from core.splitter import (
    SplitError,
    build_command,
    output_path,
    snap_bitrate,
    split,
)


def _chapter(number, start_ms, end_ms, title="Sciezka", performer=None):
    return Chapter(
        number=number,
        title=title,
        performer=performer,
        start_ms=start_ms,
        end_ms=end_ms,
    )


# --- snap_bitrate -----------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        (128, 128),
        (130, 128),
        (125, 128),
        (96, 96),
        (64, 64),
        (60, 64),
        (320, 320),
        (400, 320),  # above the top rung
        (10, 64),    # below the bottom rung
        (112, 96),   # midpoint 96<->128 -> ties to lower
        (144, 128),  # midpoint 128<->160 -> ties to lower
    ],
)
def test_snap_bitrate(raw, expected):
    assert snap_bitrate(raw) == expected


# --- output_path ------------------------------------------------------------

def test_output_path_basic(tmp_path):
    ch = _chapter(1, 0, 1000)
    assert output_path(ch, "Zbrojni", tmp_path, pad=2) == tmp_path / "01 - Zbrojni.mp3"


def test_output_path_padding_width():
    ch = _chapter(7, 0, 1000)
    assert output_path(ch, "Book", Path("/out"), pad=3).name == "007 - Book.mp3"


def test_output_path_sanitizes_illegal_chars():
    ch = _chapter(3, 0, 1000)
    p = output_path(ch, 'Plan B: A/B "test"', Path("/out"), pad=2)
    assert p.name == "03 - Plan B A B test.mp3"


def test_output_path_empty_title_falls_back():
    ch = _chapter(1, 0, 1000)
    assert output_path(ch, ":::", Path("/out"), pad=2).name == "01 - audiobook.mp3"


# --- build_command ----------------------------------------------------------

def test_build_command_non_last_has_to():
    ch = _chapter(1, 0, 1731696)
    cmd = build_command(Path("ffmpeg"), Path("in.mp3"), ch, 128, Path("01 - X.mp3"))
    assert "-to" in cmd
    # -ss/-to come AFTER -i (accurate decode-based seek)
    assert cmd.index("-i") < cmd.index("-ss") < cmd.index("-to")
    assert cmd[cmd.index("-ss") + 1] == "0.000"
    assert cmd[cmd.index("-to") + 1] == "1731.696"
    assert "libmp3lame" in cmd
    assert "128k" in cmd


def test_build_command_last_chapter_has_no_to():
    # The fix: last chapter (end_ms is None) cuts to EOF, so no -to.
    ch = _chapter(22, 5_000_000, None)
    cmd = build_command(Path("ffmpeg"), Path("in.mp3"), ch, 160, Path("22 - X.mp3"))
    assert "-to" not in cmd
    assert cmd[cmd.index("-ss") + 1] == "5000.000"
    assert cmd[-1] == "22 - X.mp3"
    assert "160k" in cmd


def test_build_command_overwrites_and_quiets():
    ch = _chapter(1, 0, 1000)
    cmd = build_command(Path("ffmpeg"), Path("in.mp3"), ch, 128, Path("o.mp3"))
    assert "-y" in cmd        # overwrite without prompting (would hang a subprocess)
    assert "-nostdin" in cmd  # don't swallow stdin in a GUI subprocess


# --- split: guards ----------------------------------------------------------

def test_split_empty_chapters_raises(tmp_path):
    cue = CueSheet(album_title="X", album_performer=None, source_file="x.mp3")
    with pytest.raises(SplitError):
        split(cue, tmp_path / "x.mp3", tmp_path / "out")


# --- integration: real ffmpeg (skipped if bundled binaries unavailable) -----

def _ffmpeg_available():
    try:
        from core.ffmpeg_locator import ffmpeg_path, ffprobe_path

        return ffmpeg_path().is_file() and ffprobe_path().is_file()
    except Exception:
        return False


def _duration(path, ffprobe):
    out = subprocess.run(
        [str(ffprobe), "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


@pytest.mark.skipif(not _ffmpeg_available(), reason="bundled ffmpeg not present")
def test_split_end_to_end(tmp_path):
    from core.ffmpeg_locator import ffmpeg_path, ffprobe_path

    # Synthesize a 30s source.
    source = tmp_path / "in.mp3"
    subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
         "-c:a", "libmp3lame", "-q:a", "5", str(source)],
        check=True,
    )
    cue = CueSheet(album_title="Test", album_performer="A", source_file="in.mp3")
    cue.chapters = [
        _chapter(1, 0, 10_000),
        _chapter(2, 10_000, 25_000),
        _chapter(3, 25_000, None),  # to EOF
    ]
    seen = []
    results = split(
        cue, source, tmp_path / "out",
        on_progress=lambda d, t, c: seen.append(d),
    )

    assert [p.name for _, p in results] == [
        "01 - Test.mp3", "02 - Test.mp3", "03 - Test.mp3",
    ]
    assert all(p.is_file() for _, p in results)
    assert seen == [1, 2, 3]  # progress fired once per chapter

    # Durations match the cut points (with ~26ms MP3 frame padding tolerance).
    ffprobe = ffprobe_path()
    assert 9.5 < _duration(results[0][1], ffprobe) < 10.6   # 0..10
    assert 14.5 < _duration(results[1][1], ffprobe) < 15.6  # 10..25
    assert 4.5 < _duration(results[2][1], ffprobe) < 5.6    # 25..EOF == ~5s