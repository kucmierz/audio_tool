import sys
from pathlib import Path

# Subdirectory holding the bundled binaries, relative to the app root.
_VENDOR_SUBDIR = Path("vendor") / "ffmpeg"


def _candidate_dirs() -> list[Path]:
    """Directories where vendor/ffmpeg may live, in priority order."""
    candidates = []
    if getattr(sys, "frozen", False):
        # PyInstaller: next to the exe, or inside the unpacked bundle
        # (_MEIPASS points at _internal/ in one-folder mode).
        candidates.append(Path(sys.executable).parent / _VENDOR_SUBDIR)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / _VENDOR_SUBDIR)
    else:
        # Source run: project root = parent of core/.
        project_root = Path(__file__).resolve().parent.parent
        candidates.append(project_root / _VENDOR_SUBDIR)
    return candidates


def _find_binary(name: str) -> Path:
    for directory in _candidate_dirs():
        path = directory / name
        if path.is_file():
            return path
    searched = ", ".join(str(d) for d in _candidate_dirs())
    raise FileNotFoundError(
        f"{name} not found. Put it in vendor/ffmpeg/. Searched: {searched}"
    )


def ffmpeg_path() -> Path:
    return _find_binary("ffmpeg.exe")


def ffprobe_path() -> Path:
    return _find_binary("ffprobe.exe")
