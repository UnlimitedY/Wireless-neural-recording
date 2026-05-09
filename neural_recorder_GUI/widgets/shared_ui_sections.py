from __future__ import annotations

from collections.abc import Mapping as MappingABC
from datetime import datetime
from typing import Iterable, Mapping, Optional

from PyQt6.QtWidgets import QTextBrowser


CONTROL_SURFACE_STYLESHEET = (
    "QFrame { border-radius: 8px; background-color: #FFFFFF; border: 1px solid #E2E8F0; }"
)

LOG_LEVEL_COLORS = {
    "info": "#F8FAFC",
    "warning": "#FDE68A",
    "error": "#FCA5A5",
    "critical": "#FB7185",
    "success": "#86EFAC",
}


def format_log_html(
    message: str,
    level: str = "info",
    timestamp: Optional[str] = None,
) -> str:
    resolved_timestamp = str(timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    color = LOG_LEVEL_COLORS.get(str(level or "info").strip().lower(), "#F8FAFC")
    return (
        f'<span style="color:#94A3B8;">[{resolved_timestamp}]</span> '
        f'<span style="color:{color};">{message}</span>'
    )


class CompactSystemLogView(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self.setMinimumHeight(110)
        self.setStyleSheet(
            """
            QTextBrowser {
                background-color: #0F172A;
                color: #E2E8F0;
                border: 1px solid #1E293B;
                border-radius: 8px;
                padding: 6px;
                font-family: Consolas, Menlo, Monaco, monospace;
                font-size: 11px;
            }
            """
        )

    def set_entries(self, entries: Iterable[Mapping[str, object]]) -> None:
        html_lines = []
        for item in entries:
            if not isinstance(item, MappingABC):
                continue
            html_lines.append(
                format_log_html(
                    str(item.get("message", "") or ""),
                    level=str(item.get("level", "info") or "info"),
                    timestamp=str(item.get("timestamp", "") or ""),
                )
            )
        self.setHtml("<br/>".join(html_lines) if html_lines else "")
        self.moveCursor(self.textCursor().MoveOperation.End)
