import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import messagebox, ttk

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover - runtime environment dependent
    serial = None


DEFAULT_BAUDRATE = 115200
DEFAULT_ELECTRODES = 2
PLOT_HISTORY = 240


@dataclass
class DataFrame:
    timestamp_ms: int
    mode: str
    touch_hex: str
    electrode_count: int
    electrodes: List[Tuple[int, int, int]]


class SerialReader(threading.Thread):
    def __init__(self, event_queue: queue.Queue):
        super().__init__(daemon=True)
        self.event_queue = event_queue
        self.stop_event = threading.Event()
        self.serial_connection = None
        self.write_lock = threading.Lock()

    def connect(self, port: str, baudrate: int) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed")

        self.serial_connection = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=0.2,
            write_timeout=1,
        )
        self.stop_event.clear()
        if not self.is_alive():
            self.start()
        self.event_queue.put(("status", f"Connected to {port} @ {baudrate}"))

    def run(self) -> None:
        while not self.stop_event.is_set():
            if self.serial_connection is None:
                time.sleep(0.05)
                continue

            try:
                line = self.serial_connection.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    self.event_queue.put(("line", line))
            except Exception as exc:  # pragma: no cover - hardware dependent
                self.event_queue.put(("error", f"Serial read error: {exc}"))
                self.close()
                break

    def send(self, command: str) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            raise RuntimeError("Serial port is not connected")

        payload = (command.strip() + "\n").encode("utf-8")
        with self.write_lock:
            self.serial_connection.write(payload)
            self.serial_connection.flush()

    def close(self) -> None:
        self.stop_event.set()
        if self.serial_connection is not None:
            try:
                if self.serial_connection.is_open:
                    self.serial_connection.close()
            except Exception:
                pass
        self.serial_connection = None
        self.event_queue.put(("status", "Disconnected"))


class Mpr121DebugGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MPR121 Debug GUI")
        self.root.geometry("1220x840")

        self.event_queue: queue.Queue = queue.Queue()
        self.reader = SerialReader(self.event_queue)

        self.status_var = tk.StringVar(value="Ready")
        self.connection_var = tk.StringVar(value="Not connected")
        self.mode_var = tk.StringVar(value="-")
        self.touch_var = tk.StringVar(value="-")
        self.last_ts_var = tk.StringVar(value="-")
        self.raw_line_count_var = tk.StringVar(value="0")

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUDRATE))
        self.interval_var = tk.StringVar(value="100")
        self.threshold_touch_var = tk.StringVar(value="12")
        self.threshold_release_var = tk.StringVar(value="6")
        self.debounce_touch_var = tk.StringVar(value="0")
        self.debounce_release_var = tk.StringVar(value="0")
        self.relearn_ms_var = tk.StringVar(value="3000")
        self.read_reg_var = tk.StringVar(value="0x5E")
        self.write_reg_var = tk.StringVar(value="0x5E")
        self.write_val_var = tk.StringVar(value="0x00")
        self.raw_command_var = tk.StringVar()

        self.log_line_count = 0
        self.table_rows: Dict[int, Tuple[tk.StringVar, tk.StringVar, tk.StringVar]] = {}
        self.plot_canvases: Dict[int, tk.Canvas] = {}
        self.plot_titles: Dict[int, tk.StringVar] = {}
        self.baseline_history: Dict[int, deque] = {
            0: deque(maxlen=PLOT_HISTORY),
            1: deque(maxlen=PLOT_HISTORY),
        }
        self.filtered_history: Dict[int, deque] = {
            0: deque(maxlen=PLOT_HISTORY),
            1: deque(maxlen=PLOT_HISTORY),
        }

        self._build_ui()
        self.refresh_ports()
        self.root.after(50, self.process_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.grid(row=0, column=0, sticky="ew")
        top_frame.columnconfigure(6, weight=1)

        ttk.Label(top_frame, text="Port").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(top_frame, textvariable=self.port_var, width=28, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="w", padx=(4, 8))
        ttk.Button(top_frame, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=(0, 12))

        ttk.Label(top_frame, text="Baud").grid(row=0, column=3, sticky="w")
        self.baud_combo = ttk.Combobox(
            top_frame,
            textvariable=self.baud_var,
            values=("9600", "57600", "115200", "230400"),
            width=10,
            state="readonly",
        )
        self.baud_combo.grid(row=0, column=4, sticky="w", padx=(4, 8))

        ttk.Button(top_frame, text="Connect", command=self.connect).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(top_frame, text="Disconnect", command=self.disconnect).grid(row=0, column=6, sticky="w")

        ttk.Label(top_frame, textvariable=self.connection_var).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(top_frame, textvariable=self.status_var).grid(row=1, column=4, columnspan=3, sticky="w", pady=(8, 0))

        mid_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        mid_frame.grid(row=1, column=0, sticky="ew")
        mid_frame.columnconfigure(0, weight=0)
        mid_frame.columnconfigure(1, weight=1)

        summary_box = ttk.LabelFrame(mid_frame, text="Live Summary", padding=10)
        summary_box.grid(row=0, column=0, sticky="nw", padx=(0, 10))
        self._add_summary_row(summary_box, 0, "Mode", self.mode_var)
        self._add_summary_row(summary_box, 1, "Touch", self.touch_var)
        self._add_summary_row(summary_box, 2, "Last ms", self.last_ts_var)
        self._add_summary_row(summary_box, 3, "Lines", self.raw_line_count_var)

        cmd_box = ttk.LabelFrame(mid_frame, text="Commands", padding=10)
        cmd_box.grid(row=0, column=1, sticky="ew")
        for col in range(6):
            cmd_box.columnconfigure(col, weight=1)

        self._add_command_button(cmd_box, 0, 0, "1 Stop", "1")
        self._add_command_button(cmd_box, 0, 1, "2 Run Lock", "2")
        self._add_command_button(cmd_box, 0, 2, "3 Run Update", "3")
        self._add_command_button(cmd_box, 0, 3, "4 Default", "4")
        self._add_command_button(cmd_box, 0, 4, "5 Anti EMI", "5")
        self._add_command_button(cmd_box, 0, 5, "6 Sensitive", "6")
        self._add_command_button(cmd_box, 1, 0, "7 Reinit", "7")
        self._add_command_button(cmd_box, 1, 1, "8 Dump Reg", "8")
        self._add_command_button(cmd_box, 1, 2, "9 Toggle 2/12", "9")
        self._add_command_button(cmd_box, 1, 3, "S Snapshot", "S")
        self._add_command_button(cmd_box, 1, 4, "P Help", "P")
        ttk.Button(cmd_box, text="Clear Log", command=self.clear_log).grid(row=1, column=5, sticky="ew", padx=3, pady=3)

        ttk.Label(cmd_box, text="I,<ms>").grid(row=2, column=0, sticky="e", padx=(0, 4), pady=(10, 3))
        ttk.Entry(cmd_box, textvariable=self.interval_var, width=10).grid(row=2, column=1, sticky="w", pady=(10, 3))
        ttk.Button(cmd_box, text="Send", command=lambda: self.send_command(f"I,{self.interval_var.get().strip()}")).grid(row=2, column=2, sticky="ew", padx=3, pady=(10, 3))

        ttk.Label(cmd_box, text="T,<touch>,<release>").grid(row=2, column=3, sticky="e", padx=(12, 4), pady=(10, 3))
        threshold_frame = ttk.Frame(cmd_box)
        threshold_frame.grid(row=2, column=4, sticky="w", pady=(10, 3))
        ttk.Entry(threshold_frame, textvariable=self.threshold_touch_var, width=6).pack(side="left")
        ttk.Label(threshold_frame, text=",").pack(side="left")
        ttk.Entry(threshold_frame, textvariable=self.threshold_release_var, width=6).pack(side="left")
        ttk.Button(cmd_box, text="Send", command=self.send_thresholds).grid(row=2, column=5, sticky="ew", padx=3, pady=(10, 3))

        ttk.Label(cmd_box, text="D,<touchDb>,<relDb>").grid(row=3, column=0, sticky="e", padx=(0, 4), pady=3)
        debounce_frame = ttk.Frame(cmd_box)
        debounce_frame.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Entry(debounce_frame, textvariable=self.debounce_touch_var, width=6).pack(side="left")
        ttk.Label(debounce_frame, text=",").pack(side="left")
        ttk.Entry(debounce_frame, textvariable=self.debounce_release_var, width=6).pack(side="left")
        ttk.Button(cmd_box, text="Send", command=self.send_debounce).grid(row=3, column=2, sticky="ew", padx=3, pady=3)

        ttk.Label(cmd_box, text="B,<ms>").grid(row=3, column=3, sticky="e", padx=(12, 4), pady=3)
        ttk.Entry(cmd_box, textvariable=self.relearn_ms_var, width=10).grid(row=3, column=4, sticky="w", pady=3)
        ttk.Button(cmd_box, text="Send", command=lambda: self.send_command(f"B,{self.relearn_ms_var.get().strip()}")).grid(row=3, column=5, sticky="ew", padx=3, pady=3)

        ttk.Label(cmd_box, text="G,<reg>").grid(row=4, column=0, sticky="e", padx=(0, 4), pady=3)
        ttk.Entry(cmd_box, textvariable=self.read_reg_var, width=12).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Button(cmd_box, text="Send", command=lambda: self.send_command(f"G,{self.read_reg_var.get().strip()}")).grid(row=4, column=2, sticky="ew", padx=3, pady=3)

        ttk.Label(cmd_box, text="W,<reg>,<val>").grid(row=4, column=3, sticky="e", padx=(12, 4), pady=3)
        write_frame = ttk.Frame(cmd_box)
        write_frame.grid(row=4, column=4, sticky="w", pady=3)
        ttk.Entry(write_frame, textvariable=self.write_reg_var, width=6).pack(side="left")
        ttk.Label(write_frame, text=",").pack(side="left")
        ttk.Entry(write_frame, textvariable=self.write_val_var, width=6).pack(side="left")
        ttk.Button(cmd_box, text="Send", command=self.send_write_reg).grid(row=4, column=5, sticky="ew", padx=3, pady=3)

        ttk.Label(cmd_box, text="Raw").grid(row=5, column=0, sticky="e", padx=(0, 4), pady=(8, 0))
        raw_entry = ttk.Entry(cmd_box, textvariable=self.raw_command_var)
        raw_entry.grid(row=5, column=1, columnspan=4, sticky="ew", pady=(8, 0))
        raw_entry.bind("<Return>", lambda _event: self.send_raw())
        ttk.Button(cmd_box, text="Send Raw", command=self.send_raw).grid(row=5, column=5, sticky="ew", padx=3, pady=(8, 0))

        content_frame = ttk.Panedwindow(self.root, orient="horizontal")
        content_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))

        data_box = ttk.LabelFrame(content_frame, text="Electrode Data", padding=10)
        log_box = ttk.LabelFrame(content_frame, text="Serial Log", padding=10)
        content_frame.add(data_box, weight=2)
        content_frame.add(log_box, weight=3)

        self._build_data_panel(data_box)
        self._build_log_box(log_box)

    def _add_summary_row(self, parent: ttk.Widget, row: int, label: str, value_var: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
        ttk.Label(parent, textvariable=value_var).grid(row=row, column=1, sticky="w", pady=2)

    def _add_command_button(self, parent: ttk.Widget, row: int, column: int, text: str, command: str) -> None:
        ttk.Button(parent, text=text, command=lambda cmd=command: self.send_command(cmd)).grid(
            row=row, column=column, sticky="ew", padx=3, pady=3
        )

    def _build_data_panel(self, parent: ttk.Widget) -> None:
        self._build_data_table(parent)
        self._build_plot_panel(parent)

    def _build_data_table(self, parent: ttk.Widget) -> None:
        header_frame = ttk.Frame(parent)
        header_frame.pack(fill="x")
        for idx, title in enumerate(("Ele", "Baseline", "Filtered", "Delta")):
            ttk.Label(header_frame, text=title, width=12 if idx else 6).grid(row=0, column=idx, sticky="w")

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True, pady=(8, 0))

        for electrode in range(DEFAULT_ELECTRODES):
            baseline_var = tk.StringVar(value="-")
            filtered_var = tk.StringVar(value="-")
            delta_var = tk.StringVar(value="-")
            self.table_rows[electrode] = (baseline_var, filtered_var, delta_var)

            ttk.Label(table_frame, text=f"E{electrode}", width=6).grid(row=electrode, column=0, sticky="w", pady=1)
            ttk.Label(table_frame, textvariable=baseline_var, width=12).grid(row=electrode, column=1, sticky="w", pady=1)
            ttk.Label(table_frame, textvariable=filtered_var, width=12).grid(row=electrode, column=2, sticky="w", pady=1)
            ttk.Label(table_frame, textvariable=delta_var, width=12).grid(row=electrode, column=3, sticky="w", pady=1)

    def _build_plot_panel(self, parent: ttk.Widget) -> None:
        plot_container = ttk.Frame(parent)
        plot_container.pack(fill="both", expand=True, pady=(12, 0))

        for electrode in range(DEFAULT_ELECTRODES):
            frame = ttk.LabelFrame(plot_container, text=f"E{electrode} Trend", padding=8)
            frame.pack(fill="both", expand=True, pady=(0, 8))

            title_var = tk.StringVar(
                value=f"E{electrode} var(base)=-  var(filtered)=-"
            )
            self.plot_titles[electrode] = title_var
            ttk.Label(frame, textvariable=title_var).pack(anchor="w", pady=(0, 4))

            canvas = tk.Canvas(frame, height=180, bg="white", highlightthickness=1, highlightbackground="#cccccc")
            canvas.pack(fill="both", expand=True)
            self.plot_canvases[electrode] = canvas
            canvas.bind("<Configure>", lambda _event, ele=electrode: self.draw_plot(ele))

    def _build_log_box(self, parent: ttk.Widget) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        self.log_text = tk.Text(parent, wrap="none", height=30)
        self.log_text.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=y_scroll.set)

    def refresh_ports(self) -> None:
        if serial is None:
            self.port_combo["values"] = ()
            self.status_var.set("pyserial not installed")
            return

        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])
        if not ports:
            self.port_var.set("")
            self.status_var.set("No serial ports found")

    def connect(self) -> None:
        if serial is None:
            messagebox.showerror("Missing dependency", "pyserial 未安装，请先运行: pip install pyserial")
            return

        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("No port", "请先选择串口")
            return

        try:
            baudrate = int(self.baud_var.get())
            self.reader.connect(port, baudrate)
            self.connection_var.set(f"Connected: {port} @ {baudrate}")
            self.status_var.set("Connected")
        except Exception as exc:
            self.status_var.set(f"Connect failed: {exc}")
            messagebox.showerror("Connection failed", str(exc))

    def disconnect(self) -> None:
        self.reader.close()
        self.connection_var.set("Not connected")

    def send_command(self, command: str) -> None:
        command = command.strip()
        if not command:
            return
        try:
            self.reader.send(command)
            self.append_log(f">>> {command}")
            self.status_var.set(f"Sent: {command}")
        except Exception as exc:
            self.status_var.set(f"Send failed: {exc}")
            messagebox.showerror("Send failed", str(exc))

    def send_thresholds(self) -> None:
        self.send_command(f"T,{self.threshold_touch_var.get().strip()},{self.threshold_release_var.get().strip()}")

    def send_debounce(self) -> None:
        self.send_command(f"D,{self.debounce_touch_var.get().strip()},{self.debounce_release_var.get().strip()}")

    def send_write_reg(self) -> None:
        self.send_command(f"W,{self.write_reg_var.get().strip()},{self.write_val_var.get().strip()}")

    def send_raw(self) -> None:
        self.send_command(self.raw_command_var.get())

    def append_log(self, line: str) -> None:
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_line_count += 1
        self.raw_line_count_var.set(str(self.log_line_count))

        max_lines = 3000
        current_lines = int(self.log_text.index("end-1c").split(".")[0])
        if current_lines > max_lines:
            self.log_text.delete("1.0", f"{current_lines - max_lines}.0")

    def clear_log(self) -> None:
        self.log_text.delete("1.0", "end")
        self.log_line_count = 0
        self.raw_line_count_var.set("0")

    def calc_variance(self, values: deque) -> Optional[float]:
        n = len(values)
        if n < 2:
            return None
        mean_value = sum(values) / n
        return sum((value - mean_value) ** 2 for value in values) / n

    def draw_plot(self, electrode: int) -> None:
        canvas = self.plot_canvases[electrode]
        canvas.delete("all")

        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        left = 40
        right = width - 12
        top = 12
        bottom = height - 24

        canvas.create_rectangle(left, top, right, bottom, outline="#dddddd")
        canvas.create_text(left, bottom + 12, text="old", anchor="w", fill="#666666")
        canvas.create_text(right, bottom + 12, text="new", anchor="e", fill="#666666")

        baseline_values = list(self.baseline_history[electrode])
        filtered_values = list(self.filtered_history[electrode])
        all_values = baseline_values + filtered_values
        if len(all_values) < 2:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Waiting for DATA...",
                fill="#999999",
            )
            return

        min_value = min(all_values)
        max_value = max(all_values)
        if min_value == max_value:
            min_value -= 1
            max_value += 1

        def y_from_value(value: float) -> float:
            ratio = (value - min_value) / (max_value - min_value)
            return bottom - ratio * (bottom - top)

        def x_from_index(index: int, count: int) -> float:
            if count <= 1:
                return left
            return left + index * (right - left) / (count - 1)

        def draw_series(values: List[int], color: str) -> None:
            if len(values) < 2:
                return
            points: List[float] = []
            for idx, value in enumerate(values):
                points.extend((x_from_index(idx, len(values)), y_from_value(value)))
            canvas.create_line(*points, fill=color, width=2, smooth=False)

        draw_series(baseline_values, "#1f77b4")
        draw_series(filtered_values, "#d62728")

        canvas.create_text(left + 6, top + 8, text="baseline", anchor="w", fill="#1f77b4")
        canvas.create_text(left + 78, top + 8, text="filtered", anchor="w", fill="#d62728")
        canvas.create_text(left - 4, top, text=str(max_value), anchor="e", fill="#666666")
        canvas.create_text(left - 4, bottom, text=str(min_value), anchor="e", fill="#666666")

    def parse_data_frame(self, line: str) -> Optional[DataFrame]:
        parts = line.split(",")
        if len(parts) < 5 or parts[0] != "DATA":
            return None

        try:
            timestamp_ms = int(parts[1])
            mode = parts[2]
            touch_hex = parts[3]
            electrode_count = int(parts[4])
            values = parts[5:]
            electrodes = []
            for idx in range(0, len(values), 3):
                if idx + 2 >= len(values):
                    break
                baseline = int(values[idx])
                filtered = int(values[idx + 1])
                delta = int(values[idx + 2])
                electrodes.append((baseline, filtered, delta))
            return DataFrame(timestamp_ms, mode, touch_hex, electrode_count, electrodes)
        except ValueError:
            return None

    def update_from_data_frame(self, frame: DataFrame) -> None:
        self.mode_var.set(frame.mode)
        self.touch_var.set(frame.touch_hex)
        self.last_ts_var.set(str(frame.timestamp_ms))

        for electrode in range(DEFAULT_ELECTRODES):
            baseline_var, filtered_var, delta_var = self.table_rows[electrode]
            if electrode < len(frame.electrodes):
                baseline, filtered, delta = frame.electrodes[electrode]
                baseline_var.set(str(baseline))
                filtered_var.set(str(filtered))
                delta_var.set(f"{delta:+d}")
                self.baseline_history[electrode].append(baseline)
                self.filtered_history[electrode].append(filtered)
                baseline_var_value = self.calc_variance(self.baseline_history[electrode])
                filtered_var_value = self.calc_variance(self.filtered_history[electrode])
                baseline_text = "-" if baseline_var_value is None else f"{baseline_var_value:.2f}"
                filtered_text = "-" if filtered_var_value is None else f"{filtered_var_value:.2f}"
                self.plot_titles[electrode].set(
                    f"E{electrode} var(base)={baseline_text}  var(filtered)={filtered_text}"
                )
                self.draw_plot(electrode)
            else:
                baseline_var.set("-")
                filtered_var.set("-")
                delta_var.set("-")

    def process_events(self) -> None:
        while True:
            try:
                kind, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "line":
                self.append_log(payload)
                frame = self.parse_data_frame(payload)
                if frame is not None:
                    self.update_from_data_frame(frame)
            elif kind == "status":
                self.status_var.set(payload)
                if payload == "Disconnected":
                    self.connection_var.set("Not connected")
            elif kind == "error":
                self.status_var.set(payload)
                self.append_log(f"ERROR {payload}")
                self.connection_var.set("Not connected")

        self.root.after(50, self.process_events)

    def on_close(self) -> None:
        try:
            self.reader.close()
        finally:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = Mpr121DebugGui(root)
    if serial is None:
        app.append_log("pyserial not installed. Install with: pip install pyserial")
    root.mainloop()


if __name__ == "__main__":
    main()
