"""Metadata editor tab (sprint 7).

Lists every .mp3 in a chosen folder (non-recursive) and edits the five managed
ID3 frames over them -- one file at a time, or many at once. The actual
read/edit/cover work lives in core.metadata; this tab is just the window onto
it, the same way tab_split is a window onto core.splitter.

Multi-file editing follows the mp3tag model: select several files and a field
that's identical across them shows that value, while a field that differs shows
a "⟨różne⟩" hint and stays blank. Type into a field and the new value lands on
every selected file; leave it untouched and each file keeps its own. Nothing
hits disk until "Zapisz" -- except cover art, which is a separate concern in
core.metadata, so load/remove is applied to the selection immediately. Because
update_tags only ever touches the managed frames, a shared edit of the album
never disturbs per-file titles, nor year/genre/comments/cover.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.metadata import (
    AudioMetadata,
    detect_mime,
    read_cover,
    read_tags,
    remove_cover,
    set_cover,
    update_tags,
)

# Shown in a field when the selected files disagree on its value.
_MIXED = "⟨różne⟩"

# Editor field order: (model attribute, label). Mirrors core.metadata._MANAGED.
_FIELDS = (
    ("album", "Album (tytuł książki)"),
    ("album_artist", "Album artist (autor)"),
    ("artist", "Artysta (lektor)"),
    ("title", "Tytuł ścieżki"),
    ("track", "Nr ścieżki"),
)

# Table columns that mirror a per-file field, so an edit can refresh them.
_TITLE_COL = 1
_TRACK_COL = 2


# ---------------------------------------------------------------------------
# Pure helpers (no Qt) -- trivially testable.
# ---------------------------------------------------------------------------

def shared_value(values) -> str | None:
    """The common value across a selection, or None if they differ.

    None means "mixed": the UI shows the ⟨różne⟩ hint and leaves the field
    blank. An empty selection is also None (nothing to show).
    """
    seen = list(values)
    if not seen:
        return None
    first = seen[0]
    return first if all(v == first for v in seen) else None


def text_tuple(meta: AudioMetadata) -> tuple:
    """The five editable fields as a tuple, for a cheap dirty check.

    Cover state is left out on purpose: covers are written immediately, so
    they never count as a pending text change.
    """
    return (meta.album, meta.album_artist, meta.artist, meta.title, meta.track)


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------

class MetadataTab(QWidget):
    """Folder-wide ID3 metadata editor."""

    def __init__(self):
        super().__init__()
        self._paths: list[Path] = []                     # files in display order
        self._working: dict[Path, AudioMetadata] = {}    # edited copy
        self._original: dict[Path, AudioMetadata] = {}   # last-saved baseline
        self._fields: dict[str, QLineEdit] = {}
        self._suppress_selection = False                 # mute signal on reload
        self._build_ui()
        self._on_selection_changed()
        self._refresh_save_enabled()

    # ---- construction ----------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Folder selection
        src_row = QHBoxLayout()
        self.btn_folder = QPushButton("Wskaż folder…")
        self.btn_folder.clicked.connect(self._pick_folder)
        self.lbl_source = QLabel("Nie wybrano folderu.")
        self.lbl_source.setStyleSheet("color: gray;")
        src_row.addWidget(self.btn_folder)
        src_row.addWidget(self.lbl_source, 1)
        root.addLayout(src_row)

        # Master/detail: file list over the editor panel
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Plik", "Tytuł", "Nr"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.verticalHeader().setVisible(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        splitter.addWidget(self.table)

        splitter.addWidget(self._build_editor())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([320, 220])
        root.addWidget(splitter, 1)

        # Selection drives the editor; connect once both panes exist.
        self.table.selectionModel().selectionChanged.connect(
            self._on_selection_changed
        )

        # Save + status
        bottom = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: gray;")
        self.btn_save = QPushButton("Zapisz")
        self.btn_save.clicked.connect(self._on_save)
        bottom.addWidget(self.lbl_status, 1)
        bottom.addWidget(self.btn_save)
        root.addLayout(bottom)

    def _build_editor(self) -> QWidget:
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)

        # Text fields. textEdited (not textChanged) fires only on real typing,
        # so programmatic setText during selection never loops back as an edit.
        form = QFormLayout()
        for attr, label in _FIELDS:
            edit = QLineEdit()
            edit.setClearButtonEnabled(True)
            edit.textEdited.connect(partial(self._on_field_edited, attr))
            self._fields[attr] = edit
            form.addRow(label, edit)
        row.addLayout(form, 1)

        # Cover panel
        cover_box = QVBoxLayout()
        self.lbl_cover = QLabel("—")
        self.lbl_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_cover.setFixedSize(160, 160)
        self.lbl_cover.setStyleSheet("border: 1px solid palette(mid); color: gray;")
        self.btn_cover_load = QPushButton("Wczytaj okładkę…")
        self.btn_cover_load.clicked.connect(self._load_cover)
        self.btn_cover_remove = QPushButton("Usuń okładkę")
        self.btn_cover_remove.clicked.connect(self._remove_cover_selected)
        cover_box.addWidget(self.lbl_cover)
        cover_box.addWidget(self.btn_cover_load)
        cover_box.addWidget(self.btn_cover_remove)
        cover_box.addStretch(1)
        row.addLayout(cover_box)

        return panel

    # ---- folder loading --------------------------------------------------

    def _pick_folder(self):
        if self._dirty_paths() and not self._confirm_discard():
            return
        folder = QFileDialog.getExistingDirectory(self, "Wybierz folder z MP3")
        if folder:
            self._load_folder(Path(folder))

    def _confirm_discard(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Niezapisane zmiany",
            "Masz niezapisane zmiany. Odrzucić je i wczytać nowy folder?",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _load_folder(self, folder: Path):
        self.lbl_source.setText(str(folder))
        self.lbl_source.setStyleSheet("")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            paths = sorted(
                p for p in folder.iterdir() if p.suffix.lower() == ".mp3"
            )
            self._paths = paths
            self._working = {p: read_tags(p) for p in paths}
            # independent copies: editing _working must not touch the baseline
            self._original = {p: replace(self._working[p]) for p in paths}
        finally:
            QApplication.restoreOverrideCursor()
        self._populate_table()
        self._refresh_save_enabled()
        if paths:
            self.lbl_status.setText(
                f"Wczytano plików: {len(paths)}.  "
                "Zaznacz, aby edytować (Ctrl+A = wszystkie)."
            )
        else:
            self.lbl_status.setText("Brak plików MP3 w tym folderze.")

    def _populate_table(self):
        self._suppress_selection = True  # clearing rows would fire selection
        self.table.clearSelection()
        self.table.setRowCount(len(self._paths))
        for r, p in enumerate(self._paths):
            meta = self._working[p]
            self._set_cell(r, 0, p.name)
            self._set_cell(r, _TITLE_COL, meta.title)
            self._set_cell(r, _TRACK_COL, meta.track)
        self._suppress_selection = False
        self._on_selection_changed()

    def _set_cell(self, r, c, text):
        self.table.setItem(r, c, QTableWidgetItem(text))

    # ---- selection -> editor --------------------------------------------

    def _selected_paths(self) -> list[Path]:
        rows = sorted(i.row() for i in self.table.selectionModel().selectedRows())
        return [self._paths[r] for r in rows]

    def _on_selection_changed(self, *args):
        if self._suppress_selection:
            return
        paths = self._selected_paths()
        has = bool(paths)
        for attr, edit in self._fields.items():
            edit.setEnabled(has)
            if not has:
                edit.clear()
                edit.setPlaceholderText("")
                continue
            value = shared_value([getattr(self._working[p], attr) for p in paths])
            if value is None:
                edit.clear()
                edit.setPlaceholderText(_MIXED)
            else:
                edit.setPlaceholderText("")
                edit.setText(value)
        self.btn_cover_load.setEnabled(has)
        self.btn_cover_remove.setEnabled(has)
        self._update_cover_preview(paths)

    def _on_field_edited(self, attr: str, text: str):
        paths = self._selected_paths()
        for p in paths:
            setattr(self._working[p], attr, text)
        if attr in ("title", "track"):  # keep the visible columns in step
            self._refresh_rows(paths)
        self._refresh_save_enabled()

    def _refresh_rows(self, paths):
        index = {p: r for r, p in enumerate(self._paths)}
        for p in paths:
            r = index.get(p)
            if r is None:
                continue
            meta = self._working[p]
            self.table.item(r, _TITLE_COL).setText(meta.title)
            self.table.item(r, _TRACK_COL).setText(meta.track)

    # ---- cover (applied to the selection immediately) -------------------

    def _load_cover(self):
        if not self._selected_paths():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Wybierz okładkę", "", "Obrazy (*.png *.jpg *.jpeg)"
        )
        if path:
            self._apply_cover_bytes(Path(path).read_bytes())

    def _apply_cover_bytes(self, data: bytes):
        """Embed `data` as the cover on every selected file, now (not on save)."""
        paths = self._selected_paths()
        if not paths:
            return
        mime = detect_mime(data)
        if mime is None:
            QMessageBox.warning(
                self, "Niewłaściwy plik", "To nie jest obraz PNG ani JPEG."
            )
            return
        failed = self._for_each(paths, lambda p: self._set_cover_on(p, data, mime))
        self._update_cover_preview(paths)
        self._finish_cover(f"Osadzono okładkę: {len(paths) - len(failed)}.", failed)

    def _set_cover_on(self, p: Path, data: bytes, mime: str):
        set_cover(p, data, mime)
        self._working[p].has_cover = True
        self._working[p].cover_mime = mime

    def _remove_cover_selected(self):
        paths = self._selected_paths()
        if not paths:
            return
        failed = self._for_each(paths, self._remove_cover_on)
        self._update_cover_preview(paths)
        self._finish_cover(f"Usunięto okładkę: {len(paths) - len(failed)}.", failed)

    def _remove_cover_on(self, p: Path):
        remove_cover(p)
        self._working[p].has_cover = False
        self._working[p].cover_mime = None

    def _finish_cover(self, success_msg: str, failed):
        if failed:
            self.lbl_status.setText(f"{success_msg}  (błędów: {len(failed)})")
            QMessageBox.warning(
                self,
                "Część plików się nie udała",
                "\n".join(f"✗ {p.name}: {exc}" for p, exc in failed),
            )
        else:
            self.lbl_status.setText(success_msg)

    def _update_cover_preview(self, paths):
        self.lbl_cover.clear()
        if not paths:
            self.lbl_cover.setText("—")
            return
        if len(paths) > 1:  # don't load every cover just to preview
            self.lbl_cover.setText(f"Zaznaczono {len(paths)} plików")
            return
        cover = read_cover(paths[0])
        if cover is None:
            self.lbl_cover.setText("brak okładki")
            return
        pix = QPixmap()
        if pix.loadFromData(cover[0]) and not pix.isNull():
            self.lbl_cover.setPixmap(
                pix.scaled(
                    self.lbl_cover.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.lbl_cover.setText("okładka osadzona")

    # ---- save ------------------------------------------------------------

    def _dirty_paths(self) -> list[Path]:
        return [
            p for p in self._paths
            if text_tuple(self._working[p]) != text_tuple(self._original[p])
        ]

    def _refresh_save_enabled(self):
        self.btn_save.setEnabled(bool(self._dirty_paths()))

    def _on_save(self):
        dirty = self._dirty_paths()
        if not dirty:
            return
        saved = 0

        def write(p):
            nonlocal saved
            update_tags(p, self._working[p])
            self._original[p] = replace(self._working[p])  # this file is clean now
            saved += 1

        failed = self._for_each(dirty, write)
        self._refresh_save_enabled()
        if failed:
            self.lbl_status.setText(f"Zapisano {saved}, błędów {len(failed)}.")
            QMessageBox.warning(
                self,
                "Część plików się nie zapisała",
                "\n".join(f"✗ {p.name}: {exc}" for p, exc in failed),
            )
        else:
            self.lbl_status.setText(f"Zapisano: {saved}.")

    # ---- shared plumbing -------------------------------------------------

    def _for_each(self, paths, action):
        """Run `action(path)` over paths under a wait cursor, collecting
        (path, exception) for any that blow up. Returns the failure list so
        text edits and cover edits share one continue-on-error loop."""
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        failed = []
        try:
            for p in paths:
                try:
                    action(p)
                except Exception as exc:  # permissions, odd file, bad image
                    failed.append((p, exc))
        finally:
            QApplication.restoreOverrideCursor()
        return failed