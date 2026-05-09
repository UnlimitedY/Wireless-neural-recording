import os
import pickle
import sys
import time
from datetime import datetime
from multiprocessing import shared_memory
from typing import Any, Dict, List, Optional

from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout
from PyQt6.QtCore import QDate, QTimer, Qt

try:
    from .contracts import make_slot_command
    from .runtime_config import detail_frame_interval_ms, load_runtime_config
    from .ui import format_run_duration
    from ..storage.runtime_cache import SlotRuntimeCache, read_json
    from ..neural_recorder_GUI import ESBMainWindow
    from ..support.windows_runtime import apply_windows_process_role, configure_qt_runtime
except ImportError:
    from master_app.contracts import make_slot_command
    from master_app.runtime_config import detail_frame_interval_ms, load_runtime_config
    from master_app.ui import format_run_duration
    from storage.runtime_cache import SlotRuntimeCache, read_json
    from recorder_app.window import ESBMainWindow
    from support.windows_runtime import apply_windows_process_role, configure_qt_runtime


class DetailMainWindow(ESBMainWindow):
    def __init__(self, slot_payload: Dict[str, Any], runtime_root: str, command_queue=None, local_service=None):
        self.slot_payload = dict(slot_payload)
        self.command_queue = command_queue
        self.local_service = local_service
        self._closing_from_service = False
        self.runtime_cache = SlotRuntimeCache(runtime_root, str(slot_payload["slot_id"]))
        self.profile = read_json(str(slot_payload["profile_path"]), {})
        self.runtime_config = load_runtime_config(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.detail_runtime_config = dict(self.runtime_config.get("detail_view", {}))
        if self.local_service is None:
            apply_windows_process_role("detail_view")
        self._last_detail_sequence = -1
        self._last_habits_mtime = 0.0
        self._last_habits_event_mtime = 0.0
        self._last_habits_state_mtime = 0.0
        self._last_mode_switch_mtime = 0.0
        self._last_habits_cache_seq = -1
        self._last_habits_live_file_sequence = -1
        self._last_habits_live_sequence = 0
        self._habits_history_loaded = False
        self._last_impedance_history_mtime = 0.0
        self._last_impedance_result_seq = -1
        self._last_remote_cap_summary = ""
        self._last_remote_cap_history_signature = None
        self._last_detail_render_monotonic_by_stream: Dict[str, float] = {}
        self._channel_selection_dirty = {
            "mode1": False,
            "mode3": False,
            "mode2": False,
        }
        self._habits_control_dirty = {
            "reward": False,
            "light": False,
            "protocol": False,
            "inter_block": False,
        }
        self._pending_habits_values: Dict[str, Any] = {}
        self._pending_mode1_channel: Optional[int] = None
        self._pending_mode3_channel: Optional[int] = None
        self._pending_mode2_channels: Optional[List[int]] = None
        self._pending_remote_values: Dict[str, Any] = {}
        self._remote_control_dirty: Dict[str, bool] = {
            "spike_filter_enabled": False,
            "spike_filter_low_cut_hz": False,
            "spike_filter_high_cut_hz": False,
            "spike_filter_sample_rate_hz": False,
            "rc_series_resistor_kohm": False,
            "rc_shunt_cap_pf": False,
        }
        self._remote_programmatic_control_keys = set()
        self._skip_local_hardware_enumeration = True
        super().__init__()
        if self.local_service is not None:
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.impedance_history_path = self.runtime_cache.impedance_history_path
        self.setWindowTitle(f"{self.slot_payload['label']} Detail Viewer")
        self.curr_active_ports = str(self.profile.get("main_serial_port", "")) or "remote-cage"
        self._detail_render_min_interval_ms = 33
        self._detail_payload_max_frames_per_poll = max(
            1,
            int(self.detail_runtime_config.get("detail_payload_max_frames_per_poll", 1) or 1),
        )
        try:
            self.plot_min_render_interval_s = 1.0 / 30.0
        except Exception:
            pass
        self._last_detail_render_monotonic = 0.0
        self._configure_remote_view_mode()
        self._load_profile_labels()
        self._refresh_impedance_history_if_needed(force=True)
        self._install_remote_habits_proxy()
        self._refresh_habits_cache_if_needed(force=True)
        self._wire_remote_commands()
        self._wire_remote_local_state_bridge()
        self._attach_detail_stream()

        self.status_poll_timer = QTimer(self)
        self.status_poll_timer.timeout.connect(self._poll_status)
        self.status_poll_timer.start(int(self.detail_runtime_config.get("status_poll_ms", 700)))

        self.detail_poll_timer = None
        if self.local_service is None:
            self.detail_poll_timer = QTimer(self)
            self.detail_poll_timer.timeout.connect(self._poll_detail_payload)
            render_interval_ms = detail_frame_interval_ms(self.runtime_config)
            self._detail_render_min_interval_ms = int(render_interval_ms)
            configured_poll_ms = int(self.detail_runtime_config.get("detail_payload_poll_ms", 60))
            poll_ms = configured_poll_ms
            if render_interval_ms > 0:
                poll_ms = min(configured_poll_ms, max(1, int(round(render_interval_ms / 2.0))))
            self.detail_poll_timer.start(max(1, int(poll_ms)))

        self.habits_poll_timer = QTimer(self)
        self.habits_poll_timer.timeout.connect(self._refresh_habits_live_if_needed)
        habits_poll_ms = int(self.detail_runtime_config.get("habits_cache_poll_ms", 2000))
        if self.local_service is not None:
            habits_poll_ms = int(self.detail_runtime_config.get("local_habits_cache_poll_ms", 10000))
        self.habits_poll_timer.start(habits_poll_ms)

        self._spike_filter_push_timer = QTimer(self)
        self._spike_filter_push_timer.setSingleShot(True)
        self._spike_filter_push_timer.timeout.connect(self._push_spike_filter_config)

        self._impedance_compensation_push_timer = QTimer(self)
        self._impedance_compensation_push_timer.setSingleShot(True)
        self._impedance_compensation_push_timer.timeout.connect(self._push_impedance_compensation_config)

    def _configure_remote_view_mode(self) -> None:
        self._stop_remote_local_timers()
        for widget_name in (
            "main_serial_control_bar",
            "camera_control_bar",
            "sampling_control_bar",
            "rf_control_bar",
            "top_controls_separator",
            "log_group",
            "timeline_group",
        ):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.hide()
        self.statusBar().hide()
        self._hide_detail_save_controls()
        self._install_detail_display_controls()
        if hasattr(self, "habits_tab") and hasattr(self.habits_tab, "habits_panel"):
            self.habits_tab.habits_panel.set_remote_view_mode(True)
        self.plot_update_toggle_button.setChecked(True)
        self.on_plot_update_toggle_clicked(True)
        self._enable_remote_local_controls()
        self.statusBar().showMessage("Remote detail viewer attached")

    def _install_detail_display_controls(self) -> None:
        try:
            central = self.centralWidget()
            main_layout = central.layout() if central is not None else None
        except Exception:
            main_layout = None
        if main_layout is None:
            return
        try:
            bar = QFrame()
            bar.setFrameShape(QFrame.Shape.StyledPanel)
            layout = QHBoxLayout(bar)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setSpacing(8)
            for widget_name in ("plot_update_toggle_button", "display_tuning_toggle_button"):
                widget = getattr(self, widget_name, None)
                if widget is None:
                    continue
                try:
                    widget.setParent(bar)
                    widget.show()
                    layout.addWidget(widget)
                except Exception:
                    pass
            layout.addStretch(1)
            main_layout.insertWidget(0, bar)
            self.detail_display_control_bar = bar
        except Exception:
            pass

    def _stop_remote_local_timers(self) -> None:
        """Stop timers that belong to the original standalone GUI shell.

        The local chart is display/control-only; leaving old local hardware
        health timers or watchdog threads alive after attach/close creates
        periodic work that competes with chart rendering and can keep a closed
        chart object alive.
        """
        for timer_name in (
            "neural_serial_health_timer",
            "rf_serial_health_timer",
            "RF_timer",
            "mode3_trial_trigger_guard_timer",
            "charging_guard_timer",
            "video_segment_timer",
            "camera_health_timer",
            "watchdog_timer",
            "_battery_timeline_refresh_timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
        habits_panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        for timer_name in ("serial_health_timer", "update_timer"):
            timer = getattr(habits_panel, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
        watchdog = getattr(self, "watchdog", None)
        if watchdog is not None:
            try:
                watchdog.running = False
            except Exception:
                pass

    def _hide_layout_item(self, item) -> None:
        if item is None:
            return
        widget = item.widget()
        if widget is not None:
            try:
                widget.hide()
            except Exception:
                pass
            return
        layout = item.layout()
        if layout is not None:
            for index in range(layout.count()):
                self._hide_layout_item(layout.itemAt(index))

    def _hide_grid_rows(self, grid_layout, rows: List[int]) -> None:
        if grid_layout is None:
            return
        hidden_rows = set(int(row) for row in rows)
        try:
            item_count = int(grid_layout.count())
        except Exception:
            item_count = 0
        for index in range(item_count):
            try:
                row, _column, row_span, _column_span = grid_layout.getItemPosition(index)
            except Exception:
                continue
            occupied_rows = set(range(int(row), int(row) + max(1, int(row_span))))
            if occupied_rows & hidden_rows:
                self._hide_layout_item(grid_layout.itemAt(index))

    def _hide_detail_save_controls(self) -> None:
        try:
            self._hide_grid_rows(getattr(self.lfp_tab, "control_panel_layout", None), [0, 1])
        except Exception:
            pass
        try:
            self._hide_grid_rows(getattr(self.spike1ch_tab, "control_panel_layout", None), [0])
        except Exception:
            pass
        try:
            self._hide_grid_rows(getattr(self.spike4ch_tab, "control_panel_layout", None), [0])
        except Exception:
            pass
        widget_names = (
            "select_save_path_button",
            "save_path_label",
            "lfp_tab.lfp_select_file_button",
            "lfp_tab.lfp_file_path_label",
            "lfp_tab.start_save_button",
            "lfp_tab.stop_save_button",
            "lfp_tab.lfp_progress_bar",
            "lfp_tab.mode3_select_file_button",
            "lfp_tab.mode3_file_path_label",
            "lfp_tab.start_save_mode3_button",
            "lfp_tab.stop_save_mode3_button",
            "lfp_tab.mode3_progress_bar",
            "spike4ch_tab.select_file_button",
            "spike4ch_tab.file_path_label",
            "spike4ch_tab.start_save_button",
            "spike4ch_tab.stop_save_button",
            "spike4ch_tab.mode2_progress_bar",
            "spike1ch_tab.mode1_select_file_button",
            "spike1ch_tab.mode1_file_path_label",
            "spike1ch_tab.start_save_button",
            "spike1ch_tab.stop_save_button",
            "spike1ch_tab.mode1_progress_bar",
        )
        for path in widget_names:
            obj = self
            try:
                for name in path.split("."):
                    obj = getattr(obj, name)
                obj.hide()
            except Exception:
                pass

    def _enable_remote_local_controls(self) -> None:
        widget_names = (
            "lfp_tab.low_cutoff",
            "lfp_tab.high_cutoff",
            "lfp_tab.scale_combo",
            "lfp_tab.esa_scale_combo",
            "lfp_tab.mand_smooth_combo",
            "lfp_tab.update_scale_button",
            "spectrum_window_button",
            "spike1ch_tab.filter_enable_checkbox",
            "spike1ch_tab.low_cut_spin",
            "spike1ch_tab.high_cut_spin",
            "spike1ch_tab.sample_rate_combo",
            "spike1ch_tab.open_spectrum_button",
            "impedance_tab.display_toggle_button",
            "impedance_tab.history_view_combo",
            "impedance_tab.series_resistor_spin",
            "impedance_tab.shunt_cap_spin",
        )
        for path in widget_names:
            obj = self
            try:
                for name in path.split("."):
                    obj = getattr(obj, name)
                obj.setEnabled(True)
            except Exception:
                pass

    def _set_remote_control_programmatic(self, key: str, callback) -> None:
        normalized = str(key)
        self._remote_programmatic_control_keys.add(normalized)
        try:
            callback()
        finally:
            self._remote_programmatic_control_keys.discard(normalized)

    def _mark_remote_control_dirty(self, key: str) -> None:
        normalized = str(key)
        if normalized in self._remote_programmatic_control_keys:
            return
        self._remote_control_dirty[normalized] = True
        self._pending_remote_values.pop(normalized, None)

    def _resolve_remote_pending_value(self, key: str, reported_value):
        normalized = str(key)
        if normalized not in self._pending_remote_values:
            return reported_value
        pending_value = self._pending_remote_values.get(normalized)
        if pending_value == reported_value:
            self._pending_remote_values.pop(normalized, None)
            self._remote_control_dirty[normalized] = False
            return reported_value
        return pending_value

    def _sync_remote_checkbox_control(self, key: str, widget, reported_value) -> None:
        normalized = str(key)
        target_value = bool(self._resolve_remote_pending_value(normalized, bool(reported_value)))
        current_value = bool(widget.isChecked())
        if self._remote_control_dirty.get(normalized, False):
            if current_value == bool(reported_value):
                self._remote_control_dirty[normalized] = False
            else:
                return
        if current_value == target_value:
            return
        self._set_remote_control_programmatic(
            normalized,
            lambda: widget.setChecked(bool(target_value)),
        )

    def _sync_remote_double_spin_control(self, key: str, widget, reported_value) -> None:
        normalized = str(key)
        try:
            normalized_reported = float(reported_value)
        except Exception:
            if normalized not in self._pending_remote_values:
                return
            normalized_reported = None
        target_value = self._resolve_remote_pending_value(normalized, normalized_reported)
        try:
            current_value = float(widget.value())
        except Exception:
            current_value = None
        editing = self._widget_is_user_editing(widget)
        if self._remote_control_dirty.get(normalized, False):
            if (
                normalized_reported is not None
                and current_value is not None
                and abs(current_value - normalized_reported) < 1e-6
                and not editing
            ):
                self._remote_control_dirty[normalized] = False
            else:
                return
        if editing or target_value is None or current_value is None or abs(current_value - float(target_value)) < 1e-6:
            return
        self._set_remote_control_programmatic(
            normalized,
            lambda: widget.setValue(float(target_value)),
        )

    def _sync_remote_combo_control(self, key: str, combo, reported_value) -> None:
        normalized = str(key)
        target_value = self._resolve_remote_pending_value(normalized, reported_value)
        current_value = combo.currentData()
        editing = self._combo_is_user_editing(combo)
        if self._remote_control_dirty.get(normalized, False):
            if current_value == reported_value and not editing:
                self._remote_control_dirty[normalized] = False
            else:
                return
        if editing or current_value == target_value:
            return
        self._set_remote_control_programmatic(
            normalized,
            lambda: self._set_combo_value(combo, target_value),
        )

    @staticmethod
    def _widget_is_user_editing(widget) -> bool:
        try:
            if widget.hasFocus():
                return True
        except Exception:
            pass
        try:
            line_edit = widget.lineEdit()
            if line_edit is not None and line_edit.hasFocus():
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _set_combo_value(combo, value) -> None:
        try:
            index = combo.findData(value)
        except Exception:
            index = -1
        if index < 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _wire_remote_local_state_bridge(self) -> None:
        try:
            self.spike1ch_tab.sample_rate_combo.setItemData(0, 12500)
            self.spike1ch_tab.sample_rate_combo.setItemData(1, 20000)
        except Exception:
            pass
        try:
            self.spike1ch_tab.filter_enable_checkbox.toggled.connect(self._on_spike_filter_control_changed)
            self.spike1ch_tab.low_cut_spin.valueChanged.connect(self._on_spike_filter_control_changed)
            self.spike1ch_tab.high_cut_spin.valueChanged.connect(self._on_spike_filter_control_changed)
            self.spike1ch_tab.sample_rate_combo.currentIndexChanged.connect(self._on_spike_filter_control_changed)
        except Exception:
            pass
        try:
            self.impedance_tab.series_resistor_spin.valueChanged.connect(self._on_impedance_compensation_control_changed)
            self.impedance_tab.shunt_cap_spin.valueChanged.connect(self._on_impedance_compensation_control_changed)
        except Exception:
            pass

    def _on_spike_filter_control_changed(self, *_args) -> None:
        if self._remote_programmatic_control_keys:
            return
        for key in (
            "spike_filter_enabled",
            "spike_filter_low_cut_hz",
            "spike_filter_high_cut_hz",
            "spike_filter_sample_rate_hz",
        ):
            self._mark_remote_control_dirty(key)
        self._spike_filter_push_timer.start(120)

    def _on_impedance_compensation_control_changed(self, *_args) -> None:
        if self._remote_programmatic_control_keys:
            return
        self._mark_remote_control_dirty("rc_series_resistor_kohm")
        self._mark_remote_control_dirty("rc_shunt_cap_pf")
        self._impedance_compensation_push_timer.start(120)

    def _push_spike_filter_config(self) -> None:
        try:
            sample_rate_hz = int(self.spike1ch_tab.sample_rate_combo.currentData() or self.spike1ch_tab.sample_rate_combo.currentText().split()[0])
        except Exception:
            sample_rate_hz = 20000
        enabled = bool(self.spike1ch_tab.filter_enable_checkbox.isChecked())
        low_cut_hz = float(self.spike1ch_tab.low_cut_spin.value())
        high_cut_hz = float(self.spike1ch_tab.high_cut_spin.value())
        self._pending_remote_values["spike_filter_enabled"] = enabled
        self._pending_remote_values["spike_filter_low_cut_hz"] = low_cut_hz
        self._pending_remote_values["spike_filter_high_cut_hz"] = high_cut_hz
        self._pending_remote_values["spike_filter_sample_rate_hz"] = sample_rate_hz
        self._send_remote(
            make_slot_command(
                "set_spike_filter_config",
                enabled=enabled,
                low_cut_hz=low_cut_hz,
                high_cut_hz=high_cut_hz,
                sample_rate_hz=sample_rate_hz,
            )
        )

    def _push_impedance_compensation_config(self) -> None:
        series_resistor_kohm = float(self.impedance_tab.series_resistor_spin.value())
        shunt_cap_pf = float(self.impedance_tab.shunt_cap_spin.value())
        self._pending_remote_values["rc_series_resistor_kohm"] = series_resistor_kohm
        self._pending_remote_values["rc_shunt_cap_pf"] = shunt_cap_pf
        self._send_remote(
            make_slot_command(
                "set_impedance_compensation",
                series_resistor_kohm=series_resistor_kohm,
                shunt_cap_pf=shunt_cap_pf,
            )
        )

    def _load_profile_labels(self) -> None:
        try:
            profile_payload = dict(self.profile)
            profile_payload["auto_connect_main_serial"] = False
            profile_payload["auto_connect_rf"] = False
            profile_payload["auto_connect_habits"] = False
            self.apply_profile_configuration(
                profile_payload,
                auto_connect_default=False,
                log_success=False,
            )
        except Exception:
            pass

    def _install_remote_habits_proxy(self) -> None:
        panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        if panel is None:
            return
        panel.send_serial_command = self._send_remote_habits_serial_command
        self._install_remote_habits_edit_guards(panel)
        if hasattr(panel, "set_remote_sd_download_handler"):
            panel.set_remote_sd_download_handler(self._request_remote_sd_download)

    def _request_remote_sd_download(self, directory: str) -> bool:
        target_dir = str(directory or "").strip()
        if not target_dir:
            return False
        self._send_remote(make_slot_command("habits_sd_download", directory=target_dir))
        return True

    def _send_remote_habits_serial_command(self, raw_command: str) -> bool:
        command = self._translate_remote_habits_command(raw_command)
        panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        if command is None:
            if panel is not None and hasattr(panel, "add_message"):
                panel.add_message(f"Remote detail viewer does not support command: {raw_command}")
            return False
        self._capture_remote_habits_pending(raw_command)
        self._send_remote(command)
        return True

    def _install_remote_habits_edit_guards(self, panel) -> None:
        edit_groups = {
            "reward": (
                getattr(panel, "reward_left_edit", None),
                getattr(panel, "reward_middle_edit", None),
                getattr(panel, "reward_right_edit", None),
            ),
            "light": (
                getattr(panel, "low_light_edit", None),
                getattr(panel, "high_light_edit", None),
            ),
        }
        for key, widgets in edit_groups.items():
            for widget in widgets:
                if widget is None:
                    continue
                try:
                    widget.valueChanged.connect(lambda _value, control_key=key: self._mark_habits_dirty(control_key))
                except Exception:
                    pass
        for key, widget_name in (
            ("protocol", "protocol_edit"),
            ("inter_block", "inter_block_interval_edit"),
        ):
            widget = getattr(panel, widget_name, None)
            if widget is None:
                continue
            try:
                widget.valueChanged.connect(lambda _value, control_key=key: self._mark_habits_dirty(control_key))
            except Exception:
                pass

    def _mark_habits_dirty(self, key: str) -> None:
        normalized = str(key)
        self._habits_control_dirty[normalized] = True
        self._pending_habits_values.pop(normalized, None)

    @staticmethod
    def _widget_is_user_editing(widget) -> bool:
        if widget is None:
            return False
        try:
            if widget.hasFocus():
                return True
        except Exception:
            pass
        try:
            line_edit = widget.lineEdit()
            if line_edit is not None and line_edit.hasFocus():
                return True
        except Exception:
            pass
        return False

    def _resolve_habits_pending_value(self, key: str, reported_value):
        normalized = str(key)
        if normalized not in self._pending_habits_values:
            return reported_value
        pending_value = self._pending_habits_values.get(normalized)
        if pending_value == reported_value:
            self._pending_habits_values.pop(normalized, None)
            self._habits_control_dirty[normalized] = False
            return reported_value
        return pending_value

    def _capture_remote_habits_pending(self, raw_command: str) -> None:
        panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        if panel is None:
            return
        command = str(raw_command or "").strip()
        if not command:
            return
        if command.startswith("R"):
            self._pending_habits_values["reward"] = (
                int(panel.reward_left_edit.value()),
                int(panel.reward_middle_edit.value()),
                int(panel.reward_right_edit.value()),
            )
            self._habits_control_dirty["reward"] = False
            return
        if command.startswith("L"):
            self._pending_habits_values["light"] = (
                int(panel.low_light_edit.value()),
                int(panel.high_light_edit.value()),
            )
            self._habits_control_dirty["light"] = False
            return
        if command.startswith("Z"):
            self._pending_habits_values["protocol"] = int(panel.protocol_edit.value())
            self._habits_control_dirty["protocol"] = False
            return
        if command == "B":
            self._pending_habits_values["protocol"] = 5
            self._habits_control_dirty["protocol"] = False
            return
        if command == "G":
            self._pending_habits_values["protocol"] = 0
            self._habits_control_dirty["protocol"] = False
            return
        if command.startswith("I"):
            self._pending_habits_values["inter_block"] = int(panel.inter_block_interval_edit.value())
            self._habits_control_dirty["inter_block"] = False

    def _sync_habits_spin_widget(self, key: str, widget, value: Any) -> None:
        if widget is None:
            return
        normalized = str(key)
        try:
            reported_value = int(value)
        except Exception:
            if normalized not in self._pending_habits_values:
                return
            reported_value = None
        target_value = self._resolve_habits_pending_value(normalized, reported_value)
        current_value = int(widget.value())
        editing = self._widget_is_user_editing(widget)
        if self._habits_control_dirty.get(normalized, False):
            if reported_value is not None and current_value == reported_value and not editing:
                self._habits_control_dirty[normalized] = False
            else:
                return
        if editing or target_value is None or current_value == int(target_value):
            return
        widget.blockSignals(True)
        widget.setValue(int(target_value))
        widget.blockSignals(False)

    def _sync_habits_spin_group(self, key: str, controls_and_values) -> None:
        normalized = str(key)
        widgets = [widget for widget, _value in controls_and_values if widget is not None]
        if not widgets:
            return
        try:
            reported_values = tuple(int(value) for _widget, value in controls_and_values)
        except Exception:
            if normalized not in self._pending_habits_values:
                return
            reported_values = None
        target_values = self._resolve_habits_pending_value(normalized, reported_values)
        current_values = tuple(int(widget.value()) for widget in widgets)
        editing = any(self._widget_is_user_editing(widget) for widget in widgets)
        if self._habits_control_dirty.get(normalized, False):
            if reported_values is not None and current_values == reported_values and not editing:
                self._habits_control_dirty[normalized] = False
            else:
                return
        if editing or target_values is None:
            return
        normalized_target = tuple(int(value) for value in target_values)
        if current_values == normalized_target:
            return
        for widget, value in zip(widgets, normalized_target):
            widget.blockSignals(True)
            widget.setValue(int(value))
            widget.blockSignals(False)

    def _sync_habits_date_widget(self, widget, date_text: str) -> None:
        if widget is None:
            return
        normalized = str(date_text or "").strip()
        if not normalized or self._widget_is_user_editing(widget):
            return
        current_text = widget.date().toString("yyyy-MM-dd")
        if current_text == normalized:
            return
        parsed = QDate.fromString(normalized, "yyyy-MM-dd")
        if not parsed.isValid():
            parsed = QDate.fromString(normalized)
        if not parsed.isValid():
            return
        widget.blockSignals(True)
        widget.setDate(parsed)
        widget.blockSignals(False)

    def _translate_remote_habits_command(self, raw_command: str) -> Optional[Dict[str, Any]]:
        command = str(raw_command or "").strip()
        if not command:
            return None
        if command.startswith("R"):
            parts = [part.strip() for part in command[1:].split(",") if part.strip()]
            if len(parts) >= 3:
                return make_slot_command(
                    "habits_serial",
                    action="reward",
                    left=int(parts[0]),
                    right=int(parts[1]),
                    middle=int(parts[2]),
                )
            return None
        if command.startswith("L"):
            parts = [part.strip() for part in command[1:].split(",") if part.strip()]
            if len(parts) >= 2:
                return make_slot_command(
                    "habits_serial",
                    action="light",
                    low=int(parts[0]),
                    high=int(parts[1]),
                )
            return None
        if command.startswith("Z"):
            payload = command[1:].replace(",", "").strip()
            if payload:
                protocol = int(payload)
                if protocol < 0 or protocol > 10:
                    return None
                return make_slot_command("habits_serial", action="protocol", protocol=protocol)
            return None
        if command.startswith("I"):
            payload = command[1:].replace(",", "").strip()
            if payload:
                interval_ms = int(payload)
                return make_slot_command(
                    "habits_serial",
                    action="inter_block_interval",
                    minutes=max(0, int(round(interval_ms / 60000.0))),
                )
            return None
        if command.startswith("T"):
            return make_slot_command("habits_serial", action="time_sync")
        if command.startswith("W"):
            payload = command[1:].strip().strip(",")
            parts = [part.strip() for part in payload.split(",") if part.strip()]
            duration_ms = int(parts[0]) if parts else 0
            fan_enabled = bool(int(parts[1])) if len(parts) > 1 else True
            return make_slot_command(
                "habits_serial",
                action="charging_warning",
                duration_ms=duration_ms,
                fan_enabled=fan_enabled,
            )
        if command == "A":
            return make_slot_command("habits_serial", action="read_all")
        if command == "B":
            return None
        if command == "C":
            return make_slot_command("habits_serial", action="cap_snapshot")
        if command == "F":
            return make_slot_command("habits_serial", action="rf_off_ready")
        if command == "G":
            return make_slot_command("habits_serial", action="post_training_protocol0")
        if command == "H":
            return make_slot_command("habits_serial", action="handshake")
        if command in {"P", "M"}:
            return make_slot_command("habits_serial", action="pause_toggle")
        return None

    @staticmethod
    def _format_habits_trial_type(trial_type: Any) -> str:
        try:
            normalized = int(trial_type)
        except Exception:
            normalized = 0
        type_str = "Left" if normalized == 1 else "Right" if normalized == 2 else "Middle" if normalized == 3 else "Unknown"
        return f"{normalized} ({type_str})" if normalized > 0 else "-"

    @staticmethod
    def _format_habits_outcome(outcome_code: Any, outcome_text: Any) -> str:
        try:
            normalized = int(outcome_code)
        except Exception:
            normalized = -1
        label = str(outcome_text or "-").strip() or "-"
        return f"{normalized} ({label})" if normalized >= 0 else label

    @staticmethod
    def _effective_habits_protocol(habits: Dict[str, Any]) -> Any:
        if not isinstance(habits, dict):
            return 0
        return habits.get("protocol", habits.get("last_protocol", 0))

    def _sync_habits_status_information(self, panel, habits: Dict[str, Any]) -> None:
        try:
            start_date_text = str(habits.get("start_date", "") or "").strip()
            start_date_qdate = QDate.fromString(start_date_text, "yyyy-MM-dd") if start_date_text else panel.start_date_edit.date()
            if not start_date_qdate.isValid():
                start_date_qdate = panel.start_date_edit.date()
            if start_date_qdate.isValid():
                days = (datetime.now().date() - start_date_qdate.toPyDate()).days
                panel.days_label.setText(f"{float(days):.1f} d")
        except Exception:
            pass
        try:
            current_trial = int(habits.get("current_trial", 0) or 0)
        except Exception:
            current_trial = 0
        try:
            performance = float(habits.get("performance", 0.0) or 0.0)
        except Exception:
            performance = 0.0
        try:
            trials_today = int(habits.get("trials_today", 0) or 0)
        except Exception:
            trials_today = 0
        try:
            protocol_trials = int(habits.get("protocol_trials", 0) or 0)
        except Exception:
            protocol_trials = 0
        try:
            protocol_perf = float(habits.get("protocol_perf", 0.0) or 0.0)
        except Exception:
            protocol_perf = 0.0

        panel.trial_label.setText(f"{current_trial} - {performance:.1f}%")
        panel.trials_day_label.setText(f"{trials_today}/d")
        panel.protocol_trials_label.setText(str(protocol_trials))
        panel.trial_type_label.setText(self._format_habits_trial_type(habits.get("trial_type", 0)))
        panel.outcome_label.setText(
            self._format_habits_outcome(
                habits.get("outcome_code", -1),
                habits.get("outcome_text", "-"),
            )
        )
        panel.protocol_perf_label.setText(f"{protocol_perf:.1f}%")

    def _sync_habits_protocol_progress(self, panel, habits: Dict[str, Any]) -> None:
        raw_payload = str(habits.get("protocol_progress_raw", "") or "").strip()
        if not raw_payload.startswith("PG,"):
            return
        if getattr(panel, "_remote_protocol_progress_raw", "") == raw_payload:
            return
        try:
            panel.update_protocol_progress_display(raw_payload)
            panel._remote_protocol_progress_raw = raw_payload
        except Exception:
            pass

    def _sync_habits_panel_from_status(self, panel, habits: Dict[str, Any]) -> None:
        try:
            panel._set_serial_ui_connected(bool(habits.get("connected", False)))
        except Exception:
            pass
        try:
            self._sync_habits_spin_group(
                "reward",
                (
                    (getattr(panel, "reward_left_edit", None), habits.get("reward_left")),
                    (getattr(panel, "reward_middle_edit", None), habits.get("reward_middle")),
                    (getattr(panel, "reward_right_edit", None), habits.get("reward_right")),
                ),
            )
            self._sync_habits_spin_group(
                "light",
                (
                    (getattr(panel, "low_light_edit", None), habits.get("low_light")),
                    (getattr(panel, "high_light_edit", None), habits.get("high_light")),
                ),
            )
            self._sync_habits_spin_widget(
                "protocol",
                getattr(panel, "protocol_edit", None),
                self._effective_habits_protocol(habits),
            )
            self._sync_habits_spin_widget(
                "inter_block",
                getattr(panel, "inter_block_interval_edit", None),
                habits.get("inter_block_interval_minutes"),
            )
        except Exception:
            pass
        try:
            panel.sync_time_label.setText(str(habits.get("last_sync_text", "Not synced") or "Not synced"))
            panel.pause_btn.setText("Resume Habits" if habits.get("paused") else "Pause Habits")
            panel.cap_detect_label.setText(str(habits.get("cap_detect_text", "Disabled") or "Disabled"))
            panel.cap_baseline_label.setText(str(habits.get("cap_baseline_text", "- / -") or "- / -"))
            panel.cap_filtered_label.setText(str(habits.get("cap_filtered_text", "- / -") or "- / -"))
            panel.cap_delta_label.setText(str(habits.get("cap_delta_text", "- / -") or "- / -"))
            panel.cap_drift_label.setText(str(habits.get("cap_drift_text", "Unknown") or "Unknown"))
            drift_state = str(habits.get("cap_drift_state", "unknown") or "unknown").strip().lower()
            if panel.cap_detect_label.text() == "Enabled":
                panel.cap_detect_label.setStyleSheet("font-weight: bold; color: #2E7D32;")
            else:
                panel.cap_detect_label.setStyleSheet("font-weight: bold; color: #888;")
            if drift_state == "alarm":
                panel.cap_drift_label.setStyleSheet("font-weight: bold; color: #C62828;")
                panel.cap_delta_label.setStyleSheet("font-weight: bold; color: #C62828;")
            elif drift_state == "warning":
                panel.cap_drift_label.setStyleSheet("font-weight: bold; color: #EF6C00;")
                panel.cap_delta_label.setStyleSheet("font-weight: bold; color: #EF6C00;")
            elif drift_state == "normal":
                panel.cap_drift_label.setStyleSheet("font-weight: bold; color: #2E7D32;")
                panel.cap_delta_label.setStyleSheet("font-weight: bold; color: #2E7D32;")
            if hasattr(panel, "cap_drift_state"):
                panel.cap_drift_state = drift_state
            cap_summary = (
                f"{panel.cap_detect_label.text()} | "
                f"Baseline {panel.cap_baseline_label.text()} | "
                f"Delta {panel.cap_delta_label.text()} | "
                f"{panel.cap_drift_label.text()}"
            )
            if cap_summary != self._last_remote_cap_summary:
                self._last_remote_cap_summary = cap_summary
                if (
                    cap_summary.strip()
                    and cap_summary != "Disabled | Baseline - / - | Delta - / - | Unknown"
                    and hasattr(panel, "add_message")
                ):
                    panel.add_message(f"Cap baseline update: {cap_summary}")
        except Exception:
            pass
        try:
            cap_history = [dict(item) for item in list(habits.get("cap_history", []) or []) if isinstance(item, dict)]
            cap_signature = tuple(
                (
                    item.get("trial_num"),
                    item.get("timestamp"),
                    item.get("delta_left"),
                    item.get("delta_right"),
                    item.get("baseline_left"),
                    item.get("baseline_right"),
                    item.get("filtered_left"),
                    item.get("filtered_right"),
                    item.get("detect_enabled"),
                )
                for item in cap_history
            )
            if cap_signature != getattr(self, "_last_remote_cap_history_signature", None):
                self._last_remote_cap_history_signature = cap_signature
                panel.cap_history.clear()
                for item in cap_history:
                    panel.cap_history.append(dict(item))
                panel.update_cap_trial_chart()
        except Exception:
            pass
        try:
            self._sync_habits_status_information(panel, habits)
        except Exception:
            pass
        self._sync_habits_protocol_progress(panel, habits)
        connected = bool(habits.get("connected", False))
        for widget_name in (
            "reward_btn",
            "light_btn",
            "protocol_btn",
            "block_switch_mode_btn",
            "post_training_protocol0_btn",
            "inter_block_interval_btn",
            "cap_snapshot_btn",
            "pause_btn",
            "handshake_btn",
            "time_btn",
        ):
            widget = getattr(panel, widget_name, None)
            if widget is not None:
                widget.setEnabled(connected)
        read_all_btn = getattr(panel, "read_all_btn", None)
        if read_all_btn is not None:
            try:
                read_all_btn.setEnabled(connected)
                read_all_btn.setText("Read All Values")
                read_all_btn.setToolTip("")
            except Exception:
                pass
        if hasattr(panel, "download_sd_btn"):
            try:
                panel.apply_remote_sd_download_state(
                    connected=connected,
                    active=bool(habits.get("sd_download_active", False)),
                    status_text=str(habits.get("sd_download_status_text", "") or ""),
                    error_text=str(habits.get("sd_download_last_error", "") or ""),
                )
            except Exception:
                panel.download_sd_btn.setEnabled(connected and not bool(habits.get("sd_download_active", False)))

    def _attach_detail_stream(self) -> None:
        if self.local_service is not None:
            return
        try:
            self.command_queue.put(make_slot_command("detail_attach", attached=True), block=False)
        except Exception:
            pass

    def _send_remote(self, command: Dict[str, Any]) -> None:
        if self.local_service is not None:
            try:
                self.local_service.handle_chart_command(command)
            except Exception:
                pass
            return
        try:
            self.command_queue.put(command, block=False)
        except Exception:
            pass

    def _wire_remote_commands(self) -> None:
        mode2_button = getattr(self.spike4ch_tab, "send_channels_button", None)
        try:
            mode2_button.clicked.disconnect()
        except Exception:
            pass
        try:
            mode2_button.setEnabled(False)
            mode2_button.setVisible(False)
        except Exception:
            pass

        try:
            self.spike1ch_tab.send_command_button.clicked.disconnect()
        except Exception:
            pass
        self.spike1ch_tab.send_command_button.clicked.connect(self._send_remote_mode1_channel)
        try:
            self.spike1ch_tab.channel_combo.currentIndexChanged.connect(self._on_mode1_channel_combo_changed)
        except Exception:
            pass

        try:
            self.spike1ch_tab.send_mode3_command_button.clicked.disconnect()
        except Exception:
            pass
        self.spike1ch_tab.send_mode3_command_button.clicked.connect(self._send_remote_mode3_channel)
        try:
            self.spike1ch_tab.mode3_channel_combo.currentIndexChanged.connect(self._on_mode3_channel_combo_changed)
        except Exception:
            pass

        for combo in getattr(self.spike4ch_tab, "channel_combos", []):
            try:
                combo.setEnabled(False)
                combo.setVisible(False)
            except Exception:
                pass

        try:
            self.raster_tab.auto_threshold_button.clicked.disconnect()
        except Exception:
            pass
        self.raster_tab.auto_threshold_button.clicked.connect(self._send_remote_auto_threshold)

        try:
            self.raster_tab.send_threshold_button.clicked.disconnect()
        except Exception:
            pass
        self.raster_tab.send_threshold_button.clicked.connect(self._send_remote_manual_threshold)

        if hasattr(self, "impedance_tab") and hasattr(self.impedance_tab, "start_test_button"):
            try:
                self.impedance_tab.start_test_button.clicked.disconnect()
            except Exception:
                pass
            self.impedance_tab.start_test_button.clicked.connect(
                lambda: self._send_remote(make_slot_command("start_impedance_test"))
            )

    def _send_remote_mode2_channels(self) -> None:
        self._pending_mode2_channels = None
        self._channel_selection_dirty["mode2"] = False
        try:
            self.statusBar().showMessage("Mode2 v2 streams fixed 16 channels (RHD Ch0-Ch15).")
        except Exception:
            pass

    def _send_remote_mode1_channel(self) -> None:
        channel = int(self.spike1ch_tab.channel_combo.currentIndex())
        self._pending_mode1_channel = channel
        self._channel_selection_dirty["mode1"] = False
        self._send_remote(
            make_slot_command(
                "set_mode1_channel",
                channel=channel,
                auto_threshold=False,
            )
        )

    def _send_remote_mode3_channel(self) -> None:
        channel = int(self.spike1ch_tab.mode3_channel_combo.currentIndex())
        self._pending_mode3_channel = channel
        self._channel_selection_dirty["mode3"] = False
        self._send_remote(
            make_slot_command(
                "set_mode3_raw_channel",
                channel=channel,
            )
        )

    def _on_mode1_channel_combo_changed(self, _index: int) -> None:
        self._pending_mode1_channel = None
        self._channel_selection_dirty["mode1"] = True

    def _on_mode3_channel_combo_changed(self, _index: int) -> None:
        self._pending_mode3_channel = None
        self._channel_selection_dirty["mode3"] = True

    def _on_mode2_channel_combo_changed(self, _index: int) -> None:
        self._pending_mode2_channels = None
        self._channel_selection_dirty["mode2"] = False

    @staticmethod
    def _combo_is_user_editing(combo) -> bool:
        if combo is None:
            return False
        try:
            if combo.hasFocus():
                return True
        except Exception:
            pass
        try:
            view = combo.view()
            if view is not None and view.isVisible():
                return True
        except Exception:
            pass
        return False

    def _send_remote_auto_threshold(self) -> None:
        self._send_remote(
            make_slot_command(
                "auto_spike_threshold",
            )
        )

    def _send_remote_manual_threshold(self) -> None:
        try:
            threshold_uv = float(self.raster_tab.threshold_combo.currentText().strip())
        except Exception:
            return
        neural = self._current_status().get("neural", {})
        active_mode = str(neural.get("active_mode", "Idle") or "Idle").strip().lower()
        self._send_remote(
            make_slot_command(
                "set_spike_threshold",
                channel=self.raster_tab.channel_combo.currentIndex(),
                threshold_uv=threshold_uv,
                use_mode1_mapping=(active_mode == "single channel spike"),
            )
        )

    def _detach_detail_stream(self) -> None:
        if self.local_service is not None:
            if not bool(self._closing_from_service):
                try:
                    self.local_service.on_local_chart_closed()
                except Exception:
                    pass
            return
        try:
            self.command_queue.put(make_slot_command("detail_attach", attached=False), block=False)
        except Exception:
            pass

    def _current_status(self) -> Dict[str, Any]:
        if self.local_service is not None:
            try:
                return dict(self.local_service.get_chart_status_snapshot())
            except Exception:
                return {}
        return self.runtime_cache.read_status({})

    def update_slot_status(self, status: Dict[str, Any]) -> None:
        if not isinstance(status, dict):
            return
        self._apply_status_payload(status)

    @staticmethod
    def _detail_payload_stream_key(payload: object) -> str:
        try:
            header = payload[0] if isinstance(payload, (list, tuple)) and payload else None
            mode = int(header[0]) if isinstance(header, (list, tuple)) and header else -1
        except Exception:
            mode = -1
        if mode == 0:
            return "lfp"
        if mode == 1:
            return "spike"
        if mode == 2:
            return "mode2"
        return "unknown"

    def _apply_status_payload(self, status: Dict[str, Any]) -> None:
        if not status:
            return
        battery = status.get("battery", {})
        try:
            self.update_idle_status([
                float(battery.get("reported_rsoc", battery.get("rsoc", 0.0)) or 0.0),
                float(battery.get("stat", 0.0) or 0.0),
                float(battery.get("voltage", 0.0) or 0.0),
            ])
        except Exception:
            pass
        try:
            neural = status.get("neural", {})
            self._sync_remote_sample_mode_hint(neural)
            self.run_time_label.setText(
                f"Run: {format_run_duration(neural.get('run_time_min', 0.0))}"
            )
            self.set_main_serial_ui_connected(bool(neural.get("connected", False)), str(neural.get("port", "")))
            self._sync_remote_controls(status)
            impedance_result_seq = int(neural.get("impedance_result_seq", -1) or -1)
        except Exception:
            impedance_result_seq = -1
            pass
        try:
            rf = status.get("rf", {})
            state = str(rf.get("power_state", "unknown"))
            self.rf_power_status = 2 if state == "on" else 1 if state == "off" else 0
            self.rf_power_status_update()
        except Exception:
            pass
        try:
            habits = status.get("habits", {})
            habits_cache_seq = int(habits.get("cache_update_seq", -1) or -1)
        except Exception:
            habits_cache_seq = -1
        if not bool(getattr(self, "_habits_history_loaded", False)):
            self._refresh_habits_cache_if_needed(force=True, cache_seq=habits_cache_seq)
        elif habits_cache_seq > self._last_habits_cache_seq:
            self._last_habits_cache_seq = int(habits_cache_seq)
            self._refresh_habits_live_if_needed(force=True)
        if impedance_result_seq > self._last_impedance_result_seq:
            self._refresh_impedance_history_if_needed(force=True, result_seq=impedance_result_seq)
        else:
            self._refresh_impedance_history_if_needed(force=False)

    def _sync_remote_sample_mode_hint(self, neural: Dict[str, Any]) -> None:
        if not isinstance(neural, dict):
            return
        active_mode = neural.get("active_mode", "")
        stream_mode = neural.get("stream_mode", None)
        mode_parser = getattr(self, "_mode_label_to_sample_index", None)
        if not callable(mode_parser):
            mode_parser = self._fallback_mode_label_to_sample_index
        mode_index = mode_parser(stream_mode)
        if mode_index is None:
            mode_index = mode_parser(active_mode)
        if mode_index is None:
            return
        try:
            self.current_sample_mode = int(mode_index)
            self._sample_mode_hint_initialized = True
        except Exception:
            pass
        try:
            self.current_recording_mode = str(active_mode or "")
        except Exception:
            pass
        try:
            self._remote_active_mode_text = str(active_mode or "")
        except Exception:
            pass
        mode3_marker = getattr(self, "_mark_mode3_display_hint", None)
        if callable(mode3_marker):
            if int(mode_index) == 3:
                mode3_marker(True)
            else:
                mode3_marker(False)
        refresh_display_tuning = getattr(self, "_refresh_display_tuning_button_style", None)
        if callable(refresh_display_tuning):
            refresh_display_tuning()

    @staticmethod
    def _fallback_mode_label_to_sample_index(mode_label: Any) -> Optional[int]:
        try:
            numeric_mode = int(mode_label)
        except Exception:
            numeric_mode = None
        if numeric_mode in (0, 1, 2, 3):
            return int(numeric_mode)
        normalized = str(mode_label or "").strip().lower().replace(" ", "")
        normalized = normalized.replace("_", "").replace("-", "")
        if normalized in {"16channelslfp", "lfp", "mode0", "mode0lfp+mand+raw", "mode0lfp+mand"}:
            return 0
        if normalized in {"singlechannelspike", "singlechannel", "mode1"}:
            return 1
        if normalized in {"16channelsspike", "16channel", "16chspike", "4channelsspike", "4channel", "mode2"}:
            return 2
        if normalized in {"esa&mua", "esamua", "rsa&mua", "rsamua", "mode3"}:
            return 3
        if "mode3" in normalized or ("mua" in normalized and ("esa" in normalized or "rsa" in normalized)):
            return 3
        return None

    def _poll_status(self) -> None:
        self._apply_status_payload(self._current_status())

    def handle_detail_payload(self, payload: object) -> None:
        now_mono = time.monotonic()
        stream_key = self._detail_payload_stream_key(payload)
        last_by_stream = getattr(self, "_last_detail_render_monotonic_by_stream", None)
        if not isinstance(last_by_stream, dict):
            last_by_stream = {}
            self._last_detail_render_monotonic_by_stream = last_by_stream
        try:
            last_by_stream[stream_key] = now_mono
            self._last_detail_render_monotonic = now_mono
            self.update_plot_data(payload)
        except Exception:
            pass

    def _poll_detail_payload(self) -> None:
        meta = read_json(self.runtime_cache.detail_buffer_meta_path, {})
        if not meta or not meta.get("attached"):
            return
        queue_entries = meta.get("payload_queue")
        if isinstance(queue_entries, list) and queue_entries:
            pending_entries = []
            for entry in queue_entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    sequence = int(entry.get("sequence", -1) or -1)
                except Exception:
                    sequence = -1
                if sequence > self._last_detail_sequence:
                    pending_entries.append((sequence, entry))
            pending_entries.sort(key=lambda item: item[0])
            frames_processed = 0
            for sequence, entry in pending_entries:
                if frames_processed >= int(self._detail_payload_max_frames_per_poll):
                    break
                payload_size = int(entry.get("payload_size", 0) or 0)
                if payload_size <= 0:
                    self._last_detail_sequence = max(self._last_detail_sequence, sequence)
                    continue
                payload_path = str(entry.get("payload_path", "")).strip()
                if not payload_path or not os.path.exists(payload_path):
                    self._last_detail_sequence = max(self._last_detail_sequence, sequence)
                    continue
                try:
                    with open(payload_path, "rb") as f:
                        raw = f.read()
                    payload = pickle.loads(raw)
                except Exception:
                    return
                self._last_detail_sequence = max(self._last_detail_sequence, sequence)
                self.handle_detail_payload(payload)
                frames_processed += 1
                try:
                    os.remove(payload_path)
                except OSError:
                    pass
            return
        sequence = int(meta.get("sequence", -1) or -1)
        if sequence <= self._last_detail_sequence:
            return
        payload_size = int(meta.get("payload_size", 0) or 0)
        if payload_size <= 0:
            return
        try:
            if meta.get("transport") == "file":
                payload_path = str(meta.get("payload_path", "")).strip()
                if not payload_path or not os.path.exists(payload_path):
                    return
                with open(payload_path, "rb") as f:
                    raw = f.read()
            else:
                shm_name = str(meta.get("shared_memory_name", "")).strip()
                if not shm_name:
                    return
                shm = shared_memory.SharedMemory(name=shm_name)
                try:
                    raw = bytes(shm.buf[:payload_size])
                finally:
                    shm.close()
            payload = pickle.loads(raw)
        except Exception:
            return
        self._last_detail_sequence = sequence
        self.handle_detail_payload(payload)

    def _refresh_habits_cache_if_needed(self, force: bool = False, cache_seq: Optional[int] = None) -> None:
        panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        if panel is None:
            return
        if self.local_service is not None:
            if not force and cache_seq is None:
                return
            try:
                habits = getattr(self.local_service, "habits", None)
                trials = habits.get_trial_data_for_cache() if hasattr(habits, "get_trial_data_for_cache") else []
                event_data = habits.get_event_data_for_cache() if hasattr(habits, "get_event_data_for_cache") else []
                state_data = habits.get_state_data_for_cache() if hasattr(habits, "get_state_data_for_cache") else []
                mode_switches = habits.get_mode_switches_for_cache() if hasattr(habits, "get_mode_switches_for_cache") else []
            except Exception:
                return
            if cache_seq is not None:
                self._last_habits_cache_seq = int(cache_seq)
            self._apply_habits_cache_payload(panel, trials, event_data, state_data, mode_switches)
            try:
                self._last_habits_live_sequence = max(
                    int(getattr(self, "_last_habits_live_sequence", 0) or 0),
                    self._latest_local_habits_live_sequence(),
                )
            except Exception:
                pass
            self._habits_history_loaded = True
            return
        trial_path = self.runtime_cache.habits_trial_data_path
        event_path = self.runtime_cache.habits_event_data_path
        state_path = self.runtime_cache.habits_state_data_path
        mode_switch_path = self.runtime_cache.mode_switches_path
        try:
            trial_mtime = os.path.getmtime(trial_path) if os.path.exists(trial_path) else 0.0
            event_mtime = os.path.getmtime(event_path) if os.path.exists(event_path) else 0.0
            state_mtime = os.path.getmtime(state_path) if os.path.exists(state_path) else 0.0
            mode_mtime = os.path.getmtime(mode_switch_path) if os.path.exists(mode_switch_path) else 0.0
        except Exception:
            return
        if (
            not force
            and trial_mtime <= self._last_habits_mtime
            and event_mtime <= self._last_habits_event_mtime
            and state_mtime <= self._last_habits_state_mtime
            and mode_mtime <= self._last_mode_switch_mtime
        ):
            return
        self._last_habits_mtime = trial_mtime
        self._last_habits_event_mtime = event_mtime
        self._last_habits_state_mtime = state_mtime
        self._last_mode_switch_mtime = mode_mtime
        if cache_seq is not None:
            self._last_habits_cache_seq = int(cache_seq)
        trials = read_json(trial_path, [])
        event_data = read_json(event_path, [])
        state_data = read_json(state_path, [])
        mode_switches = read_json(mode_switch_path, [])
        self._apply_habits_cache_payload(panel, trials, event_data, state_data, mode_switches)
        self._habits_history_loaded = True
        self._refresh_habits_live_if_needed(force=True)

    @staticmethod
    def _parse_habits_live_timestamp(value: Any) -> Any:
        if isinstance(value, datetime):
            return value
        if value is None:
            return value
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return value

    def _refresh_habits_live_if_needed(self, force: bool = False) -> None:
        panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        if panel is None:
            return
        if self.local_service is not None:
            if force:
                self._refresh_local_habits_live_updates()
            return
        try:
            live_path = self.runtime_cache.habits_live_path
            if not os.path.exists(live_path):
                return
            payload = self.runtime_cache.read_habits_live({})
            file_sequence = int(payload.get("sequence", -1) or -1)
        except Exception:
            return
        if not force and file_sequence <= int(getattr(self, "_last_habits_live_file_sequence", -1) or -1):
            return
        self._last_habits_live_file_sequence = file_sequence
        self.handle_habits_live_payload(payload)

    def _latest_local_habits_live_sequence(self) -> int:
        habits = getattr(self.local_service, "habits", None) if self.local_service is not None else None
        getter = getattr(habits, "get_live_updates_since", None)
        if not callable(getter):
            return int(getattr(self, "_last_habits_live_sequence", 0) or 0)
        try:
            latest = list(getter(0, limit=1))
        except Exception:
            return int(getattr(self, "_last_habits_live_sequence", 0) or 0)
        if not latest:
            return int(getattr(self, "_last_habits_live_sequence", 0) or 0)
        try:
            return max(int(item.get("sequence", 0) or 0) for item in latest if isinstance(item, dict))
        except Exception:
            return int(getattr(self, "_last_habits_live_sequence", 0) or 0)

    def _refresh_local_habits_live_updates(self) -> None:
        habits = getattr(self.local_service, "habits", None) if self.local_service is not None else None
        getter = getattr(habits, "get_live_updates_since", None)
        if not callable(getter):
            return
        try:
            tail_limit = max(1, int(self.detail_runtime_config.get("habits_live_update_tail", 20000) or 20000))
        except Exception:
            tail_limit = 20000
        try:
            updates = list(getter(int(getattr(self, "_last_habits_live_sequence", 0) or 0), limit=tail_limit))
        except Exception:
            return
        if not updates:
            return
        self.handle_habits_live_payload({
            "last_update_sequence": max(
                int(item.get("sequence", 0) or 0)
                for item in updates
                if isinstance(item, dict)
            ),
            "timestamp_epoch": time.time(),
            "updates": updates,
        })

    def handle_habits_live_payload(self, payload) -> None:
        panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        if panel is None:
            return
        if isinstance(payload, dict):
            updates = list(payload.get("updates", []) or [])
        else:
            updates = list(payload or [])
        if not updates:
            return
        last_sequence = int(getattr(self, "_last_habits_live_sequence", 0) or 0)
        new_updates = [
            item for item in updates
            if isinstance(item, dict) and int(item.get("sequence", 0) or 0) > last_sequence
        ]
        if not new_updates:
            return
        self._apply_habits_live_updates(panel, new_updates)
        try:
            self._last_habits_live_sequence = max(int(item.get("sequence", 0) or 0) for item in new_updates)
        except Exception:
            pass

    def _apply_habits_live_updates(self, panel, updates) -> None:
        chart_refresh_required = False
        for update in list(updates or []):
            if not isinstance(update, dict):
                continue
            kind = str(update.get("kind", "") or "")
            payload = update.get("payload", {})
            if not isinstance(payload, dict):
                continue
            try:
                if kind == "trial":
                    trial = dict(payload)
                    trial["timestamp"] = self._parse_habits_live_timestamp(trial.get("timestamp"))
                    if isinstance(trial.get("timestamp"), datetime):
                        panel.data_manager.add_trial(trial)
                        chart_refresh_required = True
                elif kind == "event":
                    panel.data_manager.add_event_data(
                        int(payload.get("trial_num", 0) or 0),
                        list(payload.get("events", []) or []),
                    )
                    chart_refresh_required = True
                elif kind == "state":
                    panel.data_manager.add_state_data(
                        int(payload.get("trial_num", 0) or 0),
                        int(payload.get("outcome", -1) or -1),
                        list(payload.get("states", []) or []),
                    )
                    chart_refresh_required = True
                elif kind == "mode_switch":
                    start = self._parse_habits_live_timestamp(payload.get("start"))
                    end = self._parse_habits_live_timestamp(payload.get("end"))
                    if isinstance(start, datetime) and isinstance(end, datetime):
                        panel.data_manager.add_mode_switch({"start": start, "end": end})
                        chart_refresh_required = True
            except Exception:
                continue
        if not chart_refresh_required:
            return
        try:
            panel._refresh_status_information()
            panel.plot_24h_trials()
            panel.update_performance_chart()
            panel.update_event_chart()
            panel.update_cap_trial_chart()
        except Exception:
            pass

    def _apply_habits_cache_payload(self, panel, trials, event_data, state_data, mode_switches) -> None:
        try:
            panel.data_manager.clear_data()
            panel.data_manager.load_from_list(trials or [])
            panel.data_manager.state_data.clear()
            for item in state_data or []:
                if isinstance(item, dict):
                    panel.data_manager.state_data.append(dict(item))
            panel.data_manager.event_data.clear()
            for item in event_data or []:
                if isinstance(item, dict):
                    panel.data_manager.event_data.append(dict(item))
            panel.data_manager.mode_switches = []
            for item in mode_switches or []:
                start_raw = item.get("start")
                end_raw = item.get("end")
                if not start_raw or not end_raw:
                    continue
                try:
                    panel.data_manager.mode_switches.append({
                        "start": datetime.fromisoformat(str(start_raw)),
                        "end": datetime.fromisoformat(str(end_raw)),
                    })
                except Exception:
                    continue
            panel._refresh_status_information()
            panel.plot_24h_trials()
            panel.update_performance_chart()
            panel.update_event_chart()
            panel.update_cap_trial_chart()
        except Exception:
            pass

    def _refresh_impedance_history_if_needed(
        self,
        force: bool = False,
        result_seq: Optional[int] = None,
    ) -> None:
        if not hasattr(self, "impedance_tab"):
            return
        path = self.runtime_cache.impedance_history_path
        try:
            history_mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
        except Exception:
            return
        if not force and history_mtime <= self._last_impedance_history_mtime:
            return
        self._last_impedance_history_mtime = history_mtime
        if result_seq is not None:
            self._last_impedance_result_seq = int(result_seq)
        try:
            self.impedance_history_path = path
            self._load_impedance_history()
        except Exception:
            pass

    def _sync_remote_controls(self, status: Dict[str, Any]) -> None:
        neural = status.get("neural", {})
        habits = status.get("habits", {})
        connected = bool(neural.get("connected", False))
        active_mode_text = str(neural.get("active_mode", "Idle") or "Idle").strip().lower()
        auto_threshold_running = bool(neural.get("auto_threshold_running", False))
        auto_threshold_available = connected and bool(neural.get("sampling", False)) and active_mode_text == "single channel spike"
        ui_capabilities = dict(neural.get("ui_capabilities", {}) or {})
        self._enable_remote_local_controls()
        spike_filter_supported = bool(ui_capabilities.get("remote_spike_filter_config", True))
        impedance_comp_supported = bool(ui_capabilities.get("remote_impedance_compensation", True))
        self._sync_remote_checkbox_control(
            "spike_filter_enabled",
            self.spike1ch_tab.filter_enable_checkbox,
            bool(neural.get("spike_filter_enabled", False)),
        )
        self._sync_remote_double_spin_control(
            "spike_filter_low_cut_hz",
            self.spike1ch_tab.low_cut_spin,
            neural.get("spike_filter_low_cut_hz"),
        )
        self._sync_remote_double_spin_control(
            "spike_filter_high_cut_hz",
            self.spike1ch_tab.high_cut_spin,
            neural.get("spike_filter_high_cut_hz"),
        )
        self._sync_remote_combo_control(
            "spike_filter_sample_rate_hz",
            self.spike1ch_tab.sample_rate_combo,
            int(float(neural.get("spike_filter_sample_rate_hz", 20000.0) or 20000.0)),
        )
        self._sync_remote_double_spin_control(
            "rc_series_resistor_kohm",
            self.impedance_tab.series_resistor_spin,
            neural.get("rc_series_resistor_kohm"),
        )
        self._sync_remote_double_spin_control(
            "rc_shunt_cap_pf",
            self.impedance_tab.shunt_cap_spin,
            neural.get("rc_shunt_cap_pf"),
        )
        try:
            self.spike1ch_tab.filter_enable_checkbox.setEnabled(spike_filter_supported)
            self.spike1ch_tab.low_cut_spin.setEnabled(spike_filter_supported)
            self.spike1ch_tab.high_cut_spin.setEnabled(spike_filter_supported)
            self.spike1ch_tab.sample_rate_combo.setEnabled(spike_filter_supported)
            self.spike1ch_tab.open_spectrum_button.setEnabled(bool(ui_capabilities.get("local_spike_spectrum", True)))
            self.spectrum_window_button.setEnabled(bool(ui_capabilities.get("local_lfp_spectrum", True)))
            self.impedance_tab.series_resistor_spin.setEnabled(impedance_comp_supported)
            self.impedance_tab.shunt_cap_spin.setEnabled(impedance_comp_supported)
        except Exception:
            pass
        try:
            self.spike1ch_tab.open_spectrum_button.setToolTip("Opens the local spike spectrum window for this detail chart.")
            self.spectrum_window_button.setToolTip("Opens the local LFP spectrum window for this detail chart.")
        except Exception:
            pass
        try:
            self.spike1ch_tab.channel_combo.setEnabled(connected)
        except Exception:
            pass
        try:
            self.spike1ch_tab.mode3_channel_combo.setEnabled(connected)
        except Exception:
            pass
        for combo in getattr(self.spike4ch_tab, "channel_combos", []):
            try:
                combo.setEnabled(False)
                combo.setVisible(False)
            except Exception:
                pass
        try:
            self.spike4ch_tab.send_channels_button.setEnabled(False)
            self.spike4ch_tab.send_channels_button.setVisible(False)
        except Exception:
            pass
        self.spike1ch_tab.send_command_button.setEnabled(connected)
        self.spike1ch_tab.send_mode3_command_button.setEnabled(connected)
        self.raster_tab.auto_threshold_button.setEnabled(auto_threshold_available and not auto_threshold_running)
        self.raster_tab.auto_threshold_button.setText(
            "Auto threshold update..." if auto_threshold_running else "Auto threshold update"
        )
        self.raster_tab.auto_threshold_button.setToolTip(
            "Matches the original GUI: available only during Single Channel Spike sampling, sweeps all 16 Mode1 channels, and waits for a full raw-data window before updating each threshold."
        )
        self.raster_tab.send_threshold_button.setEnabled(connected)
        self.raster_tab.channel_combo.setEnabled(connected)
        self.raster_tab.threshold_combo.setEnabled(connected)
        if hasattr(self, "impedance_tab") and hasattr(self.impedance_tab, "start_test_button"):
            impedance_running = bool(neural.get("impedance_test_running", False))
            self.impedance_tab.start_test_button.setEnabled(connected and not impedance_running)
            if hasattr(self.impedance_tab, "update_progress"):
                self.impedance_tab.update_progress(
                    int(float(neural.get("impedance_progress_percent", 0.0) or 0.0)),
                    str(neural.get("impedance_status_text", "Ready") or "Ready"),
                    impedance_running,
                )

        mode1_channel = int(neural.get("mode1_raw_channel", 0) or 0)
        mode3_channel = int(neural.get("mode3_raw_channel", 0) or 0)
        try:
            target_mode1_channel = max(0, min(15, mode1_channel))
            if self._pending_mode1_channel is not None:
                if target_mode1_channel == self._pending_mode1_channel:
                    self._pending_mode1_channel = None
                else:
                    target_mode1_channel = int(self._pending_mode1_channel)
            mode1_editing = self._combo_is_user_editing(self.spike1ch_tab.channel_combo)
            if (
                self._channel_selection_dirty["mode1"]
                and not mode1_editing
                and self.spike1ch_tab.channel_combo.currentIndex() == target_mode1_channel
            ):
                self._channel_selection_dirty["mode1"] = False
            if not self._channel_selection_dirty["mode1"] and not mode1_editing:
                self.spike1ch_tab.channel_combo.blockSignals(True)
                self.spike1ch_tab.channel_combo.setCurrentIndex(target_mode1_channel)
                self.spike1ch_tab.channel_combo.blockSignals(False)

            target_mode3_channel = max(0, min(15, mode3_channel))
            if self._pending_mode3_channel is not None:
                if target_mode3_channel == self._pending_mode3_channel:
                    self._pending_mode3_channel = None
                else:
                    target_mode3_channel = int(self._pending_mode3_channel)
            mode3_editing = self._combo_is_user_editing(self.spike1ch_tab.mode3_channel_combo)
            if (
                self._channel_selection_dirty["mode3"]
                and not mode3_editing
                and self.spike1ch_tab.mode3_channel_combo.currentIndex() == target_mode3_channel
            ):
                self._channel_selection_dirty["mode3"] = False
            if not self._channel_selection_dirty["mode3"] and not mode3_editing:
                self.spike1ch_tab.mode3_channel_combo.blockSignals(True)
                self.spike1ch_tab.mode3_channel_combo.setCurrentIndex(target_mode3_channel)
                self.spike1ch_tab.mode3_channel_combo.blockSignals(False)

            self._pending_mode2_channels = None
            self._channel_selection_dirty["mode2"] = False
            for combo in getattr(self.spike4ch_tab, "channel_combos", []):
                combo.blockSignals(True)
                combo.setEnabled(False)
                combo.setVisible(False)
                combo.blockSignals(False)
            fixed_label = getattr(self.spike4ch_tab, "fixed_channels_label", None)
            if fixed_label is not None:
                fixed_label.setText("Mode2 v2: fixed RHD Ch0-Ch15, 10.417 kHz, 8-bit raw")
        except Exception:
            pass

        panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        if panel is None:
            return
        try:
            self._sync_habits_panel_from_status(panel, habits)
            panel.mouse_id_edit.setText(str(habits.get("mouse_id", "") or ""))
            panel.selected_data_dir = str(habits.get("selected_data_dir", "") or "")
            self._sync_habits_date_widget(
                getattr(panel, "start_date_edit", None),
                str(habits.get("start_date", "") or "").strip(),
            )
            panel.sync_time_label.setText(str(habits.get("last_sync_text", "Not synced") or "Not synced"))
        except Exception:
            pass

    def _stop_window_resources_for_close(self) -> None:
        self._stop_remote_local_timers()
        try:
            self.status_poll_timer.stop()
            if self.detail_poll_timer is not None:
                self.detail_poll_timer.stop()
            self.habits_poll_timer.stop()
            self._spike_filter_push_timer.stop()
            self._impedance_compensation_push_timer.stop()
        except Exception:
            pass
        try:
            self.camera_timer.stop()
        except Exception:
            pass
        spectrum_window = getattr(self, "spectrum_window", None)
        if spectrum_window is not None:
            try:
                spectrum_window.close()
            except Exception:
                pass
            self.spectrum_window = None

    def closeEvent(self, event):
        self._detach_detail_stream()
        self._stop_window_resources_for_close()
        event.accept()

    def close_from_service(self) -> None:
        self._closing_from_service = True
        try:
            # Service-initiated detach is already reflected in SlotService state.
            # Avoid re-entering closeEvent/_detach_detail_stream; just stop local
            # timers and release the widget asynchronously.
            self._stop_window_resources_for_close()
            try:
                self.hide()
            except Exception:
                pass
            try:
                self.deleteLater()
            except Exception:
                self.close()
        finally:
            self._closing_from_service = False


def run_detail_window(slot_payload: Dict[str, Any], runtime_root: str, command_queue) -> int:
    configure_qt_runtime()
    app = QApplication(sys.argv[:1] or ["detail_window"])
    window = DetailMainWindow(slot_payload, runtime_root, command_queue)
    window.show()
    return app.exec()
