from typing import Any, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    from .ui import set_tone
except ImportError:
    from master_app.ui import set_tone


def set_text_if_changed(widget, text: Any) -> None:
    normalized = str(text)
    try:
        if widget.text() == normalized:
            return
    except Exception:
        pass
    widget.setText(normalized)


def set_enabled_if_changed(widget, enabled: bool) -> None:
    enabled = bool(enabled)
    try:
        if widget.isEnabled() == enabled:
            return
    except Exception:
        pass
    widget.setEnabled(enabled)


def set_checked_if_changed(widget, checked: bool) -> None:
    checked = bool(checked)
    try:
        if widget.isChecked() == checked:
            return
    except Exception:
        pass
    widget.setChecked(checked)


def set_tone_if_changed(widget: QWidget, tone: str) -> None:
    normalized = str(tone or "neutral")
    try:
        if widget.property("tone") == normalized:
            return
    except Exception:
        pass
    set_tone(widget, normalized)


class CompactActionBar(QWidget):
    """Dense two-column action area for the frequently used controls."""

    def __init__(self, columns: int = 2):
        super().__init__()
        self.columns = max(1, int(columns or 1))
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)

    def add_action(self, widget: QWidget, row: int, column: int, column_span: int = 1) -> None:
        self.grid.addWidget(widget, int(row), int(column), 1, max(1, int(column_span or 1)))


class CollapsibleSectionCard(QFrame):
    """Section card that keeps low-frequency controls out of the default view."""

    def __init__(self, title: str, description: str = "", expanded: bool = True):
        super().__init__()
        self.setObjectName("SectionCard")
        self._title = str(title)
        self._expanded = bool(expanded)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("SectionToggle")
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(self._expanded)
        self.toggle_button.clicked.connect(self.set_expanded)
        header.addWidget(self.toggle_button, 1)
        root.addLayout(header)

        if description:
            self.description_label = QLabel(description)
            self.description_label.setObjectName("SectionDescription")
            self.description_label.setWordWrap(True)
            root.addWidget(self.description_label)
        else:
            self.description_label = None

        self.body_widget = QWidget()
        self.body_layout = QVBoxLayout(self.body_widget)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        root.addWidget(self.body_widget)
        self.set_expanded(self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.toggle_button.setChecked(self._expanded)
        self.toggle_button.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )
        self.toggle_button.setText(self._title)
        self.body_widget.setVisible(self._expanded)
        if self.description_label is not None:
            self.description_label.setVisible(self._expanded)


class StatusChipGrid(QWidget):
    """Small labeled chips for dense, diff-rendered status summaries."""

    def __init__(self, columns: int = 3):
        super().__init__()
        self.columns = max(1, int(columns or 1))
        self._chips: Dict[str, QLabel] = {}
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(6)

    def set_chip(self, key: str, text: Any, tone: str = "neutral", tooltip: Optional[str] = None) -> QLabel:
        normalized_key = str(key)
        chip = self._chips.get(normalized_key)
        if chip is None:
            chip = QLabel()
            chip.setObjectName("StatusChip")
            chip.setTextFormat(Qt.TextFormat.RichText)
            chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            index = len(self._chips)
            self.grid.addWidget(chip, index // self.columns, index % self.columns)
            self._chips[normalized_key] = chip
        set_text_if_changed(chip, text)
        set_tone_if_changed(chip, tone)
        if tooltip is not None and chip.toolTip() != str(tooltip):
            chip.setToolTip(str(tooltip))
        return chip
