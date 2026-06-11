import sys

from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget

from ui.tab_metadata import MetadataTab
from ui.tab_split import SplitTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AudioTool")
        self.resize(900, 600)

        tabs = QTabWidget()
        tabs.addTab(MetadataTab(), "Edytor metadanych")
        tabs.addTab(SplitTab(), "Dzielenie audiobooka")
        self.setCentralWidget(tabs)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
