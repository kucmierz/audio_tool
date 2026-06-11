from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class SplitTab(QWidget):
    """Audiobook splitting tab. Placeholder until sprint 4."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        label = QLabel("Dzielenie audiobooka — wkrótce")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
