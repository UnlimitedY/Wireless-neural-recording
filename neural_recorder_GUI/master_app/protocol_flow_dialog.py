from typing import Any, Dict, List

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:
    from ..services.protocol_flow import (
        build_block,
        default_protocol_flow_state,
        normalize_condition,
        normalize_protocol_flow_state,
    )
except ImportError:
    from services.protocol_flow import (
        build_block,
        default_protocol_flow_state,
        normalize_condition,
        normalize_protocol_flow_state,
    )


class ProtocolFlowDialog(QDialog):
    def __init__(self, status: Dict[str, Any], command_sender, parent=None):
        super().__init__(parent)
        self.status = dict(status or {})
        self.command_sender = command_sender
        habits = dict(self.status.get("habits", {}) or {})
        self.flow_state = normalize_protocol_flow_state(habits.get("protocol_flow") or default_protocol_flow_state())
        self._dirty = False
        self._force_refresh_on_next_status = False
        self._loading_flow = False
        self._loading_editor = False
        self._pending_saved_signature = None
        self.setWindowTitle("DBRuleSwitch Protocol Flow")
        self.resize(760, 560)
        self._build_ui()
        self._load_flow()
        self._sync_editor_from_selection()

    def update_status(self, status: Dict[str, Any]) -> None:
        self.status = dict(status or {})
        habits = dict(self.status.get("habits", {}) or {})
        incoming_flow = normalize_protocol_flow_state(
            habits.get("protocol_flow") or self.flow_state
        )
        force_refresh = bool(self._force_refresh_on_next_status)
        self._force_refresh_on_next_status = False
        incoming_signature = self._flow_signature(incoming_flow)
        if force_refresh:
            self._pending_saved_signature = None
        elif self._pending_saved_signature is not None:
            if incoming_signature == self._pending_saved_signature:
                self._pending_saved_signature = None
                self._dirty = False
            else:
                self.state_label.setText(self._status_text())
                self._refresh_dirty_ui()
                return
        selected_id = ""
        selected = self._selected_block()
        if selected:
            selected_id = str(selected.get("block_id", "") or "")
        self.state_label.setText(self._status_text())
        if self._dirty and not force_refresh:
            self._refresh_dirty_ui()
            return
        self.flow_state = incoming_flow
        self._dirty = False
        self._load_flow()
        if selected_id:
            for row in range(self.flow_list.count()):
                block = dict(self.flow_list.item(row).data(Qt.ItemDataRole.UserRole) or {})
                if str(block.get("block_id", "") or "") == selected_id:
                    self.flow_list.setCurrentRow(row)
                    break
        self._sync_editor_from_selection()
        self._refresh_dirty_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        state_label = QLabel(self._status_text())
        state_label.setWordWrap(True)
        state_label.setObjectName("SectionDescription")
        self.state_label = state_label
        root.addWidget(state_label)

        palette_layout = QHBoxLayout()
        palette_layout.addWidget(QLabel("Protocols"))
        for protocol in range(11):
            button = QPushButton(f"P{protocol}")
            button.setFixedWidth(48)
            button.clicked.connect(lambda _checked=False, p=protocol: self._add_protocol(p))
            palette_layout.addWidget(button)
        palette_layout.addStretch(1)
        root.addLayout(palette_layout)

        body = QHBoxLayout()
        self.flow_list = QListWidget()
        self.flow_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.flow_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.flow_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.flow_list.currentRowChanged.connect(lambda _row: self._sync_editor_from_selection())
        self.flow_list.model().rowsMoved.connect(lambda *_args: self._mark_dirty())
        body.addWidget(self.flow_list, 2)

        editor_widget = QWidget()
        editor = QFormLayout(editor_widget)
        self.block_title = QLabel("Select a block")
        self.block_title.setWordWrap(True)
        editor.addRow(self.block_title)

        self.trial_min_spin = QSpinBox()
        self.trial_min_spin.setRange(0, 100000)
        self.trial_min_spin.setSuffix(" trials")
        self.trial_min_spin.valueChanged.connect(lambda _value: self._editor_value_changed())
        editor.addRow("Trial min", self.trial_min_spin)

        self.time_min_spin = QSpinBox()
        self.time_min_spin.setRange(0, 60 * 24 * 60)
        self.time_min_spin.setSuffix(" min")
        self.time_min_spin.valueChanged.connect(lambda _value: self._editor_value_changed())
        editor.addRow("Time min", self.time_min_spin)

        self.perf_mode_combo = QComboBox()
        self.perf_mode_combo.addItem("None", "none")
        self.perf_mode_combo.addItem("Side50", "side50")
        self.perf_mode_combo.addItem("Retention100", "retention100")
        self.perf_mode_combo.addItem("Protocol0 Time", "protocol0_time")
        self.perf_mode_combo.currentIndexChanged.connect(lambda _index: self._editor_value_changed())
        editor.addRow("Perf mode", self.perf_mode_combo)

        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(0, 100)
        self.threshold_spin.setSuffix("%")
        self.threshold_spin.valueChanged.connect(lambda _value: self._editor_value_changed())
        editor.addRow("Perf threshold", self.threshold_spin)

        self.window_size_spin = QSpinBox()
        self.window_size_spin.setRange(0, 1000)
        self.window_size_spin.valueChanged.connect(lambda _value: self._editor_value_changed())
        editor.addRow("Window size", self.window_size_spin)

        self.required_count_spin = QSpinBox()
        self.required_count_spin.setRange(1, 100)
        self.required_count_spin.valueChanged.connect(lambda _value: self._editor_value_changed())
        editor.addRow("Required count", self.required_count_spin)

        button_grid = QGridLayout()
        self.apply_conditions_btn = QPushButton("Apply Conditions")
        self.apply_conditions_btn.clicked.connect(self._apply_conditions_to_selected)
        self.remove_block_btn = QPushButton("Remove Future Block")
        self.remove_block_btn.clicked.connect(self._remove_selected)
        self.reset_btn = QPushButton("Reset Defaults")
        self.reset_btn.clicked.connect(self._reset_defaults)
        self.set_current_btn = QPushButton("Set Current Protocol")
        self.set_current_btn.clicked.connect(self._set_current_protocol)
        button_grid.addWidget(self.apply_conditions_btn, 0, 0)
        button_grid.addWidget(self.remove_block_btn, 0, 1)
        button_grid.addWidget(self.set_current_btn, 1, 0)
        button_grid.addWidget(self.reset_btn, 1, 1)
        editor.addRow(button_grid)
        body.addWidget(editor_widget, 1)
        root.addLayout(body)

        footer = QHBoxLayout()
        self.update_state_btn = QPushButton("Update Current State")
        self.update_state_btn.clicked.connect(self._request_update_state)
        self.save_btn = QPushButton("Save Flow")
        self.save_btn.clicked.connect(self._save_flow)
        footer.addWidget(self.update_state_btn)
        footer.addStretch(1)
        footer.addWidget(self.save_btn)
        root.addLayout(footer)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _status_text(self) -> str:
        habits = dict(self.status.get("habits", {}) or {})
        flow = dict(habits.get("protocol_flow", {}) or {})
        progress = dict(flow.get("firmware_progress", {}) or {})
        trial_target = progress.get("trial_target", "-")
        ready_text = "ready" if bool(progress.get("host_ready", False)) else "not ready"
        if getattr(self, "_pending_saved_signature", None) is not None:
            dirty_text = " | Save pending"
        elif bool(getattr(self, "_dirty", False)):
            dirty_text = " | Unsaved edits"
        else:
            dirty_text = ""
        return (
            f"Current P{int(habits.get('protocol', 0) or 0)} | "
            f"Trials {int(habits.get('protocol_trials', 0) or 0)}/{trial_target} | "
            f"Current block {flow.get('current_block_id', '') or '-'} | "
            f"Firmware condition {ready_text}"
            f"{dirty_text}"
        )

    def _load_flow(self) -> None:
        self._loading_flow = True
        try:
            self.flow_list.clear()
            for block in list(self.flow_state.get("blocks", []) or []):
                self._append_block_item(block)
            if self.flow_list.count() > 0:
                self.flow_list.setCurrentRow(0)
        finally:
            self._loading_flow = False

    def _append_block_item(self, block: Dict[str, Any]) -> None:
        item = QListWidgetItem(self._block_label(block))
        item.setData(Qt.ItemDataRole.UserRole, dict(block))
        current_id = str(self.flow_state.get("current_block_id", "") or "")
        block_id = str(block.get("block_id", "") or "")
        flags = item.flags() | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if not bool(block.get("fixed", False)) and block_id != current_id:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        else:
            flags &= ~Qt.ItemFlag.ItemIsDragEnabled
        item.setFlags(flags)
        if bool(block.get("fixed", False)):
            item.setForeground(QBrush(QColor("#6b7280")))
            item.setBackground(QBrush(QColor("#f3f4f6")))
            item.setToolTip("Completed fixed block. Order and conditions are locked.")
        elif block_id == current_id:
            item.setBackground(QBrush(QColor("#e0f2fe")))
            item.setToolTip("Current running block. Conditions can be edited and saved.")
        else:
            item.setToolTip("Future block. It can be reordered or edited before it starts.")
        self.flow_list.addItem(item)

    def _block_label(self, block: Dict[str, Any]) -> str:
        block_id = str(block.get("block_id", "") or "")
        current_id = str(self.flow_state.get("current_block_id", "") or "")
        if bool(block.get("fixed", False)):
            state = "DONE/FIXED"
        elif block_id == current_id:
            state = "CURRENT"
        else:
            state = "FUTURE"
        condition = normalize_condition(int(block.get("protocol", 0) or 0), block.get("conditions"))
        return (
            f"[{state}] {block_id}: P{block.get('protocol', 0)} "
            f"[{condition['perf_mode']}, trials >= {condition['trial_min']}, "
            f"time >= {int(condition.get('time_min_sec', 0) or 0) // 60} min]"
        )

    def _flow_signature(self, state: Dict[str, Any]):
        normalized = normalize_protocol_flow_state(state)
        blocks = []
        for block in list(normalized.get("blocks", []) or []):
            protocol = int(block.get("protocol", 0) or 0)
            condition = normalize_condition(protocol, block.get("conditions"))
            blocks.append((
                str(block.get("block_id", "") or ""),
                protocol,
                bool(block.get("fixed", False)),
                int(condition.get("trial_min", 0) or 0),
                int(condition.get("time_min_sec", 0) or 0),
                str(condition.get("perf_mode", "none") or "none"),
                int(condition.get("threshold", 0) or 0),
                int(condition.get("window_size", 0) or 0),
                int(condition.get("required_count", 1) or 1),
            ))
        return (str(normalized.get("current_block_id", "") or ""), tuple(blocks))

    def _refresh_dirty_ui(self) -> None:
        if hasattr(self, "save_btn"):
            if self._pending_saved_signature is not None:
                self.save_btn.setText("Save Flow...")
            else:
                self.save_btn.setText("Save Flow*" if self._dirty else "Save Flow")
        if hasattr(self, "state_label"):
            self.state_label.setText(self._status_text())

    def _mark_dirty(self) -> None:
        if self._loading_flow:
            return
        self._dirty = True
        self._refresh_dirty_ui()

    def _collect_flow_state(self) -> Dict[str, Any]:
        blocks: List[Dict[str, Any]] = []
        for row in range(self.flow_list.count()):
            block = dict(self.flow_list.item(row).data(Qt.ItemDataRole.UserRole) or {})
            blocks.append(block)
        state = dict(self.flow_state)
        state["blocks"] = blocks
        current_id = str(self.flow_state.get("current_block_id", "") or "")
        if current_id not in {str(block.get("block_id", "")) for block in blocks} and blocks:
            current_id = str(blocks[0].get("block_id", ""))
        state["current_block_id"] = current_id
        return normalize_protocol_flow_state(state)

    def _selected_block(self) -> Dict[str, Any]:
        item = self.flow_list.currentItem()
        return dict(item.data(Qt.ItemDataRole.UserRole) or {}) if item else {}

    def _sync_editor_from_selection(self) -> None:
        self._loading_editor = True
        block = self._selected_block()
        try:
            enabled = bool(block)
            fixed = bool(block.get("fixed", False))
            current = str(block.get("block_id", "") or "") == str(self.flow_state.get("current_block_id", "") or "")
            protocol = int(block.get("protocol", 0) or 0)
            condition = normalize_condition(protocol, block.get("conditions"))
            if fixed:
                state_text = "fixed completed"
            elif current:
                state_text = "current running"
            else:
                state_text = "future"
            self.block_title.setText(f"{block.get('block_id', '-')}: Protocol {protocol} ({state_text})")
            self.trial_min_spin.setValue(int(condition["trial_min"]))
            self.time_min_spin.setValue(int(condition.get("time_min_sec", 0) or 0) // 60)
            mode_index = max(0, self.perf_mode_combo.findData(condition["perf_mode"]))
            self.perf_mode_combo.setCurrentIndex(mode_index)
            self.threshold_spin.setValue(int(condition["threshold"]))
            self.window_size_spin.setValue(int(condition["window_size"]))
            self.required_count_spin.setValue(int(condition["required_count"]))
            for widget in (
                self.trial_min_spin,
                self.time_min_spin,
                self.perf_mode_combo,
                self.threshold_spin,
                self.window_size_spin,
                self.required_count_spin,
                self.apply_conditions_btn,
            ):
                widget.setEnabled(enabled and not fixed)
            self.remove_block_btn.setEnabled(enabled and not fixed and not current)
        finally:
            self._loading_editor = False

    def _apply_conditions_to_selected(self, mark_dirty: bool = True) -> None:
        item = self.flow_list.currentItem()
        if item is None:
            return
        block = dict(item.data(Qt.ItemDataRole.UserRole) or {})
        if bool(block.get("fixed", False)):
            return
        protocol = int(block.get("protocol", 0) or 0)
        block["conditions"] = normalize_condition(protocol, {
            "trial_min": self.trial_min_spin.value(),
            "time_min_sec": self.time_min_spin.value() * 60,
            "perf_mode": self.perf_mode_combo.currentData(),
            "threshold": self.threshold_spin.value(),
            "window_size": self.window_size_spin.value(),
            "required_count": self.required_count_spin.value(),
        })
        item.setData(Qt.ItemDataRole.UserRole, block)
        item.setText(self._block_label(block))
        if mark_dirty:
            self._mark_dirty()

    def _editor_value_changed(self) -> None:
        if self._loading_editor or self._loading_flow:
            return
        self._apply_conditions_to_selected(mark_dirty=True)

    def _add_protocol(self, protocol: int) -> None:
        self.flow_state = self._collect_flow_state()
        block = build_block(len(self.flow_state.get("blocks", []) or []), int(protocol))
        existing_ids = {str(item.get("block_id", "")) for item in self.flow_state.get("blocks", []) or []}
        base_id = str(block["block_id"])
        suffix = 1
        while block["block_id"] in existing_ids:
            block["block_id"] = f"{base_id}_{suffix}"
            suffix += 1
        self._append_block_item(block)
        self.flow_list.setCurrentRow(self.flow_list.count() - 1)
        self._mark_dirty()

    def _remove_selected(self) -> None:
        item = self.flow_list.currentItem()
        if item is None:
            return
        block = dict(item.data(Qt.ItemDataRole.UserRole) or {})
        if bool(block.get("fixed", False)):
            return
        self.flow_list.takeItem(self.flow_list.row(item))
        self._sync_editor_from_selection()
        self._mark_dirty()

    def _reset_defaults(self) -> None:
        fixed_blocks = []
        for row in range(self.flow_list.count()):
            block = dict(self.flow_list.item(row).data(Qt.ItemDataRole.UserRole) or {})
            if bool(block.get("fixed", False)):
                fixed_blocks.append(block)
        default_state = default_protocol_flow_state()
        default_state["blocks"] = fixed_blocks + [
            block for block in default_state["blocks"]
            if str(block.get("block_id", "")) not in {str(item.get("block_id", "")) for item in fixed_blocks}
        ]
        self.flow_state = normalize_protocol_flow_state(default_state)
        self._load_flow()
        self._mark_dirty()

    def _save_flow(self) -> None:
        self._apply_conditions_to_selected()
        self.flow_state = self._collect_flow_state()
        self._pending_saved_signature = self._flow_signature(self.flow_state)
        self.command_sender("protocol_flow_update", flow=self.flow_state)
        self._dirty = False
        self._refresh_dirty_ui()

    def _set_current_protocol(self) -> None:
        block = self._selected_block()
        if not block:
            return
        self.command_sender("protocol_flow_set_current", protocol=int(block.get("protocol", 0) or 0))

    def _request_update_state(self) -> None:
        self._dirty = False
        self._pending_saved_signature = None
        self._force_refresh_on_next_status = True
        self._refresh_dirty_ui()
        self.command_sender("read_all")
