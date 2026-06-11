from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class MetadataTab(QWidget):
    """Metadata editor tab. Placeholder until sprint 7."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        label = QLabel("Edytor metadanych — wkrótce")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
