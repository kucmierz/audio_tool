import pytest

from core.cue import parse_cue_file, parse_cue_text

# audioteka-style sheet: ms offsets in REM, TRACK numbering from 00,
# a base64-ish REM token that must be ignored.
AUDIOTEKA_CUE = """\
PERFORMER "Terry Pratchett"
TITLE "Zbrojni"
FILE "zbrojni.mp3" MP3
REM dGhpcyBpcyBub3QgYSByZWFsIHRva2Vu==
  TRACK 00 AUDIO
    PERFORMER "Maciej Kowalik"
    REM "0000 - zbrojni.mp3" 0
    TITLE "Sciezka 01"
    INDEX 01 00:00:00
  TRACK 01 AUDIO
    PERFORMER "Maciej Kowalik"
    REM "0001 - zbrojni.mp3" 1731696
    TITLE "Sciezka 02"
    INDEX 01 28:51:52
  TRACK 02 AUDIO
    PERFORMER "Maciej Kowalik"
    REM "0002 - zbrojni.mp3" 3700000
    TITLE "Sciezka 03"
    INDEX 01 61:40:00
"""

# Plain cue from another source: no ms offsets, INDEX only.
PLAIN_CUE = """\
TITLE "Some Book"
PERFORMER "Some Author"
FILE "book.mp3" MP3
TRACK 01 AUDIO
  TITLE "Intro"
  INDEX 01 00:00:00
TRACK 02 AUDIO
  TITLE "Chapter One"
  INDEX 01 01:30:45
"""


def test_globals_and_source_file():
    sheet = parse_cue_text(AUDIOTEKA_CUE)
    assert sheet.album_title == "Zbrojni"
    assert sheet.album_performer == "Terry Pratchett"
    assert sheet.source_file == "zbrojni.mp3"


def test_chapters_numbered_sequentially_from_1_even_if_track_starts_at_00():
    sheet = parse_cue_text(AUDIOTEKA_CUE)
    assert [c.number for c in sheet.chapters] == [1, 2, 3]


def test_rem_ms_offset_takes_priority_over_index():
    sheet = parse_cue_text(AUDIOTEKA_CUE)
    # INDEX 28:51:52 would give 1731693 ms; REM says 1731696.
    assert sheet.chapters[1].start_ms == 1731696


def test_ends_come_from_next_start_and_last_is_none():
    sheet = parse_cue_text(AUDIOTEKA_CUE)
    assert sheet.chapters[0].end_ms == 1731696
    assert sheet.chapters[1].end_ms == 3700000
    assert sheet.chapters[2].end_ms is None  # last chapter: until EOF
    assert sheet.chapters[2].duration_ms is None


def test_track_performer_is_narrator():
    sheet = parse_cue_text(AUDIOTEKA_CUE)
    assert all(c.performer == "Maciej Kowalik" for c in sheet.chapters)


def test_index_fallback_uses_cd_frames_not_hundredths():
    sheet = parse_cue_text(PLAIN_CUE)
    # 01:30:45 -> (1*60 + 30) * 1000 + 45/75 * 1000 = 90600 ms
    assert sheet.chapters[1].start_ms == 90600
    assert sheet.chapters[0].end_ms == 90600


def test_missing_timing_raises():
    bad = 'TRACK 01 AUDIO\n  TITLE "No timing here"\n'
    with pytest.raises(ValueError, match="neither"):
        parse_cue_text(bad)


def test_no_tracks_raises():
    with pytest.raises(ValueError, match="No TRACK"):
        parse_cue_text('TITLE "Empty"\n')


def test_decreasing_starts_raise():
    bad = (
        'TRACK 01 AUDIO\n  REM "a.mp3" 5000\n'
        'TRACK 02 AUDIO\n  REM "a.mp3" 1000\n'
    )
    with pytest.raises(ValueError, match="increasing"):
        parse_cue_text(bad)


def test_missing_title_gets_fallback_name():
    cue = 'TRACK 01 AUDIO\n  REM "a.mp3" 0\n'
    sheet = parse_cue_text(cue)
    assert sheet.chapters[0].title == "Track 01"


def test_reads_cp1250_file(tmp_path):
    content = 'TITLE "Zażółć gęślą jaźń"\nTRACK 01 AUDIO\n  REM "a.mp3" 0\n'
    path = tmp_path / "test.cue"
    path.write_bytes(content.encode("cp1250"))
    sheet = parse_cue_file(path)
    assert sheet.album_title == "Zażółć gęślą jaźń"


def test_reads_utf8_with_bom(tmp_path):
    content = 'TITLE "Zażółć"\nTRACK 01 AUDIO\n  REM "a.mp3" 0\n'
    path = tmp_path / "test.cue"
    path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    sheet = parse_cue_file(path)
    assert sheet.album_title == "Zażółć"
