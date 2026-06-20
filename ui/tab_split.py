"""Audiobook splitting tab (sprint 4).

Scans a working folder for "books" -- an .mp3 that has a sibling .cue of the
same stem. Shows them in a checkable list, previews chapters of the selected
book, and cuts the checked ones via core.splitter on a background thread.

Cut chapters go into a per-book "<album>/" subfolder, so re-scanning the same
folder never mistakes already-cut chapter files for new sources. The folder
scan is non-recursive for the same reason.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.cue import parse_cue_file
from core.ffmpeg_locator import ffprobe_path
from core.splitter import SplitError, split

# Windows: stop ffprobe from flashing a console window. No-op elsewhere.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Pure helpers (no Qt) -- trivially testable.
# ---------------------------------------------------------------------------

_ILLEGAL = '<>:"/\\|?*'


def safe_name(name: str) -> str:
    """Strip characters Windows forbids in a folder/file name."""
    cleaned = "".join("_" if c in _ILLEGAL else c for c in name)
    cleaned = cleaned.strip().rstrip(".")
    return cleaned or "audiobook"


def fmt_hms(ms) -> str:
    """Milliseconds -> 'HH:MM:SS'. None -> em dash."""
    if ms is None:
        return "—"
    total = int(ms) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def chapter_durations(chapters, total_ms):
    """Per-chapter duration in ms. The last chapter's end is the real file
    length (total_ms); if that's unknown (probe failed) it stays None."""
    durations = []
    for i, ch in enumerate(chapters):
        if i + 1 < len(chapters):
            durations.append(chapters[i + 1].start_ms - ch.start_ms)
        elif total_ms is not None:
            durations.append(total_ms - ch.start_ms)
        else:
            durations.append(None)
    return durations


def probe_source(mp3: Path):
    """One ffprobe call -> (duration_ms, bitrate_kbps). Either may be None if
    ffprobe is missing or the field isn't reported."""
    try:
        exe = str(ffprobe_path())
    except FileNotFoundError:
        return None, None
    cmd = [
        exe, "-v", "error",
        "-show_entries", "format=duration,bit_rate",
        "-of", "json", str(mp3),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, creationflags=_NO_WINDOW
        )
        fmt = json.loads(proc.stdout).get("format", {})
    except (OSError, ValueError):
        return None, None
    duration = fmt.get("duration")
    bit_rate = fmt.get("bit_rate")
    duration_ms = int(float(duration) * 1000) if duration else None
    bitrate_kbps = round(int(bit_rate) / 1000) if bit_rate else None
    return duration_ms, bitrate_kbps


@dataclass
class Book:
    source: Path
    cue: object  # CueSheet
    album: str
    author: str
    chapter_count: int
    total_ms: object  # int or None
    bitrate_kbps: object  # int or None

    @property
    def label(self) -> str:
        author = self.author or "—"
        return (
            f"{self.album}   ·   {author}   ·   "
            f"{self.chapter_count} rozdz.   ·   {fmt_hms(self.total_ms)}"
        )


def discover_books(folder: Path):
    """Non-recursive scan: every .mp3 with a sibling .cue of the same stem."""
    books = []
    for mp3 in sorted(folder.iterdir()):
        if mp3.suffix.lower() != ".mp3":
            continue
        cue_path = mp3.with_suffix(".cue")
        if not cue_path.exists():
            continue
        try:
            cue = parse_cue_file(str(cue_path))
        except Exception:
            continue  # unparseable .cue -- skip quietly, don't break the scan
        total_ms, bitrate = probe_source(mp3)
        books.append(
            Book(
                source=mp3,
                cue=cue,
                album=cue.album_title or mp3.stem,
                author=cue.album_performer or "",
                chapter_count=len(cue.chapters),
                total_ms=total_ms,
                bitrate_kbps=bitrate,
            )
        )
    return books


# ---------------------------------------------------------------------------
# Background splitting
# ---------------------------------------------------------------------------

class SplitWorker(QObject):
    """Cuts a batch of books off the GUI thread. One book at a time; a failure
    in one book is recorded and the batch carries on to the next."""

    book_started = Signal(int, int, str)  # index (1-based), total books, album
    chapter_done = Signal(int, int)       # done, total chapters (this book)
    finished = Signal(list)               # [(album, ok: bool, detail: str), ...]

    def __init__(self, jobs):
        super().__init__()
        self._jobs = jobs  # [(Book, outdir: Path), ...]

    @Slot()
    def run(self):
        results = []
        total = len(self._jobs)
        for i, (book, outdir) in enumerate(self._jobs, start=1):
            self.book_started.emit(i, total, book.album)

            def progress(done, ch_total, chapter):
                self.chapter_done.emit(done, ch_total)

            try:
                split(book.cue, book.source, outdir, on_progress=progress)
                results.append((book.album, True, str(outdir)))
            except SplitError as exc:
                results.append((book.album, False, str(exc)))
            except Exception as exc:  # missing ffmpeg, permissions, etc.
                results.append((book.album, False, f"{type(exc).__name__}: {exc}"))
        self.finished.emit(results)


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------

class SplitTab(QWidget):
    """Audiobook splitting tab."""

    def __init__(self):
        super().__init__()
        self._books = []
        self._out_root = None  # Path; defaults to the scanned folder
        self._thread = None
        self._worker = None
        self._build_ui()
        self._set_running(False)
        self._refresh_start_enabled()

    # ---- construction ----------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Source selection
        src_row = QHBoxLayout()
        self.btn_folder = QPushButton("Wskaż folder…")
        self.btn_mp3 = QPushButton("Wskaż MP3…")
        self.btn_folder.clicked.connect(self._pick_folder)
        self.btn_mp3.clicked.connect(self._pick_mp3)
        self.lbl_source = QLabel("Nie wybrano folderu.")
        self.lbl_source.setStyleSheet("color: gray;")
        src_row.addWidget(self.btn_folder)
        src_row.addWidget(self.btn_mp3)
        src_row.addWidget(self.lbl_source, 1)
        root.addLayout(src_row)

        # Output root
        out_row = QHBoxLayout()
        self.btn_out = QPushButton("Folder wyjściowy…")
        self.btn_out.clicked.connect(self._pick_out_root)
        self.lbl_out = QLabel("—")
        self.lbl_out.setStyleSheet("color: gray;")
        out_row.addWidget(self.btn_out)
        out_row.addWidget(self.lbl_out, 1)
        root.addLayout(out_row)

        # Master/detail: checkable book list over chapter table
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.list_books = QListWidget()
        self.list_books.currentRowChanged.connect(self._show_chapters)
        self.list_books.itemChanged.connect(self._refresh_start_enabled)
        splitter.addWidget(self.list_books)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Tytuł", "Start", "Długość"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        splitter.addWidget(self.table)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([150, 360])
        root.addWidget(splitter, 1)

        # Info line (detected bitrate of the selected book)
        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color: gray;")
        root.addWidget(self.lbl_info)

        # Progress + start
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m rozdz.")
        self.lbl_status = QLabel("")
        self.btn_start = QPushButton("Potnij zaznaczone")
        self.btn_start.clicked.connect(self._start)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.lbl_status)
        bottom.addWidget(self.btn_start)
        root.addLayout(bottom)

    # ---- source / output selection --------------------------------------

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Wybierz folder z audiobookami"
        )
        if folder:
            self._load_folder(Path(folder))

    def _pick_mp3(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Wybierz plik MP3", "", "Pliki MP3 (*.mp3)"
        )
        if path:
            self._load_folder(Path(path).parent)

    def _pick_out_root(self):
        start = str(self._out_root) if self._out_root else ""
        folder = QFileDialog.getExistingDirectory(self, "Folder wyjściowy", start)
        if folder:
            self._out_root = Path(folder)
            self._update_out_label()

    def _load_folder(self, folder: Path):
        self.lbl_source.setText(str(folder))
        self.lbl_source.setStyleSheet("")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._books = discover_books(folder)
        finally:
            QApplication.restoreOverrideCursor()
        self._out_root = folder
        self._update_out_label()
        self._populate_books()
        self._refresh_start_enabled()
        if self._books:
            self.lbl_status.setText(f"Znaleziono książek: {len(self._books)}")
        else:
            self.lbl_status.setText(
                "Nie znaleziono książek (mp3 z parą .cue) w tym folderze."
            )

    def _update_out_label(self):
        if self._out_root is None:
            self.lbl_out.setText("—")
            return
        self.lbl_out.setText(
            f"{self._out_root}   (każda książka → podfolder z tytułem)"
        )
        self.lbl_out.setStyleSheet("")

    # ---- book list / chapter table --------------------------------------

    def _populate_books(self):
        self.list_books.blockSignals(True)
        self.list_books.clear()
        for book in self._books:
            item = QListWidgetItem(book.label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list_books.addItem(item)
        self.list_books.blockSignals(False)
        if self._books:
            self.list_books.setCurrentRow(0)  # fires _show_chapters
        else:
            self.table.setRowCount(0)
            self.lbl_info.setText("")

    def _show_chapters(self, row):
        self.table.setRowCount(0)
        if row < 0 or row >= len(self._books):
            self.lbl_info.setText("")
            return
        book = self._books[row]
        chapters = book.cue.chapters
        durations = chapter_durations(chapters, book.total_ms)
        self.table.setRowCount(len(chapters))
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for r, (ch, dur) in enumerate(zip(chapters, durations)):
            self._set_cell(r, 0, str(ch.number), right)
            self._set_cell(r, 1, ch.title or "")
            self._set_cell(r, 2, fmt_hms(ch.start_ms), right)
            self._set_cell(r, 3, fmt_hms(dur), right)

        if book.bitrate_kbps:
            bitrate = f"{book.bitrate_kbps} kbps (wykryty)"
        else:
            bitrate = "nieznany (użyję fallbacku 128k)"
        warn = (
            ""
            if book.total_ms is not None
            else "    ⚠ brak ffprobe — długość ostatniego rozdziału nieznana"
        )
        self.lbl_info.setText(f"Bitrate: {bitrate}{warn}")

    def _set_cell(self, r, c, text, align=Qt.AlignmentFlag.AlignVCenter):
        item = QTableWidgetItem(text)
        item.setTextAlignment(align)
        self.table.setItem(r, c, item)

    # ---- start / progress -----------------------------------------------

    def _checked_books(self):
        checked = []
        for i in range(self.list_books.count()):
            if self.list_books.item(i).checkState() == Qt.CheckState.Checked:
                checked.append(self._books[i])
        return checked

    def _refresh_start_enabled(self, *args):
        running = self._thread is not None
        self.btn_start.setEnabled(not running and bool(self._checked_books()))

    def _set_running(self, running):
        for w in (self.btn_folder, self.btn_mp3, self.btn_out, self.list_books):
            w.setEnabled(not running)
        if running:
            self.btn_start.setEnabled(False)

    def _start(self):
        books = self._checked_books()
        if not books or self._out_root is None:
            return
        jobs = [(b, self._out_root / safe_name(b.album)) for b in books]

        self._thread = QThread(self)
        self._worker = SplitWorker(jobs)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.book_started.connect(self._on_book_started)
        self._worker.chapter_done.connect(self._on_chapter_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_thread_done)

        self._set_running(True)
        self.progress.setRange(0, 0)  # busy until the first chapter completes
        self.lbl_status.setText("Start…")
        self._thread.start()

    @Slot(int, int, str)
    def _on_book_started(self, index, total, album):
        self.progress.setRange(0, 0)
        if total > 1:
            self.lbl_status.setText(f"Książka {index}/{total}: {album}")
        else:
            self.lbl_status.setText(album)

    @Slot(int, int)
    def _on_chapter_done(self, done, ch_total):
        if self.progress.maximum() != ch_total:
            self.progress.setRange(0, ch_total)
        self.progress.setValue(done)

    @Slot(list)
    def _on_finished(self, results):
        done = [r for r in results if r[1]]
        failed = [r for r in results if not r[1]]
        if not failed:
            self.lbl_status.setText(
                f"Gotowe: {len(done)}/{len(results)} książek pocięte."
            )
            QMessageBox.information(
                self,
                "Gotowe",
                "Pocięto książek: {}.\n\n{}".format(
                    len(done), "\n".join(f"✓ {a}" for a, _, _ in done)
                ),
            )
            return

        self.lbl_status.setText(
            f"Zakończono: {len(done)} OK, {len(failed)} z błędem."
        )
        lines = [f"✓ {a}" for a, _, _ in done]
        lines += [f"✗ {a}\n    {detail}" for a, _, detail in failed]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Zakończono z błędami")
        box.setText(f"Pocięto: {len(done)}.   Błędy: {len(failed)}.")
        box.setDetailedText("\n".join(lines))
        box.exec()

    @Slot()
    def _on_thread_done(self):
        self._thread = None
        self._worker = None
        self._set_running(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._refresh_start_enabled()