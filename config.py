# Bitrate (kbps) used when the source bitrate cannot be detected.
FALLBACK_BITRATE_KBPS = 128

# Output filename pattern for chapters, e.g. "01 - Zbrojni.mp3"
# (zero-padded track number + album title). Used from sprint 3 on.
CHAPTER_FILENAME_TEMPLATE = "{number:02d} - {album}.mp3"

# Chapter tag scheme (implemented in sprint 5):
#   Album        = book title   (.cue global TITLE)
#   Album Artist = author       (.cue global PERFORMER)
#   Artist       = narrator     (track-level PERFORMER)
#   Title        = track title from .cue
#   Track number = n/total
