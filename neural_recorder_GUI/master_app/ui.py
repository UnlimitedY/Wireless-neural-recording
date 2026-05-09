from dataclasses import dataclass
from html import escape
from typing import Any, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

try:
    from ..services.health_metrics import decode_bq25176_stat, normalize_bq25176_stat
except ImportError:
    from services.health_metrics import decode_bq25176_stat, normalize_bq25176_stat


MASTER_CONSOLE_STYLESHEET = """
QMainWindow {
    background: #f3f5f7;
    color: #111827;
    font-family: "SF Pro Text", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}

QWidget#MasterConsoleRoot {
    background: #f3f5f7;
}

QScrollArea {
    border: none;
    background: transparent;
}

QFrame#SlotCard {
    background: #ffffff;
    border: 1px solid #d7dde4;
    border-radius: 16px;
}

QFrame#SectionCard {
    background: #fafbfc;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
}

QFrame#MetricTile {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
}

QFrame#MetricTile[tone="ok"] {
    border-color: #c9d8cd;
}

QFrame#MetricTile[tone="warning"] {
    border-color: #ddd1b0;
}

QFrame#MetricTile[tone="danger"] {
    border-color: #e1c0c0;
}

QLabel#CardTitle {
    color: #111827;
    font-size: 19px;
    font-weight: 700;
}

QLabel#CardSubtitle {
    color: #6b7280;
    font-size: 10px;
}

QLabel#MouseBadgeCaption {
    color: #6b7280;
    font-size: 10px;
    font-weight: 600;
}

QLabel#MouseBadgeValue {
    color: #111827;
    background: #f3f4f6;
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 13px;
    font-weight: 700;
}

QLabel#SectionTitle {
    color: #111827;
    font-size: 12px;
    font-weight: 700;
}

QLabel#SectionDescription {
    color: #6b7280;
    font-size: 10px;
}

QLabel#MetricCaption {
    color: #6b7280;
    font-size: 10px;
    font-weight: 600;
}

QLabel#MetricValue {
    color: #111827;
    font-size: 12px;
    font-weight: 600;
}

QLabel#StatusPill {
    color: #374151;
    background: #f3f4f6;
    border: 1px solid #d1d5db;
    border-radius: 999px;
    padding: 5px 10px;
    font-size: 10px;
    font-weight: 700;
}

QLabel#StatusPill[tone="ok"] {
    color: #166534;
    background: #f0fdf4;
    border-color: #cfe9d7;
}

QLabel#StatusPill[tone="warning"] {
    color: #92400e;
    background: #fffbeb;
    border-color: #ead8ac;
}

QLabel#StatusPill[tone="danger"] {
    color: #991b1b;
    background: #fef2f2;
    border-color: #ebc3c3;
}

QLabel#StatusPill[tone="neutral"] {
    color: #4b5563;
    background: #f3f4f6;
    border-color: #d1d5db;
}

QLabel#StatusChip {
    color: #374151;
    background: #f3f4f6;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    padding: 6px 8px;
    font-size: 10px;
    font-weight: 700;
}

QLabel#StatusChip[tone="ok"] {
    color: #166534;
    background: #f0fdf4;
    border-color: #cfe9d7;
}

QLabel#StatusChip[tone="warning"] {
    color: #92400e;
    background: #fffbeb;
    border-color: #ead8ac;
}

QLabel#StatusChip[tone="danger"] {
    color: #991b1b;
    background: #fef2f2;
    border-color: #ebc3c3;
}

QToolButton#SectionToggle {
    min-height: 26px;
    border: none;
    background: transparent;
    color: #111827;
    padding: 0;
    font-size: 12px;
    font-weight: 700;
    text-align: left;
}

QToolButton#SectionToggle:hover {
    background: transparent;
    color: #374151;
}

QLabel#InlineLabel {
    color: #4b5563;
    font-size: 10px;
    font-weight: 600;
}

QLabel#ErrorBanner {
    color: #991b1b;
    background: #fef2f2;
    border: 1px solid #ebc3c3;
    border-radius: 10px;
    padding: 7px 10px;
    font-size: 10px;
    font-weight: 600;
}

QPushButton,
QToolButton {
    min-height: 32px;
    border-radius: 8px;
    padding: 6px 10px;
    border: 1px solid #d1d5db;
    background: #ffffff;
    color: #111827;
    font-weight: 600;
}

QPushButton:hover,
QToolButton:hover {
    background: #f9fafb;
}

QPushButton:pressed,
QToolButton:pressed {
    background: #f3f4f6;
}

QPushButton[tone="accent"],
QToolButton[tone="accent"] {
    background: #111827;
    border-color: #111827;
    color: white;
}

QPushButton:hover[tone="accent"],
QToolButton:hover[tone="accent"] {
    background: #1f2937;
}

QPushButton:checked[tone="accent"],
QToolButton:checked[tone="accent"] {
    background: #374151;
    border-color: #374151;
}

QPushButton[tone="danger"],
QToolButton[tone="danger"] {
    background: #991b1b;
    border-color: #991b1b;
    color: white;
}

QPushButton:hover[tone="danger"],
QToolButton:hover[tone="danger"] {
    background: #7f1d1d;
}

QPushButton[tone="success"],
QToolButton[tone="success"] {
    background: #166534;
    border-color: #166534;
    color: white;
}

QPushButton:hover[tone="success"],
QToolButton:hover[tone="success"] {
    background: #14532d;
}

QPushButton[tone="warning"],
QToolButton[tone="warning"] {
    background: #b45309;
    border-color: #b45309;
    color: white;
}

QPushButton:hover[tone="warning"],
QToolButton:hover[tone="warning"] {
    background: #92400e;
}

QPushButton[tone="ghost"],
QToolButton[tone="ghost"] {
    background: #ffffff;
    border-color: #d1d5db;
    color: #374151;
}

QPushButton:hover[tone="ghost"],
QToolButton:hover[tone="ghost"] {
    background: #f9fafb;
}

QPushButton:disabled,
QToolButton:disabled {
    background: #f3f4f6;
    color: #9ca3af;
    border-color: #e5e7eb;
}

QComboBox,
QSpinBox,
QDateEdit,
QLineEdit {
    min-height: 34px;
    border-radius: 8px;
    border: 1px solid #d1d5db;
    background: #ffffff;
    padding: 0 10px;
    color: #111827;
}

QComboBox:focus,
QSpinBox:focus,
QDateEdit:focus,
QLineEdit:focus {
    border: 1px solid #9ca3af;
}

QCheckBox {
    color: #374151;
    font-weight: 600;
    spacing: 8px;
}

QTextBrowser {
    background: #ffffff;
    color: #111827;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    padding: 8px;
}

QTextBrowser a {
    color: #1d4ed8;
}

QProgressBar {
    min-height: 20px;
    background: #ffffff;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    text-align: center;
    color: #374151;
}

QProgressBar::chunk {
    background: #374151;
    border-radius: 7px;
}
"""


def repolish(widget: QWidget) -> None:
    style = widget.style()
    if style is None:
        return
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_tone(widget: QWidget, tone: str) -> None:
    if widget.property("tone") == tone:
        return
    widget.setProperty("tone", tone)
    repolish(widget)


def coerce_voltage_mv(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        if isinstance(value, str):
            text = value.strip().lower()
            if not text:
                return 0.0
            if text.endswith("mv"):
                return float(text[:-2])
            if text.endswith("v"):
                return float(text[:-1]) * 1000.0
            numeric = float(text)
        else:
            numeric = float(value)
    except Exception:
        return 0.0
    if abs(numeric) <= 20.0:
        return numeric * 1000.0
    return numeric


def format_voltage_text(value: Any) -> str:
    voltage_mv = coerce_voltage_mv(value)
    if voltage_mv <= 0:
        return "-- V"
    return f"{voltage_mv / 1000.0:.2f} V"


def normalize_battery_stat(value: Any) -> Optional[int]:
    return normalize_bq25176_stat(value)


def battery_charge_text_and_state(value: Any) -> Tuple[str, str]:
    status = decode_bq25176_stat(value)
    if status is None:
        return ("Charge --", "neutral")
    return (status.charge_state_text, status.charge_state_level)


def battery_power_text_and_state(value: Any) -> Tuple[str, str]:
    status = decode_bq25176_stat(value)
    if status is None:
        return ("Power --", "neutral")
    return (status.power_state_text, status.power_state_level)


@dataclass(frozen=True)
class BatteryDisplayStatus:
    charge_text: str
    charge_state: str
    power_text: str
    power_state: str
    tooltip: str
    raw_stat: Optional[int]
    pending_power_fault_samples: int = 0


class BatteryStatusSmoother:
    """Format decoded BQ25176 display status while preserving the old API."""

    def __init__(self, confirm_samples: int = 3):
        self.confirm_samples = max(1, int(confirm_samples))
        self._contradictory_count = 0

    def update(self, value: Any) -> BatteryDisplayStatus:
        stat = normalize_battery_stat(value)
        charge_text, charge_state = battery_charge_text_and_state(stat)
        power_text, power_state = battery_power_text_and_state(stat)
        self._contradictory_count = 0
        tooltip = self._tooltip_for(stat, charge_text, power_text)
        return BatteryDisplayStatus(
            charge_text=charge_text,
            charge_state=charge_state,
            power_text=power_text,
            power_state=power_state,
            tooltip=tooltip,
            raw_stat=stat,
            pending_power_fault_samples=self._contradictory_count,
        )

    @staticmethod
    def _tooltip_for(stat: Optional[int], charge_text: str, power_text: str) -> str:
        if stat is None:
            return "Battery charging/power status has not been reported yet."
        status = decode_bq25176_stat(stat)
        if status is None:
            return f"BQ25176 stat={stat}: {charge_text}; {power_text}."
        return (
            f"BQ25176 stat={status.raw_stat}: PG={status.pg_raw}, "
            f"STAT={status.stat_raw}; {status.status_text}; {charge_text}; {power_text}."
        )


STATUS_OK_COLOR = "#166534"
STATUS_WARNING_COLOR = "#92400E"
STATUS_DANGER_COLOR = "#B91C1C"
STATUS_NEUTRAL_COLOR = "#374151"


def format_status_text(value: Any, state: Optional[str] = None) -> str:
    normalized = str(state or "neutral").strip().lower()
    if normalized == "ok":
        color = STATUS_OK_COLOR
        weight = 700
    elif normalized == "warning":
        color = STATUS_WARNING_COLOR
        weight = 700
    elif normalized in {"danger", "error", "alert"}:
        color = STATUS_DANGER_COLOR
        weight = 700
    else:
        color = STATUS_NEUTRAL_COLOR
        weight = 600
    return (
        f'<span style="color: {color}; font-weight: {weight};">'
        f"{escape(str(value))}</span>"
    )


def format_run_duration(run_time_min: Any) -> str:
    try:
        total_minutes = max(0, int(round(float(run_time_min or 0.0))))
    except Exception:
        total_minutes = 0
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_on_off_text(enabled: Any, on_text: str = "ON", off_text: str = "OFF") -> str:
    return format_status_text(on_text if bool(enabled) else off_text, "ok" if bool(enabled) else "danger")


def join_status_fragments(*fragments: Any, separator: str = "  |  ") -> str:
    return separator.join(str(fragment) for fragment in fragments if fragment is not None)


class MetricTile(QFrame):
    def __init__(self, caption: str, value: str = "--"):
        super().__init__()
        self.setObjectName("MetricTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(3)

        self.caption_label = QLabel(caption)
        self.caption_label.setObjectName("MetricCaption")
        layout.addWidget(self.caption_label)

        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.value_label.setWordWrap(True)
        self.value_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        normalized = str(value)
        if self.value_label.text() == normalized:
            return
        self.value_label.setText(normalized)

    def set_tone(self, tone: str) -> None:
        if self.property("tone") == tone:
            return
        set_tone(self, tone)


class SectionCard(QFrame):
    def __init__(self, title: str, description: str = ""):
        super().__init__()
        self.setObjectName("SectionCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("SectionTitle")
        header_layout.addWidget(self.title_label)

        if description:
            self.description_label = QLabel(description)
            self.description_label.setObjectName("SectionDescription")
            self.description_label.setWordWrap(True)
            header_layout.addWidget(self.description_label)
        else:
            self.description_label = None

        root.addLayout(header_layout)

        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        root.addLayout(self.body_layout)
