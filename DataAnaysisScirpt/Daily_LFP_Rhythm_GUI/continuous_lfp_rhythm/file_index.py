import datetime
import os
import re


def parse_filename_time_ms(fname):
    """
    Extract the PC-side timestamp embedded in the EDF file name.
    """
    match = re.search(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", fname)
    if not match:
        return 0.0
    try:
        dt = datetime.datetime.strptime(match.group(1), "%Y-%m-%d-%H-%M-%S")
        return dt.timestamp() * 1000.0
    except Exception:
        return 0.0


def _classify_edf_type(fname):
    if fname.endswith("lfp.edf"):
        return "mode0_lfp"
    if fname.endswith("sensor.edf"):
        return "sensor"
    if fname.endswith("LFP&ESA.edf"):
        return "mode3_lfp"
    if fname.endswith("raw_data.edf"):
        return "mode3_raw_data"
    if fname.endswith("mode3_raw.edf"):
        return "mode3_raw"
    return "unknown"


def _parse_hardware_end_ms_from_header(header):
    candidates = [header.get("recording_additional", ""), header.get("patientcode", "")]
    for remark in candidates:
        if not remark:
            continue
        match = re.search(r"timestamp\s*[:=]?\s*(\d+)", remark, re.IGNORECASE)
        if match:
            return float(match.group(1))
        matches = re.findall(r"(\d+)", remark)
        if matches:
            return float(matches[-1])
    return 0.0


def _emit_index_progress(progress_callback, stage, done, total=None, payload=None):
    if progress_callback is None:
        return
    progress_callback(stage, done, total, payload or {})


def index_files(mode0_dir=None, mode3_dir=None, progress_callback=None):
    """
    Scan Mode0 / Mode3 directories and return a sorted list of file records.
    """
    candidates = []
    dirs_to_scan = []
    if mode0_dir and os.path.exists(mode0_dir):
        dirs_to_scan.append(mode0_dir)
    if mode3_dir and os.path.exists(mode3_dir):
        dirs_to_scan.append(mode3_dir)

    scanned_dirs = 0
    for current_dir in dirs_to_scan:
        for dirpath, _, filenames in os.walk(current_dir):
            scanned_dirs += 1
            if scanned_dirs == 1 or scanned_dirs % 50 == 0:
                _emit_index_progress(
                    progress_callback,
                    "scan_dir",
                    scanned_dirs,
                    None,
                    {"dirpath": dirpath, "found": len(candidates)},
                )
            for fname in filenames:
                if not fname.endswith(".edf"):
                    continue
                rec_type = _classify_edf_type(fname)
                if rec_type == "unknown":
                    continue
                file_path = os.path.join(dirpath, fname)
                candidates.append(
                    {
                        "file_path": file_path,
                        "basename": fname,
                        "type": rec_type,
                        "filename_time_ms": parse_filename_time_ms(fname),
                        "hw_end_ms": 0.0,
                    }
                )
                if len(candidates) == 1 or len(candidates) % 25 == 0:
                    _emit_index_progress(
                        progress_callback,
                        "discover_file",
                        len(candidates),
                        None,
                        {"basename": fname, "found": len(candidates)},
                    )

    _emit_index_progress(
        progress_callback,
        "discover_done",
        len(candidates),
        len(candidates),
        {"directories": scanned_dirs},
    )

    if not candidates:
        return []

    return sorted(candidates, key=lambda item: item["filename_time_ms"])


def generate_file_coverage(records, progress_callback=None):
    """
    Enrich indexed file records with fs / n_samples / duration / aligned start_ts / end_ts.
    """
    if not records:
        return []

    enriched = []
    total_records = len(records)
    for idx, record in enumerate(records, start=1):
        hw_end_ms = float(record.get("hw_end_ms", 0.0) or 0.0)
        reader = None
        try:
            import pyedflib

            reader = pyedflib.EdfReader(record["file_path"])
            header = reader.getHeader()
            fs = float(reader.getSampleFrequency(0))
            n_samples = int(reader.getNSamples()[0])
            duration_s = n_samples / fs if fs > 0 else 0.0
            if hw_end_ms <= 0:
                hw_end_ms = _parse_hardware_end_ms_from_header(header)
        except Exception:
            fs, n_samples, duration_s = 1000.0, 0, 0.0
        finally:
            try:
                if reader is not None:
                    reader.close()
            except Exception:
                pass

        hw_start_ms = hw_end_ms - duration_s * 1000.0 if hw_end_ms > 0 else 0.0
        boot_offset = record["filename_time_ms"] - hw_start_ms if hw_start_ms > 0 else 0.0
        enriched.append(
            {
                **record,
                "hw_end_ms": hw_end_ms,
                "fs": fs,
                "n_samples": n_samples,
                "duration_s": duration_s,
                "hw_start_ms": hw_start_ms,
                "boot_offset": boot_offset,
                "hw_session_id": -1,
                "start_ts": 0.0,
                "end_ts": 0.0,
            }
        )
        if progress_callback is not None:
            progress_callback(idx, total_records, record)

    grouped_hw = {}
    for record in enriched:
        grouped_hw.setdefault(record["filename_time_ms"], []).append(record)
    for same_time_records in grouped_hw.values():
        valid_hw = [item["hw_end_ms"] for item in same_time_records if item["hw_end_ms"] > 0]
        if valid_hw:
            shared = valid_hw[0]
            for item in same_time_records:
                item["hw_end_ms"] = shared
                item["hw_start_ms"] = shared - item["duration_s"] * 1000.0
                item["boot_offset"] = (
                    item["filename_time_ms"] - item["hw_start_ms"] if item["hw_start_ms"] > 0 else 0.0
                )

    enriched.sort(key=lambda item: item["filename_time_ms"])
    session_id = 0
    last_valid_offset = None
    for item in enriched:
        if item["hw_start_ms"] <= 0:
            continue
        current_offset = item["boot_offset"]
        if last_valid_offset is not None and abs(current_offset - last_valid_offset) > 5000.0:
            session_id += 1
        item["hw_session_id"] = session_id
        last_valid_offset = current_offset

    session_offsets = {}
    for item in enriched:
        if item["hw_session_id"] == -1:
            continue
        session_offsets.setdefault(item["hw_session_id"], []).append(item["boot_offset"])

    for item in enriched:
        if item["hw_session_id"] != -1:
            offsets = session_offsets[item["hw_session_id"]]
            median_offset = sorted(offsets)[len(offsets) // 2]
            item["start_ts"] = item["hw_start_ms"] + median_offset
            item["end_ts"] = item["hw_end_ms"] + median_offset
        else:
            item["start_ts"] = item["filename_time_ms"]
            item["end_ts"] = item["filename_time_ms"] + item["duration_s"] * 1000.0

    return sorted(enriched, key=lambda item: item["start_ts"])
