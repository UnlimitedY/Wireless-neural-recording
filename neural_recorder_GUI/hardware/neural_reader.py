"""
数据处理流程：
1. Habits 数据不考虑其timestamp，每个trial开始的时候触发一个serial 信号，读取该信号出现的PC 时间记录为一个文件为trialnum + PC_time, 加上 Trial.txt 记录的状态和Tevent.txt记录的事件offset作为标准认知行为学时间
2. 神经信号数据 直接使用每一个包上传的时候的PC时间，通过预实验来构建时间延迟模型进行时间校正
(不再使用led 和摄像头时间，数据处理过于复杂)
"""

""" 
Imports
"""
# from asyncore import read
# from email import message
# import asyncio
# from aioserial import AioSerial
import serial
import time
import datetime
import threading
import binascii
import numpy as np
import logging
# import json
import multiprocessing
# from multiprocessing import Process, freeze_support
import collections as coll
import math
from PyQt6.QtCore import QThread, pyqtSignal, QObject
import re
import queue as _pyqueue
import os
try:
    from ..storage.shared_memory_payload import pack_for_queue, unpack_from_queue
except ImportError:
    from storage.shared_memory_payload import pack_for_queue, unpack_from_queue
try:
    from ..pipeline import RawFrame, SerialDataPipeline
except ImportError:
    try:
        from pipeline import RawFrame, SerialDataPipeline
    except ImportError:
        RawFrame = None
        SerialDataPipeline = None
try:
    from ..support.path_utils import build_daily_file_path
except ImportError:
    from support.path_utils import build_daily_file_path
try:
    from ..support.windows_runtime import apply_windows_process_role
except ImportError:
    from support.windows_runtime import apply_windows_process_role
try:
    from pyedflib import EdfWriter, FILETYPE_EDFPLUS
    EDF_AVAILABLE = True
except Exception:
    EDF_AVAILABLE = False

def save_process_main(queue):
    apply_windows_process_role("data_writer")
    mode0_raw = get_raw_data_container()
    mode0_sensors = get_events_data_container()
    mode3_raw = get_mode3_data_container()
    mode3_sensors = get_events_data_container()

    write_q: "_pyqueue.Queue" = _pyqueue.Queue()

    def writer_loop():
        while True:
            task = write_q.get()
            if task is None:
                break
            try:
                mode = task.get("mode")
                if mode == 0:
                    _process_mode0_save(task)
                elif mode == 1:
                    _process_mode1_save(task)
                elif mode == 2:
                    _process_mode2_save(task)
                elif mode == 3:
                    _process_mode3_save(task)
            except Exception as e:
                logging.error(f"Save Process Writer Error: {e}", exc_info=True)
                print(f"Save Process Writer Error: {e}")

    writer_thread = threading.Thread(target=writer_loop, daemon=True)
    writer_thread.start()

    def _calc_start_time(end_timestamp_ms: float, timestamps_ms: list, tail_ms: float):
        if not timestamps_ms:
            return datetime.datetime.fromtimestamp(end_timestamp_ms / 1000.0)
        try:
            duration_ms = float(timestamps_ms[-1]) - float(timestamps_ms[0]) + float(tail_ms)
        except Exception:
            duration_ms = float(tail_ms)
        start_timestamp_ms = float(end_timestamp_ms) - float(duration_ms)
        return datetime.datetime.fromtimestamp(start_timestamp_ms / 1000.0)

    def _finalize_miss_packets(raw_dict: dict, max_interval: float, ts_key: str = "TimeStamp", index_key: str = "PacketIndex", expected_step: int = 1):
        try:
            packet_indices = raw_dict.get(index_key, [])
            if packet_indices is not None and len(packet_indices) > 1:
                miss_idx, miss_num = _calc_miss_packets_from_indices(packet_indices, expected_step=expected_step)
                raw_dict["MissPacketsIndex"] = np.asarray(miss_idx, dtype=np.int64)
                raw_dict["MissPackets"] = float(miss_num)
                return
            raw_dict["MissPacketsIndex"] = np.asarray([], dtype=np.int64)
            raw_dict["MissPackets"] = 0
            logging.critical(f"PacketIndex unavailable in finalize miss packets: key={index_key}, size={0 if packet_indices is None else len(packet_indices)}")
        except Exception:
            raw_dict["MissPacketsIndex"] = []
            raw_dict["MissPackets"] = 0

    while True:
        packed_msg = queue.get()
        try:
            msg = unpack_from_queue(packed_msg)
        except FileNotFoundError as exc:
            logging.error(
                f"Save Process Queue Payload Missing: {exc}",
                exc_info=True,
            )
            print(f"Save Process Queue Payload Missing: {exc}")
            continue
        except Exception as exc:
            logging.error(
                f"Save Process Queue Decode Error: {exc}",
                exc_info=True,
            )
            print(f"Save Process Queue Decode Error: {exc}")
            continue
        if msg is None:
            break

        try:
            msg_type = msg.get("type")
        except Exception:
            msg_type = None

        if msg_type == "shutdown":
            break

        if msg_type == "append_chunk":
            mode = int(msg.get("mode", -1))
            if mode == 0:
                timestamps = msg.get("timestamps", [])
                channels = msg.get("channels", [])
                mand_channels = msg.get("mand_channels", [])
                raw_samples = msg.get("raw_samples", [])
                raw_channels = msg.get("raw_channels", [])
                raw_alignment = msg.get("raw_alignment", [])
                raw_alignment_gap_fills = msg.get("raw_alignment_gap_fills", [])
                mode0_quant_bits = msg.get("mode0_quant_bits", [])
                mode0_quant_full_scale_uv = msg.get("mode0_quant_full_scale_uv", [])
                sensor_masks = msg.get("sensor_masks", [])
                sensors = msg.get("sensors", {})
                update_flags = msg.get("update_flags", [])

                packet_indices = msg.get("packet_indices", [])
                mode0_raw["TimeStamp"].extend(timestamps)
                mode0_raw["PacketIndex"].extend(packet_indices)
                for i in range(min(16, len(channels))):
                    mode0_raw[f"Channel_{i}"].extend(channels[i])
                for i in range(min(16, len(mand_channels))):
                    mode0_raw[f"MAND_Channel_{i}"].extend(mand_channels[i])
                mode0_raw["Raw"].extend(raw_samples)
                mode0_raw["RawChannel"].extend(raw_channels)
                mode0_raw["Raw_alignment"].extend(raw_alignment)
                if isinstance(raw_alignment_gap_fills, list):
                    base_packets = max(0, len(mode0_raw["PacketIndex"]) - len(packet_indices))
                    for item in raw_alignment_gap_fills:
                        try:
                            pos, values = item
                            mode0_raw.setdefault("Raw_alignment_gap_fills", []).append((int(pos) + base_packets, list(values or [])))
                        except Exception:
                            continue
                if isinstance(mode0_quant_bits, list):
                    mode0_raw.setdefault("Mode0QuantBits", []).extend(mode0_quant_bits)
                if isinstance(mode0_quant_full_scale_uv, list):
                    mode0_raw.setdefault("Mode0QuantFullScaleUv", []).extend(mode0_quant_full_scale_uv)
                if isinstance(sensor_masks, list):
                    mode0_raw.setdefault("SensorMask", []).extend(sensor_masks)
                for k, v in sensors.items():
                    if k in mode0_sensors:
                        mode0_sensors[k].extend(v)
                mode0_sensors["UpdateFlag"].extend(update_flags)
                
                # Check for potential memory overflow (e.g. > 2 hours of data without flush)
                if len(mode0_raw["TimeStamp"]) > 7200000: # ~2 hours at 1000Hz
                     logging.warning(f"Memory Warning: Mode 0 buffer size {len(mode0_raw['TimeStamp'])} samples. Flush may be missing.")

            elif mode == 3:
                timestamps = msg.get("timestamps", [])
                lfp_channels = msg.get("lfp_channels", [])
                esa_channels = msg.get("esa_channels", [])
                raster_channels = msg.get("raster_channels", [])
                sensors = msg.get("sensors", {})
                update_flags = msg.get("update_flags", [])

                packet_indices = msg.get("packet_indices", [])
                mode3_raw["TimeStamp"].extend(timestamps)
                mode3_raw["PacketIndex"].extend(packet_indices)
                for i in range(min(16, len(lfp_channels))):
                    mode3_raw[f"Channel_{i}"].extend(lfp_channels[i])
                for i in range(min(16, len(esa_channels))):
                    mode3_raw[f"ESA_Channel_{i}"].extend(esa_channels[i])
                for i in range(min(16, len(raster_channels))):
                    mode3_raw[f"Raster_Channel_{i}"].extend(raster_channels[i])
                for k, v in sensors.items():
                    if k in mode3_sensors:
                        mode3_sensors[k].extend(v)
                mode3_sensors["UpdateFlag"].extend(update_flags)
                
                if len(mode3_raw["TimeStamp"]) > 7200000:
                     logging.warning(f"Memory Warning: Mode 3 buffer size {len(mode3_raw['TimeStamp'])} samples.")
            continue

        if msg_type == "append_mode3_encoded":
            timestamps = msg.get("timestamps", [])
            packet_indices = msg.get("packet_indices", [])
            packet_words_list = msg.get("packet_words", [])
            sensors = msg.get("sensors", {})
            update_flags = msg.get("update_flags", [])
            raw_data_per_packet_mode3 = int(msg.get("raw_data_per_packet_mode3", 2) or 2)
            dac_resolution = float(msg.get("dac_resolution", DEFAULT_DAC_RESOLUTION) or DEFAULT_DAC_RESOLUTION)

            mode3_raw["TimeStamp"].extend(timestamps)
            mode3_raw["PacketIndex"].extend(packet_indices)
            for packet_words in packet_words_list:
                lfp_channels, esa_channels, raster_channels = _decode_mode3_lfp_esa_packet_words(
                    packet_words,
                    raw_data_per_packet_mode3=raw_data_per_packet_mode3,
                    dac_resolution=dac_resolution,
                )
                for i in range(16):
                    mode3_raw[f"Channel_{i}"].extend(lfp_channels[i])
                    mode3_raw[f"ESA_Channel_{i}"].extend(esa_channels[i])
                    mode3_raw[f"Raster_Channel_{i}"].extend(raster_channels[i])
            for k, v in sensors.items():
                if k in mode3_sensors:
                    mode3_sensors[k].extend(v)
            mode3_sensors["UpdateFlag"].extend(update_flags)
            continue

        if msg_type == "append_mode3_raw":
            timestamps = msg.get("timestamps", [])
            raw_samples = msg.get("raw_samples", [])
            raw_channels = msg.get("raw_channels", [])
            raw_sizes = msg.get("raw_sizes", [])
            raw_alignment = msg.get("raw_alignment", [])
            raw_packet_indices = msg.get("packet_indices", [])

            mode3_raw["Raw_data"].extend(raw_samples)
            mode3_raw["Raw_timestamp"].extend(timestamps)
            mode3_raw["Raw_channel"].extend(raw_channels)
            mode3_raw["Raw_packet_sizes"].extend(raw_sizes)
            mode3_raw["Raw_alignment"].extend(raw_alignment)
            mode3_raw["Raw_packet_indices"].extend(raw_packet_indices)
            continue

        if msg_type == "append_mode3_raw_encoded":
            timestamps = msg.get("timestamps", [])
            raw_word_packets = msg.get("raw_word_packets", [])
            raw_channels = msg.get("raw_channels", [])
            raw_alignment = msg.get("raw_alignment", [])
            raw_packet_indices = msg.get("packet_indices", [])
            dac_resolution = float(msg.get("dac_resolution", DEFAULT_DAC_RESOLUTION) or DEFAULT_DAC_RESOLUTION)

            for packet_words in raw_word_packets:
                raw_samples = _decode_mode3_raw_sample_words(packet_words, dac_resolution=dac_resolution)
                mode3_raw["Raw_data"].extend(raw_samples)
                mode3_raw["Raw_packet_sizes"].append(len(raw_samples))
            mode3_raw["Raw_timestamp"].extend(timestamps)
            mode3_raw["Raw_channel"].extend(raw_channels)
            mode3_raw["Raw_alignment"].extend(raw_alignment)
            mode3_raw["Raw_packet_indices"].extend(raw_packet_indices)
            continue

        if msg_type == "flush":
            mode = int(msg.get("mode", -1))
            addr = msg.get("addr", "")
            end_timestamp = float(msg.get("end_timestamp", time.time() * 1000))
            params = msg.get("params", {})

            if mode == 0:
                raw_snapshot = mode0_raw
                sensors_snapshot = mode0_sensors
                mode0_raw = get_raw_data_container()
                mode0_sensors = get_events_data_container()

                _finalize_miss_packets(raw_snapshot, float(params.get("LFP_max_interval", 5)), index_key="PacketIndex", expected_step=1)
                start_time = _calc_start_time(end_timestamp, raw_snapshot.get("TimeStamp", []), 4.0)

                write_q.put({
                    "mode": 0,
                    "raw_data": raw_snapshot,
                    "sensors_data": sensors_snapshot,
                    "addr": addr,
                    "start_time": start_time,
                    "end_timestamp": end_timestamp,
                    "params": {
                        "LFP_max_interval": params.get("LFP_max_interval", 5),
                        "raw_data_per_packet_channel": params.get("raw_data_per_packet_channel", 4),
                        "sensor_name": params.get("sensor_name", []),
                        "sensor_fs": params.get("sensor_fs", 0),
                    },
                })

            elif mode == 3:
                raw_snapshot = mode3_raw
                sensors_snapshot = mode3_sensors
                mode3_raw = get_mode3_data_container()
                mode3_sensors = get_events_data_container()

                _finalize_miss_packets(raw_snapshot, float(params.get("mode3_max_interval", 3)), index_key="PacketIndex", expected_step=1)
                start_time = _calc_start_time(end_timestamp, raw_snapshot.get("TimeStamp", []), 2.0)

                write_q.put({
                    "mode": 3,
                    "raw_data": raw_snapshot,
                    "sensors_data": sensors_snapshot,
                    "addr": addr,
                    "start_time": start_time,
                    "end_timestamp": end_timestamp,
                    "params": {
                        "mode3_max_interval": params.get("mode3_max_interval", 3),
                        "raw_data_per_packet_mode3": params.get("raw_data_per_packet_mode3", 2),
                        "sensor_name": params.get("sensor_name", []),
                        "sensor_fs": params.get("sensor_fs", 0),
                        "mode3_raw_max_interval": params.get("mode3_raw_max_interval", 15),
                        "mode3_thresholds": params.get("mode3_thresholds", [DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * 16),
                    },
                })
            continue

        if isinstance(msg, dict) and "mode" in msg and "raw_data" in msg:
            write_q.put(msg)
            continue

    try:
        write_q.put(None)
        writer_thread.join(timeout=2.0)
    except Exception:
        pass

def _get_daily_dir(base_path, start_time):
    """
    Generate a daily subdirectory path based on the start_time.
    Creates the directory if it doesn't exist.
    """
    return build_daily_file_path(base_path, start_time)


def _process_mode0_save(task):
    raw_data = task['raw_data']
    sensors_data = task['sensors_data']
    addr = task['addr']
    start_time = task['start_time'] # datetime object
    params = task['params']
    
    # Extract params
    LFP_max_interval = params['LFP_max_interval']
    raw_data_per_packet_channel = params['raw_data_per_packet_channel']
    sensor_name = params['sensor_name']
    sensor_fs = params['sensor_fs']
    if isinstance(sensor_fs, (list, tuple, np.ndarray)):
        sensor_fs_list = [int(value or 1) for value in list(sensor_fs)]
    else:
        sensor_fs_list = [int(sensor_fs or 1)] * len(sensor_name)
    if len(sensor_fs_list) < len(sensor_name):
        pad_value = sensor_fs_list[-1] if sensor_fs_list else 1
        sensor_fs_list.extend([pad_value] * (len(sensor_name) - len(sensor_fs_list)))
    sensor_fs_list = sensor_fs_list[:len(sensor_name)]
    
    # Update addr to point to daily subdirectory
    addr = _get_daily_dir(addr, start_time)
    
    now_str = start_time.strftime('%Y-%m-%d-%H-%M-%S')
    mode0_quant_bits_values = [
        _normalize_mode0_quant_bits(value)
        for value in list(raw_data.get("Mode0QuantBits", []) or [])
    ]
    mode0_quant_fs_values = [
        _normalize_mode0_quant_full_scale_uv(value)
        for value in list(raw_data.get("Mode0QuantFullScaleUv", []) or [])
    ]
    mode0_quant_bits_for_file = (
        mode0_quant_bits_values[-1] if mode0_quant_bits_values else MODE0_QUANT_BITS_DEFAULT
    )
    mode0_quant_full_scale_for_file = (
        max(mode0_quant_fs_values) if mode0_quant_fs_values else MODE0_QUANT_FULL_SCALE_UV_DEFAULT
    )
    mode0_lfp_signed_min = -float(mode0_quant_full_scale_for_file)
    mode0_lfp_signed_max = float(mode0_quant_full_scale_for_file)
    mode0_raw_signed_min = float(MODE0_RAW_PHYSICAL_MIN)
    mode0_raw_signed_max = float(MODE0_RAW_PHYSICAL_MAX)

    # LFP/MAND/selected raw/sensors in one mixed-rate EDF.
    lfp_channels = [raw_data[f"Channel_{i}"] for i in range(16)]
    lfp_channels_filled = _interpolate_missing_packets_by_min(
        lfp_channels,
        raw_data["TimeStamp"],
        raw_data_per_packet_channel,
        LFP_max_interval,
        packet_indices=raw_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=mode0_lfp_signed_min
    )
    mand_channels = [raw_data[f"MAND_Channel_{i}"] for i in range(16)]
    mand_channels_filled = _interpolate_missing_packets_by_min(
        mand_channels,
        raw_data["TimeStamp"],
        1,
        LFP_max_interval,
        packet_indices=raw_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=0.0
    )
    raw_channel_filled = _interpolate_missing_packets_by_min(
        [raw_data.get("Raw", [])],
        raw_data["TimeStamp"],
        50,
        LFP_max_interval,
        packet_indices=raw_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=mode0_raw_signed_min
    )[0]
    raw_channel_index_filled = _interpolate_missing_packets_by_min(
        [raw_data.get("RawChannel", [])],
        raw_data["TimeStamp"],
        1,
        LFP_max_interval,
        packet_indices=raw_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=0.0
    )[0]
    raw_samples_for_alignment = list(raw_data.get("Raw", []) or [])
    raw_alignment = list(raw_data.get("Raw_alignment", []) or [])
    if len(raw_alignment) < len(raw_samples_for_alignment):
        raw_alignment.extend([0.0] * (len(raw_samples_for_alignment) - len(raw_alignment)))
    elif len(raw_alignment) > len(raw_samples_for_alignment):
        raw_alignment = raw_alignment[:len(raw_samples_for_alignment)]
    raw_alignment_filled = _interpolate_alignment_with_gap_fills(
        raw_alignment,
        raw_data["TimeStamp"],
        50,
        raw_data.get("PacketIndex", []),
        gap_fills=raw_data.get("Raw_alignment_gap_fills", []),
    )
    mode0_sensor_masks = list(raw_data.get("SensorMask", []) or sensors_data.get("UpdateFlag", []) or [])
    if not mode0_sensor_masks:
        mode0_sensor_masks = [0] * len(raw_data.get("TimeStamp", []) or [])
    sensor_mask_filled = _interpolate_missing_packets_by_min(
        [mode0_sensor_masks],
        raw_data["TimeStamp"],
        1,
        LFP_max_interval,
        packet_indices=raw_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=SENSOR_MASK_PACKET_LOSS,
    )[0]
    mode0_duration_candidates = []
    if lfp_channels_filled:
        mode0_duration_candidates.append(len(lfp_channels_filled[0]) / 1000.0)
    if mand_channels_filled:
        mode0_duration_candidates.append(len(mand_channels_filled[0]) / 250.0)
    if len(raw_channel_filled) > 0:
        mode0_duration_candidates.append(len(raw_channel_filled) / 12500.0)
    if len(raw_alignment_filled) > 0:
        mode0_duration_candidates.append(len(raw_alignment_filled) / 12500.0)
    mode0_target_duration = max(mode0_duration_candidates) if mode0_duration_candidates else None
    mode0_loss_intervals = _packet_loss_intervals_ms(
        raw_data.get("TimeStamp", []),
        raw_data.get("PacketIndex", []),
        expected_step=1,
        packet_duration_ms=1000.0 / 250.0,
    )
    sensor_channels = [sensors_data[k] for k in sensor_name]
    s_mins, s_maxs = _get_sensor_phys_ranges(sensor_name)
    s_dims = _get_sensor_dimensions(sensor_name)
    mode0_sensor_annotations = []
    sensor_tail_fill_by_label = {
        str(sensor_key): _last_numeric_value(sensor_values, 0.0)
        for sensor_key, sensor_values in zip(sensor_name, sensor_channels)
    }
    sensor_masks = list(raw_data.get("SensorMask", []) or mode0_sensor_masks or [])
    if isinstance(sensor_fs, (list, tuple, np.ndarray)):
        sensor_channels_filled = []
        for sensor_index, (sensor_key, sensor_values) in enumerate(zip(sensor_name, sensor_channels)):
            fs_value = sensor_fs_list[sensor_index] if sensor_index < len(sensor_fs_list) else 1
            fill_value = s_mins[sensor_index] if sensor_index < len(s_mins) else 0.0
            sensor_timestamps = _mode0_sensor_timestamps_from_masks(
                raw_data.get("TimeStamp", []),
                sensor_masks,
                sensor_key,
                len(sensor_values),
            )
            if sensor_timestamps:
                aligned_sensor, valid_count, start_offset = _align_sensor_samples_hold_last_to_neural_duration(
                    sensor_values,
                    sensor_timestamps,
                    raw_data.get("TimeStamp", []),
                    fs_value,
                    mode0_target_duration,
                    fill_value,
                    loss_intervals_ms=mode0_loss_intervals,
                )
                sensor_channels_filled.append(aligned_sensor)
                mode0_sensor_annotations.extend(_valid_sample_edf_annotation(
                    f"Mode0{sensor_key}",
                    [(sensor_key, valid_count)],
                ))
                mode0_sensor_annotations.append((
                    0.0,
                    0.0,
                    f"Mode0{sensor_key}StartOffsetSamples={int(start_offset)}",
                ))
            else:
                if len(sensor_values) == len(raw_data.get("TimeStamp", []) or []):
                    fallback_filled, valid_count, start_offset = _align_sensor_samples_hold_last_to_neural_duration(
                        sensor_values,
                        raw_data.get("TimeStamp", []),
                        raw_data.get("TimeStamp", []),
                        fs_value,
                        mode0_target_duration,
                        fill_value,
                        loss_intervals_ms=mode0_loss_intervals,
                    )
                    sensor_channels_filled.append(fallback_filled)
                    mode0_sensor_annotations.extend(_valid_sample_edf_annotation(
                        f"Mode0{sensor_key}",
                        [(sensor_key, valid_count)],
                    ))
                    mode0_sensor_annotations.append((
                        0.0,
                        0.0,
                        f"Mode0{sensor_key}StartOffsetSamples={int(start_offset)}",
                    ))
                else:
                    fallback_filled, valid_count, start_offset = _align_sensor_samples_hold_last_to_neural_duration(
                        sensor_values,
                        [],
                        raw_data.get("TimeStamp", []),
                        fs_value,
                        mode0_target_duration,
                        fill_value,
                        loss_intervals_ms=mode0_loss_intervals,
                    )
                    sensor_channels_filled.append(fallback_filled)
    else:
        sensor_channels_filled = _interpolate_missing_packets_by_min(
            sensor_channels,
            raw_data["TimeStamp"],
            1,
            LFP_max_interval,
            packet_indices=raw_data.get("PacketIndex", []),
            expected_index_step=1,
            physical_mins=s_mins
        )
    all_channels_filled = (
        list(lfp_channels_filled)
        + list(mand_channels_filled)
        + [raw_channel_filled, raw_channel_index_filled, raw_alignment_filled, sensor_mask_filled]
        + list(sensor_channels_filled)
    )
    lfp_labels = [f"Ch{i}" for i in range(16)]
    mand_labels = [f"MAND_Ch{i}" for i in range(16)]
    labels = lfp_labels + mand_labels + ["Raw_Selected", "Raw_Channel", "Alignment", "SensorMask"] + list(sensor_name)
    fs = [1000] * 16 + [250] * 16 + [12500, 250, 12500, 250] + sensor_fs_list
    physical_mins = (
        [mode0_lfp_signed_min] * 16
        + [0.0] * 16
        + [mode0_raw_signed_min, 0.0, 0.0, 0.0]
        + list(s_mins)
    )
    physical_maxs = (
        [mode0_lfp_signed_max] * 16
        + [1000.0] * 16
        + [mode0_raw_signed_max, 15.0, 65535.0, 65535.0]
        + list(s_maxs)
    )
    # Packet-loss gaps have already been filled with per-channel physical_min
    # above. EDF record padding is not packet loss; keep mask-like channels at
    # zero and hold sensor values at the last real received sample.
    tail_fill_values = _edf_tail_fill_values_hold_last(labels, sensor_tail_fill_by_label)
    dimensions = ["uV"] * 33 + ["index", "unit", "mask"] + list(s_dims)
    lfp_filename = addr[0:-4] + str(now_str) + "lfp.edf"
    
    End_ts = task.get('end_timestamp', 0.0)
    raw_ts = raw_data.get("TimeStamp", [])
    last_hw_ts = raw_ts[-1] if len(raw_ts) > 0 else 0
    lfp_annotations = [(0.0, 0.0, f"EndTimestamp(ms)={last_hw_ts}")]
    lfp_annotations.append((0.0, 0.0, "Mode0SensorMaskBits=1:accel;2:status;4:packet_loss"))
    lfp_annotations.extend(_valid_sample_edf_annotation(
        "Mode0",
        [(label, len(channel)) for label, channel in zip(labels, all_channels_filled)],
    ))
    lfp_annotations.extend(mode0_sensor_annotations)
    if mode0_quant_bits_values or mode0_quant_fs_values:
        if mode0_quant_bits_values:
            min_bits = min(mode0_quant_bits_values)
            max_bits = max(mode0_quant_bits_values)
            bits_text = f"{min_bits}" if min_bits == max_bits else f"{min_bits}-{max_bits}"
        else:
            bits_text = f"{mode0_quant_bits_for_file}"
        if mode0_quant_fs_values:
            min_fs = min(mode0_quant_fs_values)
            max_fs = max(mode0_quant_fs_values)
            fs_text = f"{min_fs:.0f}" if min_fs == max_fs else f"{min_fs:.0f}-{max_fs:.0f}"
        else:
            fs_text = f"{mode0_quant_full_scale_for_file:.0f}"
        lfp_annotations.append(
            (0.0, 0.0, f"M0Q_BITS={bits_text};LFPFSUV={fs_text};RAWFSUV={MODE0_RAW_QUANT_FULL_SCALE_UV:.0f}")
        )

    lfp_annotations.extend(_edf_tail_padding_annotations("Mode0", mode0_target_duration))

    _safe_write_edf(lfp_filename, all_channels_filled, labels, fs,
                    physical_min=physical_mins, physical_max=physical_maxs,
                    dimension=dimensions, annotations=lfp_annotations, starttime=start_time,
                    target_duration_seconds=mode0_target_duration,
                    tail_fill_value=tail_fill_values)


def _normalized_mode3_threshold_annotations(params):
    try:
        threshold_values = [float(value or 0.0) for value in list(params.get("mode3_thresholds", []) or [])[:16]]
    except Exception:
        threshold_values = []
    if len(threshold_values) < 16:
        threshold_values.extend([DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * (16 - len(threshold_values)))
    return threshold_values[:16]


def _mode3_threshold_edf_annotations(threshold_values):
    values = list(threshold_values or [])[:16]
    if len(values) < 16:
        values.extend([0.0] * (16 - len(values)))
    annotations = [(0.0, 0.0, "Mode3Threshold_ChannelSpace=rhd2132_channel_index")]
    for i in range(16):
        annotations.append((0.0, 0.0, f"Mode3Threshold_Ch{i}(uV)={float(values[i]):.2f}"))
    return annotations


def _process_mode3_save(task):
    esa_data = task['raw_data']
    sensors_data = task['sensors_data']
    addr = task['addr']
    start_time = task['start_time']
    params = task['params']
    
    mode3_max_interval = params['mode3_max_interval']
    raw_data_per_packet_mode3 = params['raw_data_per_packet_mode3']
    sensor_name = params['sensor_name']
    sensor_fs = params['sensor_fs']
    mode3_raw_max_interval = params['mode3_raw_max_interval']
    
    # Update addr to point to daily subdirectory
    addr = _get_daily_dir(addr, start_time)
    
    now_str = start_time.strftime('%Y-%m-%d-%H-%M-%S')
    
    # LFP & ESA
    lfp_channels = [esa_data[f"Channel_{i}"] for i in range(16)]
    esa_channels = [esa_data[f"ESA_Channel_{i}"] for i in range(16)]
    raster_channels = [esa_data[f"Raster_Channel_{i}"] for i in range(16)]
    
    all_channels = lfp_channels + esa_channels + raster_channels
    all_channel_mins = [-1000.0] * 32 + [0.0] * 16

    all_channels_filled = _interpolate_missing_packets_by_min(
        all_channels,
        esa_data["TimeStamp"],
        raw_data_per_packet_mode3,
        mode3_max_interval,
        packet_indices=esa_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=all_channel_mins
    )
    labels = [f"Ch{i}" for i in range(16)] + [f"ESA{i}" for i in range(16)] + [f"Raster{i}" for i in range(16)]
    fs = [1000] * len(all_channels)
    dims = ['uV'] * 32 + ['bool'] * 16
    phys_mins = [-1000.0] * 32 + [0.0] * 16
    phys_maxs = [1000.0] * 32 + [1.0] * 16
    mode3_filename = addr[0:-4] + str(now_str) + "LFP&ESA.edf"
    
    End_ts = task.get('end_timestamp', 0.0)
    raw_ts = esa_data.get("TimeStamp", [])
    last_hw_ts = raw_ts[-1] if len(raw_ts) > 0 else 0
    mode3_annotations = [(0.0, 0.0, f"EndTimestamp(ms)={last_hw_ts}")]
    mode3_annotations.extend(_packet_gap_edf_annotations(
        "Mode3",
        esa_data.get("PacketIndex", []),
        expected_step=1,
        packet_samples=raw_data_per_packet_mode3,
        sample_frequency=1000,
    ))
    # Thresholds are stored by mode3 raw/original channel index so EDF annotations
    # match the actual recorded channel numbering, even when Mode1 GUI control uses
    # a remapped physical-channel view.
    threshold_values = _normalized_mode3_threshold_annotations(params)
    mode3_annotations.extend(_mode3_threshold_edf_annotations(threshold_values))
    
    _safe_write_edf(mode3_filename, all_channels_filled, labels, fs, 
                    physical_min=phys_mins, physical_max=phys_maxs,
                    dimension=dims, annotations=mode3_annotations, starttime=start_time)
    
    # Sensors
    sensor_channels = [sensors_data[k] for k in sensor_name]
    s_mins, s_maxs = _get_sensor_phys_ranges(sensor_name)
    s_dims = _get_sensor_dimensions(sensor_name)
    
    sensor_channels_filled = _interpolate_missing_packets_by_min(
        sensor_channels,
        esa_data["TimeStamp"],
        1,
        mode3_max_interval,
        packet_indices=esa_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=s_mins
    )
    sensor_fs_list = [sensor_fs] * len(sensor_channels_filled)
    sensor_filename = addr[0:-4] + str(now_str) + "sensor.edf"
    sensor_annotations = [(0.0, 0.0, f"EndTimestamp(ms)={last_hw_ts}")]
    sensor_annotations.extend(_packet_gap_edf_annotations(
        "Mode3Sensor",
        esa_data.get("PacketIndex", []),
        expected_step=1,
        packet_samples=1,
        sample_frequency=sensor_fs,
    ))
    
    _safe_write_edf(sensor_filename, sensor_channels_filled, sensor_name, sensor_fs_list, 
                    physical_min=s_mins, physical_max=s_maxs,
                    dimension=s_dims, annotations=sensor_annotations, starttime=start_time)
    
    # Raw Data
    raw_samples = esa_data.get("Raw_data", [])
    raw_timestamps = esa_data.get("Raw_timestamp", [])
    raw_sizes = esa_data.get("Raw_packet_sizes", [])
    raw_ch_list = esa_data.get("Raw_channel", [])
    raw_alignment = esa_data.get("Raw_alignment", [])
    raw_packet_indices = esa_data.get("Raw_packet_indices", [])
    
    if len(raw_samples) > 0 and len(raw_timestamps) > 0:
        raw_filled = _interpolate_missing_packets_by_min_variable(
            raw_samples, raw_timestamps, raw_sizes, mode3_raw_max_interval,
            packet_indices=raw_packet_indices, expected_index_step=4
        )
        ch_broadcast = _broadcast_channel_to_samples_by_variable(
            raw_ch_list, raw_sizes, raw_timestamps, mode3_raw_max_interval, filler_value=-1000,
            packet_indices=raw_packet_indices, expected_index_step=4
        )
        alignment_filled = _interpolate_missing_packets_by_min_variable(
            raw_alignment, raw_timestamps, raw_sizes, mode3_raw_max_interval, filler_value=0,
            packet_indices=raw_packet_indices, expected_index_step=4
        )
        
        labels = ["RawData", "RawChannel", "Alignment"]
        fs = [12500, 12500, 12500]
        dims = ["uV", "index", "unit"]
        # Use expanded range for Alignment channel to handle large values
        # RawData: +/- 1000 uV
        # RawChannel: +/- 1000
        # Alignment: 0-65535 (16-bit unsigned mapped to 16-bit signed range)
        phys_mins = [-1000.0, -1000.0, 0.0]
        phys_maxs = [1000.0, 1000.0, 65535.0]
        
        mode3_raw_filename = addr[0:-4] + str(now_str) + "mode3_raw.edf"
        
        raw_ts = raw_timestamps
        last_hw_ts = raw_ts[-1] if len(raw_ts) > 0 else 0
        raw_annotations = [(0.0, 0.0, f"EndTimestamp(ms)={last_hw_ts}")]
        raw_annotations.extend(_packet_gap_edf_annotations(
            "Mode3Raw",
            raw_packet_indices,
            expected_step=4,
            sample_frequency=12500,
        ))
        raw_annotations.extend(_mode3_threshold_edf_annotations(threshold_values))
        
        _safe_write_edf(mode3_raw_filename, [raw_filled, ch_broadcast, alignment_filled], labels, fs, 
                        physical_min=phys_mins, physical_max=phys_maxs,
                        dimension=dims, annotations=raw_annotations, starttime=start_time)

def _process_mode1_save(task):
    ap_data = task['raw_data']
    sensors_data = task['sensors_data']
    addr = task['addr']
    start_time = task['start_time']
    params = task['params']
    
    Spike_max_interval = params['Spike_max_interval']
    sensor_name = params['sensor_name']
    sensor_fs = params['sensor_fs']
    
    # Update addr to point to daily subdirectory
    addr = _get_daily_dir(addr, start_time)
    
    now_str = start_time.strftime('%Y-%m-%d-%H-%M-%S')
    
    # Raw Data
    raw_samples = [ap_data["Raw_data"]]
    raw_samples_filled = _interpolate_missing_packets_by_min(
        raw_samples,
        ap_data["Raw_timestamp"],
        90,
        Spike_max_interval,
        packet_indices=ap_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=-1000.0 # Explicitly pass physical min for Raw channel
    )
    labels = ["Raw"]
    fs = [20833]
    mode1_filename = addr[0:-4] + str(now_str) + "mode1.edf"
    
    annotations = []
    ap_ts = ap_data.get("AP_timestamp", [])
    electrodes = ap_data.get("Electrode", [])
    n_ann = min(len(ap_ts), len(electrodes))
    for i in range(n_ann):
        onset_sec = float(ap_ts[i]) / 1000.0
        desc = f"Spike@Ch{electrodes[i]}"
        annotations.append((onset_sec, 0.0, desc))
    
    End_ts = task.get('end_timestamp', 0.0)
    last_hw_ts = ap_data['Raw_timestamp'][-1] if len(ap_data['Raw_timestamp'])>0 else 0
    annotations.insert(0, (0.0, 0.0, f"EndTimestamp(ms)={last_hw_ts}"))
    
    _safe_write_edf(mode1_filename, raw_samples_filled, labels, fs, 
                    dimension='uV', annotations=annotations, starttime=start_time)
    
    # Sensors
    sensor_channels = [sensors_data[k] for k in sensor_name]
    s_mins, s_maxs = _get_sensor_phys_ranges(sensor_name)
    s_dims = _get_sensor_dimensions(sensor_name)
    
    sensor_channels_filled = _interpolate_missing_packets_by_min(
        sensor_channels,
        ap_data["Raw_timestamp"],
        1,
        Spike_max_interval,
        packet_indices=ap_data.get("PacketIndex", []),
        expected_index_step=1,
        physical_mins=s_mins
    )
    sensor_fs_list = [sensor_fs] * len(sensor_channels_filled)
    sensor_filename = addr[0:-4] + str(now_str) + "sensor.edf"
    sensor_annotations = [(0.0, 0.0, f"EndTimestamp(ms)={last_hw_ts}")]
    
    _safe_write_edf(sensor_filename, sensor_channels_filled, sensor_name, sensor_fs_list, 
                    physical_min=s_mins, physical_max=s_maxs,
                    dimension=s_dims, annotations=sensor_annotations, starttime=start_time)

def _process_mode2_save(task):
    ap_lfp_data = task['raw_data']
    sensors_data = task.get('sensors_data', {})
    addr = task['addr']
    start_time = task['start_time']
    params = task['params']
    
    mode2_max_interval = params['mode2_max_interval']
    mode2_packet_samples = int(params.get('mode2_packet_samples', MODE2_V2_PACKET_SAMPLES))
    mode2_sample_frequency = float(params.get('mode2_sample_frequency', MODE2_V2_FS))
    is_mode2_v2 = (
        mode2_packet_samples == MODE2_V2_PACKET_SAMPLES
        and int(round(mode2_sample_frequency)) == int(MODE2_V2_FS)
    )
    mode2_physical_min = MODE2_V2_PHYSICAL_MIN_UV if is_mode2_v2 else -1000.0
    mode2_physical_max = MODE2_V2_PHYSICAL_MAX_UV if is_mode2_v2 else 1000.0
    sensor_name = list(params.get("sensor_name", MODE2_SENSOR_KEYS))
    sensor_fs_list = list(params.get("sensor_fs", MODE2_SENSOR_SAMPLE_RATES))
    sensor_timestamps = list(task.get("sensor_timestamps", []) or [])
    
    # Update addr to point to daily subdirectory
    addr = _get_daily_dir(addr, start_time)
    
    now_str = start_time.strftime('%Y-%m-%d-%H-%M-%S')
    
    channels = []
    labels = []
    for i in range(16):
        data_i = ap_lfp_data[f"Channel_{i}"]
        if len(data_i) > 0:
            channels.append(data_i)
            labels.append(f"Ch{i}")
            
    if len(channels) > 0:
        channels_filled = _interpolate_missing_packets_by_min(
            channels,
            ap_lfp_data["TimeStamp"],
            mode2_packet_samples,
            mode2_max_interval,
            packet_indices=ap_lfp_data.get("PacketIndex", []),
            expected_index_step=1,
            physical_mins=mode2_physical_min # Explicitly pass physical min for AP/LFP channels
        )
        raw_alignment = list(ap_lfp_data.get("Raw_alignment", []) or [])
        if raw_alignment:
            alignment_filled = _interpolate_alignment_with_gap_fills(
                raw_alignment,
                ap_lfp_data["TimeStamp"],
                mode2_packet_samples,
                ap_lfp_data.get("PacketIndex", []),
                gap_fills=ap_lfp_data.get("Raw_alignment_gap_fills", []),
            )
        else:
            first_len = len(channels_filled[0]) if channels_filled else 0
            alignment_filled = np.zeros(first_len, dtype=np.float64)
        mode2_sensor_mask_filled = _expand_packet_mask_to_sample_timeline(
            ap_lfp_data.get("SensorMask", []),
            ap_lfp_data.get("PacketIndex", []),
            mode2_packet_samples,
            expected_step=1,
            missing_fill_value=SENSOR_MASK_PACKET_LOSS,
        )
        if mode2_sensor_mask_filled.size <= 0:
            first_len = len(channels_filled[0]) if channels_filled else len(alignment_filled)
            mode2_sensor_mask_filled = np.zeros(first_len, dtype=np.float64)
        all_channels = list(channels_filled) + [alignment_filled, mode2_sensor_mask_filled]
        labels = labels + ["Alignment", "SensorMask"]
        fs = [mode2_sample_frequency] * len(all_channels)
        physical_mins = [mode2_physical_min] * len(channels_filled) + [0.0, 0.0]
        physical_maxs = [mode2_physical_max] * len(channels_filled) + [65535.0, 65535.0]
        dimensions = ["uV"] * len(channels_filled) + ["unit", "mask"]
        mode2_filename = addr[0:-4] + str(now_str) + "AP_LFP_Raw_data.edf"

        End_ts = task.get('end_timestamp', 0.0)
        raw_ts = ap_lfp_data.get("TimeStamp", [])
        last_hw_ts = raw_ts[-1] if len(raw_ts) > 0 else 0
        mode2_annotations = [(0.0, 0.0, f"EndTimestamp(ms)={last_hw_ts}")]
        mode2_annotations.append((0.0, 0.0, "Mode2SensorMaskBits=1:accel;4:packet_loss"))
        mode2_annotations.extend(_valid_sample_edf_annotation(
            "Mode2",
            [(label, len(channel)) for label, channel in zip(labels, all_channels)],
        ))
        mode2_annotations.extend(_packet_gap_edf_annotations(
            "Mode2",
            ap_lfp_data.get("PacketIndex", []),
            expected_step=1,
            packet_samples=mode2_packet_samples,
            sample_frequency=mode2_sample_frequency,
        ))
        mode2_annotations.extend(_valid_sample_edf_annotation(
            "Mode2SensorMask",
            [("SensorMask", len(mode2_sensor_mask_filled))],
        ))
        if sensor_name:
            sensor_first_ts = sensor_timestamps[0] if sensor_timestamps else ""
            sensor_last_ts = sensor_timestamps[-1] if sensor_timestamps else ""
            mode2_annotations.append((
                0.0,
                0.0,
                f"Mode2SensorTiming;FirstTimestampMs={sensor_first_ts};LastTimestampMs={sensor_last_ts}",
            ))
        mode2_duration_candidates = [
            len(channel) / float(mode2_sample_frequency)
            for channel in list(channels_filled) + [alignment_filled]
            if len(channel) > 0 and float(mode2_sample_frequency) > 0.0
        ]
        mode2_target_duration = max(mode2_duration_candidates) if mode2_duration_candidates else None
        mode2_packet_duration_ms = 1000.0 * float(mode2_packet_samples) / max(1.0, float(mode2_sample_frequency))
        mode2_loss_intervals = _packet_loss_intervals_ms(
            raw_ts,
            ap_lfp_data.get("PacketIndex", []),
            expected_step=1,
            packet_duration_ms=mode2_packet_duration_ms,
        )
        sensor_tail_fill_by_label = {}
        for sensor_index, sensor_key in enumerate(sensor_name):
            sensor_values = sensors_data.get(sensor_key, []) if isinstance(sensors_data, dict) else []
            sensor_tail_fill_by_label[str(sensor_key)] = _last_numeric_value(sensor_values, 0.0)
            sensor_fs = sensor_fs_list[sensor_index] if sensor_index < len(sensor_fs_list) else MODE2_SENSOR_SAMPLE_RATES[0]
            aligned_sensor, valid_count, start_offset = _align_sensor_samples_hold_last_to_neural_duration(
                sensor_values,
                sensor_timestamps,
                raw_ts,
                sensor_fs,
                mode2_target_duration,
                MODE2_ACCEL_PHYSICAL_MIN,
                loss_intervals_ms=mode2_loss_intervals,
            )
            if aligned_sensor.size <= 0:
                continue
            all_channels.append(aligned_sensor)
            labels.append(sensor_key)
            fs.append(int(sensor_fs))
            physical_mins.append(MODE2_ACCEL_PHYSICAL_MIN)
            physical_maxs.append(MODE2_ACCEL_PHYSICAL_MAX)
            dimensions.append("g")
            mode2_annotations.extend(_valid_sample_edf_annotation(
                f"Mode2{sensor_key}",
                [(sensor_key, valid_count)],
            ))
            mode2_annotations.append((
                0.0,
                0.0,
                f"Mode2{sensor_key}StartOffsetSamples={int(start_offset)}",
            ))

        mode2_annotations.extend(_edf_tail_padding_annotations("Mode2", mode2_target_duration))
        # Packet-loss gaps have already been filled with per-channel physical_min
        # above. EDF record padding is not packet loss; keep mask-like channels at
        # zero and hold sensor values at the last real received sample.
        tail_fill_values = _edf_tail_fill_values_hold_last(labels, sensor_tail_fill_by_label)

        _safe_write_edf(mode2_filename, all_channels, labels, fs,
                        physical_min=physical_mins,
                        physical_max=physical_maxs,
                        dimension=dimensions, annotations=mode2_annotations, starttime=start_time,
                        target_duration_seconds=mode2_target_duration,
                        tail_fill_value=tail_fill_values)

def _get_sensor_phys_ranges(sensor_names):
    p_mins = []
    p_maxs = []
    for name in sensor_names:
        name_text = str(name)
        if "Gryo" in name_text or "Gyro" in name_text:
            p_mins.append(LSM6DS3_GYRO_PHYSICAL_MIN_DPS)
            p_maxs.append(LSM6DS3_GYRO_PHYSICAL_MAX_DPS)
        elif "Accl" in name_text:
            p_mins.append(LSM6DS3_ACCEL_PHYSICAL_MIN_G)
            p_maxs.append(LSM6DS3_ACCEL_PHYSICAL_MAX_G)
        else:
            p_mins.append(-5000.0)
            p_maxs.append(5000.0)
    return p_mins, p_maxs

def _get_sensor_dimensions(sensor_names):
    dims = []
    for name in sensor_names:
        name_text = str(name)
        if "Accl" in name_text:
            dims.append("g")
        elif "Gryo" in name_text or "Gyro" in name_text:
            dims.append("dps")
        else:
            dims.append("unit")
    return dims


def _last_numeric_value(values, default=0.0):
    for value in reversed(list(values or [])):
        try:
            numeric = float(value)
        except Exception:
            continue
        if np.isfinite(numeric):
            return numeric
    return float(default)


def _edf_tail_fill_values_hold_last(labels, hold_last_by_label):
    tail_values = [0.0] * len(labels)
    if not hold_last_by_label:
        return tail_values
    for index, label in enumerate(labels):
        key = str(label)
        if key in hold_last_by_label:
            tail_values[index] = float(hold_last_by_label[key])
    return tail_values

def _safe_write_edf(
    filename,
    channel_arrays,
    labels,
    sample_rates,
    physical_min=-1000.0,
    physical_max=1000.0,
    dimension='uV',
    annotations=None,
    starttime=None,
    target_duration_seconds=None,
    tail_fill_value=None,
):
    """Write signals to an EDF/EDF+ file using pyedflib.
    Falls back by raising if pyedflib is unavailable so caller can handle.

    - channel_arrays: list of 1D arrays, one per channel
    - labels: list of string labels for channels
    - sample_rates: list of ints (same length as channel_arrays)
    - annotations: optional list of tuples (onset_seconds, duration_seconds, description)
    - starttime: datetime object for the file start time
    
    注意： edf 文件按 秒来对齐数据，所有每一个edf文件最后都会补充一段0值来对齐到下一秒，在后续数据处理过程中需要裁切一下，
    注意： edf文件精度为16位，其精度和设定的physical min和max有关，对于-1000到1000的范围，精度为0.0078125uV
    """
    if not EDF_AVAILABLE:
        # raise RuntimeError('pyedflib is not available')
        print('pyedflib is not available')
        return

    n_channels = len(channel_arrays)
    signal_headers = []
    normalized_sample_rates = []
    physical_mins_for_fill = []
    tail_values_for_fill = []
    # Build headers per channel
    for i in range(n_channels):
        sr = max(1, int(sample_rates[i]))
        label = labels[i] if i < len(labels) else f"Ch{i}"
        # Allow per-channel dimensions via list/tuple
        dim = dimension[i] if isinstance(dimension, (list, tuple)) and i < len(dimension) else dimension
        
        # Allow per-channel physical limits via list/tuple
        p_min = physical_min[i] if isinstance(physical_min, (list, tuple)) and i < len(physical_min) else physical_min
        p_max = physical_max[i] if isinstance(physical_max, (list, tuple)) and i < len(physical_max) else physical_max
        normalized_sample_rates.append(sr)
        physical_mins_for_fill.append(float(p_min))
        if isinstance(tail_fill_value, (list, tuple)) and i < len(tail_fill_value):
            tail_fill = tail_fill_value[i]
        elif tail_fill_value is None:
            tail_fill = p_min
        else:
            tail_fill = tail_fill_value
        try:
            tail_values_for_fill.append(float(tail_fill))
        except Exception:
            tail_values_for_fill.append(float(p_min))
        
        sh = {
            'label': label,
            'dimension': dim,
            'sample_frequency': sr,
            'physical_min': float(p_min),
            'physical_max': float(p_max),
            'digital_min': -32768,
            'digital_max': 32767,
            'transducer': '',
            'prefilter': ''
        }
        signal_headers.append(sh)

    # Writer
    if starttime is None:
        starttime = datetime.datetime.now()

    writer = EdfWriter(filename, n_channels=n_channels, file_type=FILETYPE_EDFPLUS)
    if annotations and hasattr(writer, "set_number_of_annotation_signals"):
        try:
            writer.set_number_of_annotation_signals(_edf_annotation_signal_count(annotations))
        except Exception:
            pass
    writer.setStartdatetime(starttime)
    writer.setSignalHeaders(signal_headers)

    # Convert arrays to float64. All channels are padded to a shared duration
    # rather than trimmed, so a shorter channel cannot silently discard data
    # from another channel in the same EDF.
    samples = []
    same_sample_rate = len(set(normalized_sample_rates)) <= 1
    for arr in channel_arrays:
        a = np.asarray(arr, dtype=np.float64)
        samples.append(a)
    if same_sample_rate:
        if target_duration_seconds is not None:
            common_duration = max(0.0, float(target_duration_seconds))
            sr = normalized_sample_rates[0] if normalized_sample_rates else 1
            common_records = int(math.ceil(common_duration)) if common_duration > 0.0 else 0
            if common_records <= 0 and any(len(sample) > 0 for sample in samples):
                common_records = 1
            target_len = common_records * sr
            aligned_samples = []
            for i, a in enumerate(samples):
                fill_value = physical_mins_for_fill[i] if i < len(physical_mins_for_fill) else 0.0
                fill_value = tail_values_for_fill[i] if i < len(tail_values_for_fill) else fill_value
                if len(a) < target_len:
                    a = np.pad(a, (0, target_len - len(a)), mode="constant", constant_values=fill_value)
                elif len(a) > target_len:
                    a = a[:target_len]
                aligned_samples.append(a)
            samples = aligned_samples
            writeable = target_len > 0
        else:
            # Pad equal-rate channels to the longest complete EDF datarecord
            # instead of trimming shorter channels.
            sr = normalized_sample_rates[0] if normalized_sample_rates else 1
            max_len = max((len(a) for a in samples), default=0)
            common_records = int(math.ceil(float(max_len) / float(sr))) if max_len > 0 else 0
            target_len = common_records * sr
            aligned_samples = []
            for i, a in enumerate(samples):
                fill_value = physical_mins_for_fill[i] if i < len(physical_mins_for_fill) else 0.0
                fill_value = tail_values_for_fill[i] if i < len(tail_values_for_fill) else fill_value
                if len(a) < target_len:
                    a = np.pad(a, (0, target_len - len(a)), mode="constant", constant_values=fill_value)
                elif len(a) > target_len:
                    a = a[:target_len]
                aligned_samples.append(a)
            samples = aligned_samples
            writeable = target_len > 0
    else:
        if target_duration_seconds is not None:
            common_duration = max(0.0, float(target_duration_seconds))
        else:
            durations = [
                float(len(a)) / float(normalized_sample_rates[i])
                for i, a in enumerate(samples)
                if i < len(normalized_sample_rates) and len(a) > 0
            ]
            common_duration = max(durations) if durations else 0.0
        common_records = int(math.ceil(common_duration)) if common_duration > 0.0 else 0
        if common_records <= 0 and any(len(sample) > 0 for sample in samples):
            common_records = 1
        aligned_samples = []
        for i, a in enumerate(samples):
            sr = normalized_sample_rates[i] if i < len(normalized_sample_rates) else 1
            target_len = common_records * sr
            fill_value = physical_mins_for_fill[i] if i < len(physical_mins_for_fill) else 0.0
            fill_value = tail_values_for_fill[i] if i < len(tail_values_for_fill) else fill_value
            if len(a) < target_len:
                if target_len > len(a):
                    a = np.pad(a, (0, target_len - len(a)), mode="constant", constant_values=fill_value)
            elif len(a) > target_len:
                a = a[:target_len]
            aligned_samples.append(a)
        samples = aligned_samples
        writeable = common_records > 0 and any(len(a) > 0 for a in samples)

    if writeable:
        writer.writeSamples(samples)

    # Optional annotations
    if annotations:
        for onset, duration, desc in annotations:
            try:
                writer.writeAnnotation(float(onset), float(duration), str(desc))
            except Exception:
                # Continue without blocking if any annotation fails
                pass

    writer.close()

def _calc_missing_counts_from_indices(packet_indices, expected_step=1, max_missing_packets=4096):
    if packet_indices is None:
        return {}, 0
    try:
        n_packets = len(packet_indices)
    except Exception:
        packet_indices = list(packet_indices)
        n_packets = len(packet_indices)
    if n_packets <= 1:
        return {}, 0
    expected_step = _normalize_packet_expected_step(expected_step)
    missing_counts = {}
    missing_total = 0
    last_valid_idx = None
    last_valid_pos = 0
    for start_pos, value in enumerate(packet_indices):
        try:
            last_valid_idx = int(value) & 0xFFFF
            last_valid_pos = start_pos
            break
        except Exception:
            continue
    if last_valid_idx is None:
        return {}, 0
    for j in range(last_valid_pos + 1, n_packets):
        try:
            curr_idx = int(packet_indices[j]) & 0xFFFF
        except Exception:
            continue
        transition = _classify_packet_index_transition(
            last_valid_idx,
            curr_idx,
            expected_step=expected_step,
            max_missing_packets=max_missing_packets,
        )
        kind = str(transition.get("kind", "") or "")
        if kind == "out_of_order" or kind == "duplicate":
            continue
        if kind == "reset":
            last_valid_idx = curr_idx
            last_valid_pos = j
            continue
        delta = int(transition.get("delta", 0) or 0)
        if delta > 0 and (delta % expected_step) != 0:
            pass
            # logging.critical(f"Packet index stride mismatch: prev={prev_idx}, curr={curr_idx}, delta={delta}, expected_step={expected_step}")
        missing = max(0, int(transition.get("missing", 0) or 0))
        if missing > max_missing_packets:
            logging.critical(
                "Missing packet count exceeded cap: "
                f"missing={missing}, cap={max_missing_packets}, prev={last_valid_idx}, curr={curr_idx}, "
                f"step={expected_step}, wrapped={bool(transition.get('wrapped', False))}"
            )
            missing = max_missing_packets
        if missing > 0:
            missing_counts[last_valid_pos] = missing
            missing_total += missing
        last_valid_idx = curr_idx
        last_valid_pos = j
    return missing_counts, missing_total

def _calc_miss_packets_from_indices(packet_indices, expected_step=1, max_missing_packets=4096):
    missing_counts, _ = _calc_missing_counts_from_indices(packet_indices, expected_step=expected_step, max_missing_packets=max_missing_packets)
    if not missing_counts:
        return [], 0
    miss_idx = sorted(missing_counts.keys())
    miss_num = int(sum(missing_counts.values()))
    return miss_idx, miss_num


def _packet_gap_edf_annotations(label, packet_indices, expected_step=1, packet_samples=None, sample_frequency=None, max_preview=8):
    annotations = []
    metadata = []
    if packet_samples is not None:
        metadata.append(f"PacketSamples={int(packet_samples)}")
    if sample_frequency is not None:
        metadata.append(f"SampleFrequencyHz={float(sample_frequency):g}")
    if expected_step is not None:
        metadata.append(f"ExpectedPacketStep={int(_normalize_packet_expected_step(expected_step))}")
    if metadata:
        annotations.append((0.0, 0.0, f"{label}PacketLayout;" + ";".join(metadata)))

    packet_indices = list(packet_indices or [])
    missing_counts, missing_total = _calc_missing_counts_from_indices(
        packet_indices,
        expected_step=expected_step,
    )
    annotations.append((0.0, 0.0, f"{label}MissingPackets={int(missing_total)}"))
    if not missing_counts:
        return annotations

    gap_items = []
    for pos, missing in sorted(missing_counts.items())[:max(1, int(max_preview))]:
        try:
            prev_idx = int(packet_indices[pos]) & 0xFFFF
        except Exception:
            continue
        next_idx = None
        for next_pos in range(pos + 1, len(packet_indices)):
            try:
                next_idx = int(packet_indices[next_pos]) & 0xFFFF
                break
            except Exception:
                continue
        if next_idx is None:
            gap_items.append(f"{prev_idx}->?(+{int(missing)})")
        else:
            gap_items.append(f"{prev_idx}->{next_idx}(+{int(missing)})")
    if len(missing_counts) > len(gap_items):
        gap_items.append("...")
    if gap_items:
        annotations.append((0.0, 0.0, f"{label}MissingGaps=" + ",".join(gap_items)))
    return annotations


def _valid_sample_edf_annotation(label, valid_counts):
    parts = []
    for name, count in valid_counts:
        try:
            parts.append(f"{name}={int(count)}")
        except Exception:
            parts.append(f"{name}=0")
    if not parts:
        return []
    return [(0.0, 0.0, f"{label}ValidSamples;{part}") for part in parts]


def _edf_tail_padding_annotations(label, target_duration_seconds):
    if target_duration_seconds is None:
        return []
    try:
        valid_seconds = max(0.0, float(target_duration_seconds))
    except Exception:
        return []
    final_seconds = float(math.ceil(valid_seconds)) if valid_seconds > 0.0 else 0.0
    padding_seconds = max(0.0, final_seconds - valid_seconds)
    return [(
        valid_seconds,
        padding_seconds,
        f"{label}TailPadSec={padding_seconds:.6f}",
    )]


def _edf_annotation_signal_count(annotations):
    annotations = list(annotations or [])
    if not annotations:
        return 1
    per_record_counts = {}
    for item in annotations:
        try:
            onset = max(0.0, float(item[0]))
        except Exception:
            onset = 0.0
        record_index = int(math.floor(onset))
        per_record_counts[record_index] = per_record_counts.get(record_index, 0) + 1
    return max(1, min(64, max(per_record_counts.values(), default=1)))


def _packet_loss_intervals_ms(timestamps, packet_indices, expected_step=1, packet_duration_ms=1.0):
    timestamps = list(timestamps or [])
    packet_indices = list(packet_indices or [])
    usable_packets = min(len(timestamps), len(packet_indices))
    if usable_packets <= 1:
        return []
    try:
        packet_duration_ms = max(0.0, float(packet_duration_ms))
    except Exception:
        packet_duration_ms = 0.0
    missing_counts, _ = _calc_missing_counts_from_indices(
        packet_indices[:usable_packets],
        expected_step=expected_step,
    )
    intervals = []
    for packet_pos, missing in sorted(missing_counts.items()):
        if packet_pos >= usable_packets - 1:
            continue
        try:
            prev_ts = float(timestamps[packet_pos])
            next_ts = float(timestamps[packet_pos + 1])
        except Exception:
            continue
        if packet_duration_ms > 0.0:
            start_ms = prev_ts + packet_duration_ms
            end_ms = start_ms + packet_duration_ms * max(0, int(missing))
            if next_ts > start_ms:
                end_ms = min(end_ms, next_ts)
        else:
            start_ms = prev_ts
            end_ms = next_ts
        if end_ms > start_ms:
            intervals.append((start_ms, end_ms))
    return intervals


def _time_in_intervals_ms(timestamp_ms, intervals_ms):
    try:
        timestamp_value = float(timestamp_ms)
    except Exception:
        return False
    for start_ms, end_ms in list(intervals_ms or []):
        try:
            if float(start_ms) <= timestamp_value < float(end_ms):
                return True
        except Exception:
            continue
    return False


def _align_sensor_samples_hold_last_to_neural_duration(
    sensor_values,
    sensor_timestamps,
    neural_timestamps,
    sensor_fs,
    target_duration_seconds,
    loss_fill_value,
    loss_intervals_ms=None,
):
    values = [float(value) for value in list(sensor_values or [])]
    sensor_timestamps = list(sensor_timestamps or [])
    neural_timestamps = list(neural_timestamps or [])
    try:
        fs = max(1.0, float(sensor_fs))
    except Exception:
        fs = 1.0
    try:
        target_duration = max(0.0, float(target_duration_seconds or 0.0))
    except Exception:
        target_duration = 0.0

    if neural_timestamps:
        try:
            first_neural_ts = float(neural_timestamps[0])
        except Exception:
            first_neural_ts = 0.0
    elif sensor_timestamps:
        try:
            first_neural_ts = float(sensor_timestamps[0])
        except Exception:
            first_neural_ts = 0.0
    else:
        first_neural_ts = 0.0

    target_len = int(round(target_duration * fs)) if target_duration > 0.0 else 0
    timestamp_count = min(len(values), len(sensor_timestamps))
    if timestamp_count > 0:
        try:
            last_offset = int(round((float(sensor_timestamps[timestamp_count - 1]) - first_neural_ts) * fs / 1000.0)) + 1
            target_len = max(target_len, last_offset)
        except Exception:
            pass
    target_len = max(target_len, len(values))
    if target_len <= 0:
        return np.asarray([], dtype=np.float64), 0, 0

    if values:
        held_value = float(values[0])
    else:
        held_value = 0.0

    if timestamp_count > 0:
        events = []
        for timestamp, value in zip(sensor_timestamps[:timestamp_count], values[:timestamp_count]):
            try:
                events.append((float(timestamp), float(value)))
            except Exception:
                continue
        events.sort(key=lambda item: item[0])
    else:
        events = [
            (first_neural_ts + 1000.0 * idx / fs, float(value))
            for idx, value in enumerate(values)
        ]

    if events:
        start_offset_samples = int(round((events[0][0] - first_neural_ts) * fs / 1000.0))
        held_value = events[0][1]
    else:
        start_offset_samples = 0

    timeline = []
    event_index = 0
    epsilon_ms = 0.5 * 1000.0 / fs
    for sample_index in range(target_len):
        sample_ts = first_neural_ts + 1000.0 * sample_index / fs
        while event_index < len(events) and events[event_index][0] <= sample_ts + epsilon_ms:
            held_value = events[event_index][1]
            event_index += 1
        if _time_in_intervals_ms(sample_ts, loss_intervals_ms):
            timeline.append(float(loss_fill_value))
        else:
            timeline.append(float(held_value))
    return np.asarray(timeline, dtype=np.float64), int(len(values)), int(start_offset_samples)


def _align_sensor_samples_to_neural_start(
    sensor_values,
    sensor_timestamps,
    neural_timestamps,
    sensor_fs,
    fill_value,
):
    values = list(sensor_values or [])
    sensor_timestamps = list(sensor_timestamps or [])
    neural_timestamps = list(neural_timestamps or [])
    if not values:
        return np.asarray([], dtype=np.float64), 0, 0
    if not sensor_timestamps or not neural_timestamps:
        return np.asarray(values, dtype=np.float64), int(len(values)), 0
    try:
        first_sensor_ts = float(sensor_timestamps[0])
        first_neural_ts = float(neural_timestamps[0])
        fs = max(1.0, float(sensor_fs))
    except Exception:
        return np.asarray(values, dtype=np.float64), int(len(values)), 0
    start_offset_samples = int(round((first_sensor_ts - first_neural_ts) * fs / 1000.0))
    trimmed_timestamps = sensor_timestamps[:len(values)]
    if start_offset_samples > 0:
        aligned = [float(fill_value)] * start_offset_samples
    if start_offset_samples < 0:
        trim = min(len(values), abs(start_offset_samples))
        values = values[trim:]
        trimmed_timestamps = trimmed_timestamps[trim:]
        aligned = []
    elif start_offset_samples == 0:
        aligned = []
    valid_count = int(len(values))
    for idx, value in enumerate(values):
        aligned.append(value)
        if idx + 1 >= len(values) or idx + 1 >= len(trimmed_timestamps):
            continue
        try:
            curr_ts = float(trimmed_timestamps[idx])
            next_ts = float(trimmed_timestamps[idx + 1])
            missing_samples = max(0, int(round((next_ts - curr_ts) * fs / 1000.0)) - 1)
        except Exception:
            missing_samples = 0
        if missing_samples > 0:
            aligned.extend([float(fill_value)] * missing_samples)
    return np.asarray(aligned, dtype=np.float64), valid_count, int(start_offset_samples)


def _mode0_sensor_mask_bit(sensor_name):
    name_text = str(sensor_name or "")
    if name_text in {"AcclX", "AcclY", "AcclZ"}:
        return 0x01
    if name_text in {"RSOC", "Battery_STAT", "Battery_voltage"}:
        return 0x02
    return 0x00


def _mode0_sensor_timestamps_from_masks(raw_timestamps, sensor_masks, sensor_name, value_count):
    mask_bit = _mode0_sensor_mask_bit(sensor_name)
    if mask_bit == 0:
        return []
    raw_timestamps = list(raw_timestamps or [])
    sensor_masks = list(sensor_masks or [])
    if not raw_timestamps or len(sensor_masks) < len(raw_timestamps):
        return []
    sensor_timestamps = []
    for timestamp, mask in zip(raw_timestamps, sensor_masks):
        try:
            mask_value = int(mask)
        except Exception:
            mask_value = 0
        if mask_value & mask_bit:
            sensor_timestamps.append(timestamp)
            if len(sensor_timestamps) >= int(value_count):
                break
    if len(sensor_timestamps) != int(value_count):
        return []
    return sensor_timestamps


def _expand_packet_mask_to_sample_timeline(packet_masks, packet_indices, packet_samples, expected_step=1, missing_fill_value=0.0):
    masks = list(packet_masks or [])
    if not masks:
        return np.asarray([], dtype=np.float64)
    packet_samples = max(1, int(packet_samples or 1))
    indices = list(packet_indices or [])
    usable_packets = min(len(masks), len(indices)) if indices else len(masks)
    if usable_packets <= 0:
        return np.asarray([], dtype=np.float64)
    missing_counts = {}
    if indices and len(indices) >= usable_packets:
        missing_counts, _ = _calc_missing_counts_from_indices(
            indices[:usable_packets],
            expected_step=expected_step,
        )
    expanded = []
    for packet_pos in range(usable_packets):
        try:
            mask_value = float(int(masks[packet_pos]) & 0xFFFF)
        except Exception:
            mask_value = 0.0
        expanded.extend([mask_value] * packet_samples)
        if packet_pos in missing_counts and packet_pos < usable_packets - 1:
            expanded.extend([float(missing_fill_value)] * (int(missing_counts[packet_pos]) * packet_samples))
    return np.asarray(expanded, dtype=np.float64)


def _count_missing_from_indices(packet_indices, expected_step=1, max_missing_packets=4096):
    packet_indices = list(packet_indices or [])
    if not packet_indices:
        return 0, 0
    _, miss_num = _calc_miss_packets_from_indices(
        packet_indices,
        expected_step=expected_step,
        max_missing_packets=max_missing_packets,
    )
    return int(miss_num), int(len(packet_indices))


def split_wireless_serial_packets(read_data):
    """Split one completed relay frame into legacy hex packet strings."""
    if not read_data:
        return []
    hex_frame = str(binascii.b2a_hex(read_data, ' ', 2))
    return re.split('[ ][2][1][2][2][ ][2][3][2][4][ ]', hex_frame[2:-10])[0:-1]

def _interpolate_missing_packets_by_min(channel_arrays, timestamps, packet_samples, max_interval, physical_mins=None, packet_indices=None, expected_index_step=1):
    """Insert filler samples for detected missing packets using a fixed minimum value 0.0.
    Returns new arrays suitable for EDF writing.
    """
    if not isinstance(channel_arrays, (list, tuple)):
        return channel_arrays
    n_packets_ts = len(timestamps)
    chunks_per_channel = []
    
    # Handle physical mins
    n_channels = len(channel_arrays)
    if physical_mins is None:
        mins_per_channel = [-1000] * n_channels
    elif isinstance(physical_mins, (int, float)):
        mins_per_channel = [physical_mins] * n_channels
    elif isinstance(physical_mins, (list, tuple)):
        if len(physical_mins) >= n_channels:
            mins_per_channel = physical_mins[:n_channels]
        else:
            mins_per_channel = list(physical_mins) + [-1000] * (n_channels - len(physical_mins))
    else:
        mins_per_channel = [-1000] * n_channels
        
    usable_n_packets = n_packets_ts
    for i, arr in enumerate(channel_arrays):
        a = list(arr)
        n_pack_arr = (len(a) // packet_samples) if packet_samples > 0 else 0
        usable_n_packets = min(usable_n_packets, n_pack_arr)
        chunks = [a[i*packet_samples:(i+1)*packet_samples] for i in range(n_pack_arr)]
        chunks_per_channel.append(chunks)

    if usable_n_packets <= 1:
        return [np.asarray(arr, dtype=np.float64) for arr in channel_arrays]

    missing_counts = {}
    if packet_indices is not None and len(packet_indices) >= usable_n_packets:
        packet_indices = list(packet_indices)[:usable_n_packets]
        missing_counts, _ = _calc_missing_counts_from_indices(packet_indices, expected_step=expected_index_step)
    else:
        logging.critical(f"PacketIndex unavailable for interpolation: usable_n_packets={usable_n_packets}, provided={0 if packet_indices is None else len(packet_indices)}")

    filled_arrays = [list() for _ in channel_arrays]
    for j in range(usable_n_packets):
        for ch_idx in range(len(channel_arrays)):
            filled_arrays[ch_idx].extend(chunks_per_channel[ch_idx][j])
        # Fill gaps between received packets, including the penultimate->last
        # transition, but never append filler after the final received packet.
        if j in missing_counts and j < (usable_n_packets - 1):
            filler_packets = missing_counts[j]
            filler_len = filler_packets * packet_samples
            for ch_idx in range(len(channel_arrays)):
                filled_arrays[ch_idx].extend([mins_per_channel[ch_idx]] * filler_len)

    # 不再追加超出usable_n_packets的额外数据块，避免文件尾部长度超过当前时间戳对应的包范围

    return [np.asarray(a, dtype=np.float64) for a in filled_arrays]


def _interpolate_alignment_with_gap_fills(raw_alignment, timestamps, packet_samples, packet_indices, gap_fills=None):
    raw_alignment = list(raw_alignment or [])
    packet_samples = max(1, int(packet_samples or 1))
    n_packets = min(len(timestamps or []), len(raw_alignment) // packet_samples)
    if n_packets <= 0:
        return np.asarray(raw_alignment, dtype=np.float64)
    gap_fill_map = {}
    for item in list(gap_fills or []):
        try:
            pos, values = item
            gap_fill_map[int(pos)] = list(values or [])
        except Exception:
            continue
    missing_counts, _ = _calc_missing_counts_from_indices(
        list(packet_indices or [])[:n_packets],
        expected_step=1,
    )
    filled = []
    for packet_pos in range(n_packets):
        start = packet_pos * packet_samples
        filled.extend(raw_alignment[start:start + packet_samples])
        if packet_pos in missing_counts and packet_pos < (n_packets - 1):
            filler_len = int(missing_counts[packet_pos]) * packet_samples
            custom_fill = gap_fill_map.get(packet_pos, [])
            if len(custom_fill) < filler_len:
                custom_fill = list(custom_fill) + [0.0] * (filler_len - len(custom_fill))
            filled.extend(custom_fill[:filler_len])
    return np.asarray(filled, dtype=np.float64)

def _interpolate_missing_packets_by_min_variable(raw_array, timestamps, packet_sizes, max_interval, filler_value=-1000, packet_indices=None, expected_index_step=1):
    """Interpolate missing packets for a single-channel array where packet sizes may vary.
    - raw_array: flat list/array of samples concatenated across packets
    - timestamps: list of per-packet timestamps (ms)
    - packet_sizes: list of per-packet sample counts
    - max_interval: threshold (ms) above which a gap indicates missing packets
    Returns a single numpy array with filler_value inserted for missing packets.
    """
    import numpy as np
    if raw_array is None or timestamps is None:
        return np.asarray([], dtype=np.float64)
    raw = list(raw_array)
    n_packets = len(timestamps)
    if n_packets == 0:
        return np.asarray(raw, dtype=np.float64)
    # Ensure packet_sizes length; if missing, assume constant size from average
    if not packet_sizes or len(packet_sizes) != n_packets:
        avg_size = int(round(len(raw) / max(1, n_packets)))
        packet_sizes = [avg_size] * n_packets
    # Reconstruct per-packet chunks
    chunks = []
    idx = 0
    for sz in packet_sizes:
        chunks.append(raw[idx:idx+sz])
        idx += sz
    # Detect missing counts
    if packet_indices is not None and len(packet_indices) >= n_packets:
        packet_indices = list(packet_indices)[:n_packets]
        missing_counts, _ = _calc_missing_counts_from_indices(packet_indices, expected_step=expected_index_step)
    else:
        missing_counts = {}
        logging.critical(f"PacketIndex unavailable for variable interpolation: n_packets={n_packets}, provided={0 if packet_indices is None else len(packet_indices)}")
    # Filler size: use median packet size for stability
    med_size = int(np.median(packet_sizes)) if packet_sizes else 0
    filled = []
    for j in range(n_packets):
        filled.extend(chunks[j])
        if j in missing_counts and j < (n_packets - 1):
            filler_len = missing_counts[j] * med_size
            if filler_len > 0:
                filled.extend([filler_value] * filler_len)
    return np.asarray(filled, dtype=np.float64)

def _build_timestamp_samples(timestamps, packet_samples, max_interval=None, insert_filler=True):
    """Build a per-sample TimeStamp channel from per-packet timestamps.
    - Repeats each packet timestamp for `packet_samples` samples.
    - Optionally inserts zeros for detected missing packets using `max_interval`.
    """
    import numpy as np
    if timestamps is None:
        return np.asarray([], dtype=np.float64)
    try:
        n_packets = len(timestamps)
    except Exception:
        timestamps = list(timestamps)
        n_packets = len(timestamps)
    if n_packets == 0 or packet_samples <= 0:
        return np.asarray([], dtype=np.float64)

    ts_samples = []
    for j in range(n_packets - 1):
        t = float(timestamps[j])
        ts_samples.extend([t] * packet_samples)
        # 仅在中间缺口插零，避免在文件末尾造成多余零点
        if insert_filler and max_interval is not None and j < (n_packets - 2):
            try:
                diff = float(timestamps[j + 1]) - t
            except Exception:
                diff = max_interval
            if diff > max_interval:
                missing = int(np.ceil(diff / max_interval)) - 1
            elif diff <= 0:
                missing = 1
            else:
                missing = 0
            if missing > 0:
                ts_samples.extend([0.0] * (missing * packet_samples))

    # Append last packet timestamp
    last_t = float(timestamps[-1])
    ts_samples.extend([last_t] * packet_samples)

    return np.asarray(ts_samples, dtype=np.float64)

def _broadcast_channel_to_samples_by_variable(channels, packet_sizes, timestamps, max_interval, filler_value=-1000, packet_indices=None, expected_index_step=1):
    """Broadcast per-packet Raw_channel to per-sample array using variable packet sizes.
    - channels: list of per-packet channel indices
    - packet_sizes: list of per-packet sample counts
    - timestamps: list of per-packet timestamps (ms)
    - max_interval: gap threshold (ms) to insert missing packet fillers
    Returns numpy array aligned to raw data length, with filler for gaps.
    """
    import numpy as np
    if channels is None or timestamps is None:
        return np.asarray([], dtype=np.float64)
    n_packets = len(timestamps)
    if n_packets == 0:
        return np.asarray([], dtype=np.float64)
    # Ensure packet_sizes length; if missing, fallback to median size
    if not packet_sizes or len(packet_sizes) != n_packets:
        packet_sizes = [1] * n_packets
    # Detect missing packet gaps
    if packet_indices is not None and len(packet_indices) >= n_packets:
        packet_indices = list(packet_indices)[:n_packets]
        missing_counts, _ = _calc_missing_counts_from_indices(packet_indices, expected_step=expected_index_step)
    else:
        missing_counts = {}
        logging.critical(f"PacketIndex unavailable for channel broadcast interpolation: n_packets={n_packets}, provided={0 if packet_indices is None else len(packet_indices)}")
    med_size = int(np.median(packet_sizes)) if packet_sizes else 0
    out = []
    for j in range(n_packets):
        ch = channels[j] if j < len(channels) else filler_value
        sz = int(packet_sizes[j])
        out.extend([float(ch)] * sz)
        # Fill gaps before the next received packet, but not after the tail.
        if j in missing_counts and j < (n_packets - 1):
            out.extend([float(filler_value)] * (missing_counts[j] * med_size))
    return np.asarray(out, dtype=np.float64)

############################################################
############################################################
############################Recording Modes################################
############################################################
############################################################
""" Mode 0/3 LFP/ESA 1kHz recording """
def get_raw_data_container(): # LFP raw data
    raw_data = {  # maximum 16 channels
        "Channel_0":[],
        "Channel_1":[],
        "Channel_2":[], 
        "Channel_3":[],
        "Channel_4":[], 
        "Channel_5":[],
        "Channel_6":[],
        "Channel_7":[],
        "Channel_8":[], 
        "Channel_9":[],
        "Channel_10":[],
        "Channel_11":[],
        "Channel_12":[],
        "Channel_13":[],
        "Channel_14":[],
        "Channel_15":[],
        "MAND_Channel_0":[],
        "MAND_Channel_1":[],
        "MAND_Channel_2":[], 
        "MAND_Channel_3":[],
        "MAND_Channel_4":[], 
        "MAND_Channel_5":[],
        "MAND_Channel_6":[],
        "MAND_Channel_7":[],
        "MAND_Channel_8":[], 
        "MAND_Channel_9":[],
        "MAND_Channel_10":[],
        "MAND_Channel_11":[],
        "MAND_Channel_12":[],
        "MAND_Channel_13":[],
        "MAND_Channel_14":[],
        "MAND_Channel_15":[],
        "Raw":[],
        "RawChannel":[],
        "Raw_alignment": [],
        "Raw_alignment_gap_fills": [],
        "SensorMask": [],
        "Mode0QuantBits": [],
        "Mode0QuantFullScaleUv": [],
       
        "TimeStamp":[] , # real PC time after time calibration every packets (ms)
        "PacketIndex":[],
        
        "MissPackets":0, 
        "MissPacketsIndex":[]
        } 
    return raw_data

def get_mode3_data_container(): # LFP raw data
    raw_data = {  # maximum 16 channels
        "Channel_0":[],
        "Channel_1":[],
        "Channel_2":[], 
        "Channel_3":[],
        "Channel_4":[], 
        "Channel_5":[],
        "Channel_6":[],
        "Channel_7":[],
        "Channel_8":[], 
        "Channel_9":[],
        "Channel_10":[],
        "Channel_11":[],
        "Channel_12":[],
        "Channel_13":[],
        "Channel_14":[],
        "Channel_15":[],

        "ESA_Channel_0":[],
        "ESA_Channel_1":[],
        "ESA_Channel_2":[], 
        "ESA_Channel_3":[],
        "ESA_Channel_4":[], 
        "ESA_Channel_5":[],
        "ESA_Channel_6":[],
        "ESA_Channel_7":[],
        "ESA_Channel_8":[], 
        "ESA_Channel_9":[],
        "ESA_Channel_10":[],
        "ESA_Channel_11":[],
        "ESA_Channel_12":[],
        "ESA_Channel_13":[],
        "ESA_Channel_14":[],
        "ESA_Channel_15":[],
        
        "Raster_Channel_0":[],
        "Raster_Channel_1":[],
        "Raster_Channel_2":[],
        "Raster_Channel_3":[],
        "Raster_Channel_4":[],
        "Raster_Channel_5":[],
        "Raster_Channel_6":[],
        "Raster_Channel_7":[],
        "Raster_Channel_8":[],
        "Raster_Channel_9":[],
        "Raster_Channel_10":[],
        "Raster_Channel_11":[],
        "Raster_Channel_12":[],
        "Raster_Channel_13":[],
        "Raster_Channel_14":[],
        "Raster_Channel_15":[],
       
        "TimeStamp":[] , # real PC time after time calibration every packets (ms)
        "PacketIndex":[],

        "AP_timestamp":[] , # raster
        "Electrode":[] , # firing electrode
        "SpikeThreshold":[],

        "Raw_data":[], # 12.5Khz data
        "Raw_channel":[], # firing channel
        "Raw_timestamp":[], # raw timestamp
        "Raw_packet_indices":[],
        "Raw_packet_sizes":[], # per-packet raw sample count for interpolation
        "Raw_alignment": [], # Alignment signal
        
        "MissPackets":0, 
        "MissPacketsIndex":[]
        } 
    return raw_data

""" IMU & Battery Status """
def get_events_data_container(): # Action potiential events & other recorded data
    events_data =  {
        "AcclX":[],
        "AcclY":[],
        "AcclZ":[],
        "GryoX":[],
        "GryoY":[],
        "GryoZ":[],

        "RSOC":[],
        "Battery_STAT":[],
        "Battery_voltage":[],
        "UpdateFlag":[]
        } 
    return events_data

############################################################
############################################################
############################Test Modes################################
############################################################
############################################################
""" Mode 1 SPike 20kHz recording """
def spike_data_container(): # mode 1; raster 16 channels + 1 channel raw data
    spike_data = {
        "AP_timestamp":[] , # raster
        "Electrode":[] , # firing electrode

        "Raw_data":[], # 20Khz data
        "Raw_channel":[], # firing channel
        "Raw_timestamp":[], # raw timestamp
        "PacketIndex":[],

        "SpikeThreshold":[],

        "MissPackets":0,
        "MissPacketsIndex":[]
    }
    return spike_data


############################################################
############################################################
############################Functions################################
############################################################
############################################################
def swap16Hex(str):
    return str[2:4] + str[0:2]

def _decode_signed_u16_hex(word_hex):
    value = int(swap16Hex(word_hex), 16) & 0xFFFF
    if value >= 0x8000:
        value -= 0x10000
    return value

def _decode_u16_hex(word_hex):
    return int(swap16Hex(str(word_hex)), 16) & 0xFFFF

def _default_mode0_power_status():
    return {
        "valid": False,
        "telemetry_seq": 0,
        "sleep_avg_us": 0.0,
        "active_avg_us": 0.0,
        "sleep_min_us": 0.0,
        "sleep_max_us": 0.0,
        "sleep_ratio": 0.0,
        "sleep_ratio_percent": 0.0,
        "tx_success_count": 0,
        "tx_fail_count": 0,
        "tx_write_error_count": 0,
        "fail_rate_percent": 0.0,
        "tx_attempt_count": 0,
        "tx_retransmit_count": 0,
        "retransmit_rate_percent": 0.0,
        "avg_retransmits_per_packet": 0.0,
        "tx_power_code": 0,
        "tx_power_dbm": 0,
        "retransmit_count": 0,
        "flags": 0,
        "estimated_mw": None,
        "estimation_distance": None,
        "warning": "",
        "warnings": [],
    }

def _default_power_guard_status():
    return {
        "valid": False,
        "state": 0,
        "state_name": "unknown",
        "low_stop_counter": 0,
        "flags": 0,
        "low_battery_hold": False,
        "recovering": False,
        "reboot_pending": False,
        "skip_auto_recovery": False,
        "auto_recovery_attempted": False,
    }

def _parse_empty_power_guard_words(packet_words):
    words = _packet_words_to_list(packet_words)
    status = _default_power_guard_status()
    if len(words) < 10:
        return status
    try:
        magic = _decode_u16_hex(words[5])
    except Exception:
        return status
    if magic != POWER_GUARD_EMPTY_STATUS_MAGIC:
        return status
    try:
        state = int(_decode_u16_hex(words[6]))
        low_stop_counter = int(_decode_u16_hex(words[7]))
        flags = int(_decode_u16_hex(words[8]))
    except Exception:
        return _default_power_guard_status()

    return {
        "valid": True,
        "state": state,
        "state_name": POWER_GUARD_STATE_NAMES.get(state, "unknown"),
        "low_stop_counter": low_stop_counter,
        "flags": flags,
        "low_battery_hold": bool(flags & POWER_GUARD_FLAG_LOW_VBAT_HOLD or state == POWER_GUARD_STATE_LOW_VBAT_HOLD),
        "recovering": bool(flags & POWER_GUARD_FLAG_RECOVERING or state == POWER_GUARD_STATE_RECOVERING),
        "reboot_pending": bool(flags & POWER_GUARD_FLAG_REBOOT_PENDING or state == POWER_GUARD_STATE_REBOOT_PENDING),
        "skip_auto_recovery": bool(flags & POWER_GUARD_FLAG_SKIP_AUTO_RECOVERY),
        "auto_recovery_attempted": bool(flags & POWER_GUARD_FLAG_AUTO_RECOVERY_DONE),
    }

def _normalize_mode0_power_estimator_config(config):
    normalized = {
        "enabled": bool(DEFAULT_MODE0_POWER_ESTIMATOR_CONFIG["enabled"]),
        "warn_retransmit_rate_percent": float(DEFAULT_MODE0_POWER_ESTIMATOR_CONFIG["warn_retransmit_rate_percent"]),
        "calibration_rows": list(DEFAULT_MODE0_POWER_ESTIMATOR_CONFIG["calibration_rows"]),
    }
    if not isinstance(config, dict):
        return normalized
    if "enabled" in config:
        normalized["enabled"] = bool(config.get("enabled"))
    for key in ("warn_retransmit_rate_percent",):
        try:
            normalized[key] = max(0.0, float(config.get(key, normalized[key]) or 0.0))
        except Exception:
            pass
    rows = config.get("calibration_rows")
    if isinstance(rows, list):
        normalized_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                normalized_rows.append({
                    "tx_power_dbm": int(float(row.get("tx_power_dbm", 0) or 0)),
                    "retransmit_rate_percent": _mode0_power_row_retransmit_rate_percent(row),
                    "mw": float(row.get("mw")),
                })
            except Exception:
                continue
        normalized["calibration_rows"] = normalized_rows
    return normalized

def _mode0_power_row_retransmit_rate_percent(row):
    if not isinstance(row, dict):
        return 0.0
    for key in ("retransmit_rate_percent", "retry_rate_percent", "tx_retransmit_rate_percent"):
        if key not in row:
            continue
        try:
            return max(0.0, float(row.get(key) or 0.0))
        except Exception:
            continue
    return 0.0

def _mode0_tx_power_dbm_from_code(tx_power_code):
    try:
        code = int(tx_power_code or 0)
    except Exception:
        code = 0
    return int(ESB_TX_POWER_CODE_TO_DBM.get(code, 0))

def _packet_words_to_wire_bytes(words):
    out = []
    for word in _packet_words_to_list(words):
        clean = str(word).strip().replace(" ", "")
        if len(clean) < 4:
            clean = clean.zfill(4)
        out.append(int(clean[0:2], 16))
        out.append(int(clean[2:4], 16))
    return out

def _estimate_mode0_power_mw(mode0_power, estimator_config):
    if not bool(estimator_config.get("enabled", True)):
        return None, None
    rows = estimator_config.get("calibration_rows", [])
    if not isinstance(rows, list) or not rows:
        return None, None
    try:
        retransmit_rate_percent = float(mode0_power.get("retransmit_rate_percent", 0.0) or 0.0)
        tx_power_dbm = int(mode0_power.get("tx_power_dbm", 0) or 0)
    except Exception:
        return None, None

    best_distance = None
    best_mw = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            row_tx_power_dbm = int(float(row.get("tx_power_dbm", tx_power_dbm) or 0))
            row_retransmit_rate_percent = _mode0_power_row_retransmit_rate_percent(row)
            row_mw = float(row.get("mw"))
        except Exception:
            continue
        distance = (
            abs(retransmit_rate_percent - row_retransmit_rate_percent) / 100.0
            + (0.0 if tx_power_dbm == row_tx_power_dbm else 5.0)
        )
        if best_distance is None or distance < best_distance:
            best_distance = float(distance)
            best_mw = float(row_mw)
    return best_mw, best_distance

def _parse_mode0_power_telemetry_words(telemetry_words, estimator_config=None, config_normalized=False):
    words = _packet_words_to_list(telemetry_words)
    if len(words) != MODE0_COMPACT_POWER_WORDS:
        return _default_mode0_power_status()
    try:
        telemetry_bytes = _packet_words_to_wire_bytes(words)
    except Exception:
        return _default_mode0_power_status()
    if len(telemetry_bytes) < 4:
        return _default_mode0_power_status()

    telemetry_seq = int(telemetry_bytes[0])
    flags = int(telemetry_bytes[1])
    retransmit_rate_percent = float(telemetry_bytes[2]) * 100.0 / 255.0
    sleep_ratio = float(telemetry_bytes[3]) / 255.0
    sleep_avg_us = 0.0
    active_avg_us = 0.0
    sleep_min_us = 0.0
    sleep_max_us = 0.0
    tx_attempt_count = 0
    tx_retransmit_count = 0
    tx_success_count = 0
    tx_fail_count = 0
    fail_rate_percent = 0.0
    avg_retransmits_per_packet = 0.0
    tx_power_code = flags & 0x000F
    tx_power_dbm = _mode0_tx_power_dbm_from_code(tx_power_code)
    retransmit_count = (flags >> 4) & 0x000F
    estimator_config = (
        dict(estimator_config or {})
        if config_normalized
        else _normalize_mode0_power_estimator_config(estimator_config or {})
    )

    mode0_power = {
        "valid": True,
        "telemetry_seq": telemetry_seq,
        "sleep_avg_us": sleep_avg_us,
        "active_avg_us": active_avg_us,
        "sleep_min_us": sleep_min_us,
        "sleep_max_us": sleep_max_us,
        "sleep_ratio": float(sleep_ratio),
        "sleep_ratio_percent": float(sleep_ratio * 100.0),
        "tx_success_count": tx_success_count,
        "tx_fail_count": tx_fail_count,
        "tx_write_error_count": 0,
        "fail_rate_percent": float(fail_rate_percent),
        "tx_attempt_count": int(tx_attempt_count),
        "tx_retransmit_count": int(tx_retransmit_count),
        "retransmit_rate_percent": float(retransmit_rate_percent),
        "avg_retransmits_per_packet": float(avg_retransmits_per_packet),
        "tx_power_code": int(tx_power_code),
        "tx_power_dbm": int(tx_power_dbm),
        "retransmit_count": int(retransmit_count),
        "flags": int(flags),
        "estimated_mw": None,
        "estimation_distance": None,
        "warning": "",
        "warnings": [],
    }
    estimated_mw, estimation_distance = _estimate_mode0_power_mw(mode0_power, estimator_config)
    mode0_power["estimated_mw"] = estimated_mw
    mode0_power["estimation_distance"] = estimation_distance

    warnings = []
    if retransmit_rate_percent >= float(estimator_config.get("warn_retransmit_rate_percent", 20.0) or 20.0):
        warnings.append("high_retransmit_rate")
    mode0_power["warnings"] = warnings
    mode0_power["warning"] = ", ".join(warnings)
    return mode0_power

DEFAULT_DAC_RESOLUTION = 1 / int("ffff", 16) * 1.225 * 2

def _packet_words_to_list(packet_words):
    if packet_words is None:
        return []
    if isinstance(packet_words, np.ndarray):
        return packet_words.tolist()
    if isinstance(packet_words, str):
        return packet_words.split()
    return list(packet_words)

def _dac_word_raw_int(word_hex):
    return int(swap16Hex(str(word_hex)), 16)

def _dac_word_uv(word_hex, dac_resolution=DEFAULT_DAC_RESOLUTION):
    return (float(_dac_word_raw_int(word_hex) * float(dac_resolution)) - 1.225) / 192 * 1000 * 1000

def _decode_mode3_lfp_esa_packet_words(packet_words, raw_data_per_packet_mode3=2, dac_resolution=DEFAULT_DAC_RESOLUTION):
    words = _packet_words_to_list(packet_words)
    per_packet = max(1, int(raw_data_per_packet_mode3 or 2))
    base = 9
    esa_base = base + 16 * per_packet
    lfp_channels = [[] for _ in range(16)]
    esa_channels = [[] for _ in range(16)]
    raster_channels = [[] for _ in range(16)]

    for channel_num in range(16):
        lfp_start = base + channel_num * per_packet
        esa_start = esa_base + channel_num * per_packet
        lfp_channels[channel_num] = [
            _dac_word_uv(word, dac_resolution=dac_resolution)
            for word in words[lfp_start:lfp_start + per_packet]
        ]
        esa_channels[channel_num] = [
            _dac_word_uv(word, dac_resolution=dac_resolution)
            for word in words[esa_start:esa_start + per_packet]
        ]

    packet_raster_bins = [[0, 0, 0] for _ in range(16)]
    for raster_time, spike_word in enumerate(words[-3:][:3]):
        spike_bits = format(_dac_word_raw_int(spike_word), "#018b")[2:]
        for channel_num, spike_num in enumerate(spike_bits):
            mapped_channel = 15 - channel_num
            packet_raster_bins[mapped_channel][raster_time] = int(spike_num)
    for channel_num in range(16):
        b0, b1, b2 = packet_raster_bins[channel_num]
        raster_channels[channel_num] = [max(b0, b1), max(b1, b2)]

    return lfp_channels, esa_channels, raster_channels

def _normalize_mode0_quant_bits(bit_depth):
    try:
        bits = int(bit_depth)
    except (TypeError, ValueError):
        bits = MODE0_QUANT_BITS_DEFAULT
    return max(MODE0_QUANT_BITS_MIN, min(MODE0_QUANT_BITS_MAX, bits))

def _normalize_mode0_quant_full_scale_uv(full_scale_uv):
    try:
        value = float(full_scale_uv)
    except (TypeError, ValueError):
        value = MODE0_QUANT_FULL_SCALE_UV_DEFAULT
    if not math.isfinite(value):
        value = MODE0_QUANT_FULL_SCALE_UV_DEFAULT
    value = max(MODE0_QUANT_FULL_SCALE_UV_MIN, min(MODE0_QUANT_FULL_SCALE_UV_MAX, value))
    steps = round((value - MODE0_QUANT_FULL_SCALE_UV_MIN) / MODE0_QUANT_FULL_SCALE_UV_STEP)
    return float(MODE0_QUANT_FULL_SCALE_UV_MIN + steps * MODE0_QUANT_FULL_SCALE_UV_STEP)

def _mode0_neural_packed_bytes_for_bits(bit_depth):
    bits = _normalize_mode0_quant_bits(bit_depth)
    return (MODE0_NEURAL_VALUES_PER_PACKET * bits + 7) // 8

def _mode0_neural_packed_words_for_bits(bit_depth):
    return (_mode0_neural_packed_bytes_for_bits(bit_depth) + 1) // 2

def _mode0_packet_base_words_for_bits(bit_depth):
    return 1 + 2 + _mode0_neural_packed_words_for_bits(bit_depth) + 1

def _mode0_packet_max_words_for_bits(bit_depth):
    return _mode0_packet_base_words_for_bits(bit_depth) + MODE0_ACCEL_WORDS + MODE0_STATUS_WORDS

def _unpack_mode0_quantized_values_from_words(packet_words, value_count, bit_depth):
    bits = _normalize_mode0_quant_bits(bit_depth)
    packet_bytes = _packet_words_to_wire_bytes(packet_words)
    expected_bits = value_count * bits
    available_bits = len(packet_bytes) * 8
    if available_bits < expected_bits:
        raise ValueError(f"Expected {value_count} packed {bits}-bit values, got {available_bits // bits}")
    values = []
    bit_pos = 0
    mask = (1 << bits) - 1
    for _ in range(value_count):
        byte_index = bit_pos // 8
        bit_offset = bit_pos % 8
        acc = 0
        for shift, idx in enumerate(range(byte_index, min(byte_index + 3, len(packet_bytes)))):
            acc |= (int(packet_bytes[idx]) & 0xFF) << (8 * shift)
        values.append((acc >> bit_offset) & mask)
        bit_pos += bits
    return values

def _decode_signed_quantized_uv(value, bit_depth, full_scale_uv):
    bits = _normalize_mode0_quant_bits(bit_depth)
    full_scale = _normalize_mode0_quant_full_scale_uv(full_scale_uv)
    value = int(value) & ((1 << bits) - 1)
    sign_bit = 1 << (bits - 1)
    if value & sign_bit:
        value -= 1 << bits
    q_max = (1 << (bits - 1)) - 1
    value = max(-q_max, min(q_max, value))
    return float(value) * full_scale / float(q_max)

def _decode_unsigned_quantized_uv(value, bit_depth):
    bits = _normalize_mode0_quant_bits(bit_depth)
    q_max = (1 << bits) - 1
    return float(int(value) & q_max) * 1000.0 / float(q_max)

def _decode_mode0_quantized_neural_words(packet_words, bit_depth=None, full_scale_uv=None):
    bits = _normalize_mode0_quant_bits(MODE0_QUANT_BITS_DEFAULT if bit_depth is None else bit_depth)
    lfp_full_scale = _normalize_mode0_quant_full_scale_uv(
        MODE0_QUANT_FULL_SCALE_UV_DEFAULT if full_scale_uv is None else full_scale_uv
    )
    packed_values = _unpack_mode0_quantized_values_from_words(
        packet_words,
        MODE0_NEURAL_VALUES_PER_PACKET,
        bits,
    )
    cursor = 0
    lfp_values = [
        _decode_signed_quantized_uv(v, bits, lfp_full_scale)
        for v in packed_values[cursor:cursor + MODE0_LFP_POINTS_PER_PACKET]
    ]
    cursor += MODE0_LFP_POINTS_PER_PACKET
    mand_values = [
        _decode_unsigned_quantized_uv(v, bits)
        for v in packed_values[cursor:cursor + MODE0_MAND_POINTS_PER_PACKET]
    ]
    cursor += MODE0_MAND_POINTS_PER_PACKET
    raw_values = [
        _decode_signed_quantized_uv(v, bits, MODE0_RAW_QUANT_FULL_SCALE_UV)
        for v in packed_values[cursor:cursor + MODE0_RAW_POINTS_PER_PACKET]
    ]

    lfp_channels = [
        lfp_values[ch * 4:(ch + 1) * 4]
        for ch in range(16)
    ]
    mand_channels = [[mand_values[ch]] for ch in range(16)]
    return lfp_channels, mand_channels, raw_values

def _decode_mode0_8bit_neural_words(packet_words):
    return _decode_mode0_quantized_neural_words(
        packet_words,
        bit_depth=MODE0_QUANT_BITS_DEFAULT,
        full_scale_uv=MODE0_QUANT_FULL_SCALE_UV_DEFAULT,
    )

def _decode_mode3_raw_sample_words(packet_words, dac_resolution=DEFAULT_DAC_RESOLUTION):
    return [_dac_word_uv(word, dac_resolution=dac_resolution) for word in _packet_words_to_list(packet_words)]

STREAM_MODE_IDLE = -1
STREAM_MODE_MODE0 = 0
STREAM_MODE_MODE1 = 1
STREAM_MODE_MODE2 = 2
STREAM_MODE_MODE3 = 3
DEFAULT_MODE3_SPIKE_THRESHOLD_UV = 60.0
MODE0_QUANT_BITS_MIN = 8
MODE0_QUANT_BITS_MAX = 12
MODE0_QUANT_BITS_DEFAULT = 12
MODE0_QUANT_FULL_SCALE_UV_MIN = 500.0
MODE0_QUANT_FULL_SCALE_UV_MAX = 10000.0
MODE0_QUANT_FULL_SCALE_UV_STEP = 500.0
MODE0_QUANT_FULL_SCALE_UV_DEFAULT = 1000.0
MODE0_RAW_QUANT_FULL_SCALE_UV = 500.0
MODE0_SIGNED_UV_FULL_SCALE = MODE0_QUANT_FULL_SCALE_UV_DEFAULT
MODE0_SIGNED_UV_PHYSICAL_MIN = -MODE0_SIGNED_UV_FULL_SCALE
MODE0_SIGNED_UV_PHYSICAL_MAX = MODE0_SIGNED_UV_FULL_SCALE
MODE0_RAW_PHYSICAL_MIN = -MODE0_RAW_QUANT_FULL_SCALE_UV
MODE0_RAW_PHYSICAL_MAX = MODE0_RAW_QUANT_FULL_SCALE_UV
MODE0_LFP_POINTS_PER_PACKET = 64
MODE0_MAND_POINTS_PER_PACKET = 16
MODE0_RAW_POINTS_PER_PACKET = 50
MODE0_NEURAL_VALUES_PER_PACKET = (
    MODE0_LFP_POINTS_PER_PACKET
    + MODE0_MAND_POINTS_PER_PACKET
    + MODE0_RAW_POINTS_PER_PACKET
)
MODE0_NEURAL_PACKED_BITS = MODE0_QUANT_BITS_DEFAULT
MODE0_NEURAL_PACKED_BYTES = _mode0_neural_packed_bytes_for_bits(MODE0_NEURAL_PACKED_BITS)
MODE0_NEURAL_PACKED_WORDS = _mode0_neural_packed_words_for_bits(MODE0_NEURAL_PACKED_BITS)
MODE0_NEURAL_PACKED_MAX_BITS = MODE0_QUANT_BITS_MAX
MODE0_NEURAL_PACKED_MAX_BYTES = _mode0_neural_packed_bytes_for_bits(MODE0_NEURAL_PACKED_MAX_BITS)
MODE0_NEURAL_PACKED_MAX_WORDS = _mode0_neural_packed_words_for_bits(MODE0_NEURAL_PACKED_MAX_BITS)
MODE0_COMPACT_POWER_WORDS = 2
MODE0_POWER_TELEMETRY_WORDS = MODE0_COMPACT_POWER_WORDS
MODE0_HEADER_RAW_CHANNEL_MASK = 0x0F
MODE0_HEADER_OVERFLOW_FLAG = 0x10
MODE0_HEADER_ACCEL_FLAG = 0x20
MODE0_HEADER_STATUS_FLAG = 0x40
MODE0_ACCEL_WORDS = 3
MODE0_STATUS_WORDS = 3 + MODE0_COMPACT_POWER_WORDS
MODE0_PACKET_BASE_WORDS = _mode0_packet_base_words_for_bits(MODE0_QUANT_BITS_DEFAULT)
MODE0_PACKET_MAX_WORDS = _mode0_packet_max_words_for_bits(MODE0_QUANT_BITS_DEFAULT)
MODE0_PACKET_MAX_CONFIG_WORDS = _mode0_packet_max_words_for_bits(MODE0_QUANT_BITS_MAX)
MODE0_PACKET_WORDS = MODE0_PACKET_MAX_WORDS
MODE0_SENSOR_KEYS = ("AcclX", "AcclY", "AcclZ", "RSOC", "Battery_STAT", "Battery_voltage")
MODE0_SENSOR_SAMPLE_RATES = (100, 100, 100, 2, 2, 2)
SENSOR_MASK_PACKET_LOSS = 0x04
MODE2_V2_FORMAT_BYTE = 0x88
MODE2_V2_PACKET_WORDS = 124
MODE2_V2_EXT_PACKET_WORDS = 126
MODE2_V2_HEADER_WORDS = 4
MODE2_V2_PACKET_SAMPLES = 15
MODE2_V2_CHANNELS = 16
MODE2_V2_PAYLOAD_BYTES = MODE2_V2_PACKET_SAMPLES * MODE2_V2_CHANNELS
MODE2_V2_PAYLOAD_WORDS = MODE2_V2_PAYLOAD_BYTES // 2
MODE2_V2_FS = 10417
MODE2_V2_FULL_SCALE_UV = 500.0
MODE2_V2_UV_PER_COUNT = MODE2_V2_FULL_SCALE_UV / 127.0
MODE2_V2_PHYSICAL_MIN_UV = -504.0
MODE2_V2_PHYSICAL_MAX_UV = MODE2_V2_FULL_SCALE_UV
MODE2_V2_GUI_MISSING_SAMPLE = -MODE2_V2_FULL_SCALE_UV
MODE2_V2_DETAIL_TAIL_PACKETS = 160
MODE2_ALIGNMENT_PULSE_SECONDS = 0.006
MODE2_SENSOR_KEYS = ("AcclX", "AcclY", "AcclZ")
MODE2_SENSOR_SAMPLE_RATES = (100, 100, 100)
LSM6DS3_ACCEL_LSB_G = 0.061 / 1000.0
LSM6DS3_GYRO_LSB_DPS = (4.375 * (500.0 / 125.0)) / 1000.0
LSM6DS3_ACCEL_PHYSICAL_MIN_G = -1.99885
LSM6DS3_ACCEL_PHYSICAL_MAX_G = 1.99879
LSM6DS3_GYRO_PHYSICAL_MIN_DPS = -32768.0 * LSM6DS3_GYRO_LSB_DPS
LSM6DS3_GYRO_PHYSICAL_MAX_DPS = 32767.0 * LSM6DS3_GYRO_LSB_DPS
MODE2_ACCEL_PHYSICAL_MIN = LSM6DS3_ACCEL_PHYSICAL_MIN_G
MODE2_ACCEL_PHYSICAL_MAX = LSM6DS3_ACCEL_PHYSICAL_MAX_G
WIRELESS_GROUP_PACKETS = 10
WIRELESS_GROUP_END_MARKER = b"%&'("
WIRELESS_PACKET_SEPARATOR_MARKER = b"\x21\x22\x23\x24"
MODE2_V2_SERIAL_PACKET_BYTES = MODE2_V2_PACKET_WORDS * 2 + 2 + len(WIRELESS_PACKET_SEPARATOR_MARKER)
MODE2_V2_EXT_SERIAL_PACKET_BYTES = MODE2_V2_EXT_PACKET_WORDS * 2 + 2 + len(WIRELESS_PACKET_SEPARATOR_MARKER)
MODE2_V2_MIN_SERIAL_PACKET_BYTES = min(MODE2_V2_SERIAL_PACKET_BYTES, MODE2_V2_EXT_SERIAL_PACKET_BYTES)
MODE2_V2_MAX_SERIAL_PACKET_BYTES = max(MODE2_V2_SERIAL_PACKET_BYTES, MODE2_V2_EXT_SERIAL_PACKET_BYTES)
POWER_GUARD_EMPTY_STATUS_MAGIC = 0x0B01
POWER_GUARD_STATE_NORMAL = 0
POWER_GUARD_STATE_LOW_VBAT_HOLD = 1
POWER_GUARD_STATE_RECOVERING = 2
POWER_GUARD_STATE_REBOOT_PENDING = 3
POWER_GUARD_STATE_NAMES = {
    POWER_GUARD_STATE_NORMAL: "normal",
    POWER_GUARD_STATE_LOW_VBAT_HOLD: "low_vbat_hold",
    POWER_GUARD_STATE_RECOVERING: "recovering",
    POWER_GUARD_STATE_REBOOT_PENDING: "reboot_pending",
}
POWER_GUARD_FLAG_LOW_VBAT_HOLD = 0x0001
POWER_GUARD_FLAG_RECOVERING = 0x0002
POWER_GUARD_FLAG_REBOOT_PENDING = 0x0004
POWER_GUARD_FLAG_SKIP_AUTO_RECOVERY = 0x0008
POWER_GUARD_FLAG_AUTO_RECOVERY_DONE = 0x0010
DEFAULT_MODE0_POWER_ESTIMATOR_CONFIG = {
    "enabled": True,
    "warn_retransmit_rate_percent": 20.0,
    "calibration_rows": [
        {
            "tx_power_dbm": 0,
            "retransmit_rate_percent": 0.0,
            "mw": 15.0,
        }
    ],
}
GUI_INTERVAL_LOSS_EPSILON = 0.02
GUI_STREAM_TO_MODE = {
    "mode0_lfp": 0,
    "mode1_raw": 1,
    "mode2_raw": 2,
    "mode3_lfp_esa": 3,
    "mode3_raw": 3,
}
PACKET_LOSS_LEGACY_BUCKETS = ("lfp", "spike", "mode2")
PACKET_LOSS_STREAM_BUCKETS = tuple(GUI_STREAM_TO_MODE)
PACKET_LOSS_ALL_BUCKETS = PACKET_LOSS_LEGACY_BUCKETS + PACKET_LOSS_STREAM_BUCKETS
PACKET_LOSS_DEFAULT_WARMUP_PACKETS = 2
GUI_STREAM_DEFAULT_TARGET_FPS = 12.0
GUI_STREAM_MAX_REFRESH_FPS = 30.0
GUI_STREAM_NOMINAL_PACKETS_PER_SECOND = {
    "mode0_lfp": 1000.0 / 4.0,
    "mode1_raw": 20833.0 / 90.0,
    "mode2_raw": MODE2_V2_FS / MODE2_V2_PACKET_SAMPLES,
    "mode3_lfp_esa": 1000.0 / 2.0,
    "mode3_raw": 1000.0 / 8.0,
}


def _gui_packets_per_refresh_at_least_fps(stream_key, fps):
    try:
        packets_per_second = float(GUI_STREAM_NOMINAL_PACKETS_PER_SECOND[stream_key])
        fps = float(fps)
    except Exception:
        return 1
    if fps <= 0.0:
        return 1
    return max(1, int(math.floor(packets_per_second / fps)))


def _gui_packets_per_refresh_at_most_fps(stream_key, fps):
    try:
        packets_per_second = float(GUI_STREAM_NOMINAL_PACKETS_PER_SECOND[stream_key])
        fps = float(fps)
    except Exception:
        return 1
    if fps <= 0.0:
        return 1
    return max(1, int(math.ceil(packets_per_second / fps)))


GUI_STREAM_INTERVAL_DEFAULTS = {
    stream_key: _gui_packets_per_refresh_at_least_fps(stream_key, GUI_STREAM_DEFAULT_TARGET_FPS)
    for stream_key in GUI_STREAM_TO_MODE
}
GUI_STREAM_INTERVAL_MIN = {
    stream_key: _gui_packets_per_refresh_at_most_fps(stream_key, GUI_STREAM_MAX_REFRESH_FPS)
    for stream_key in GUI_STREAM_TO_MODE
}
MODE0_LFP_DETAIL_MAX_PACKETS = 80
MODE3_LFP_ESA_DETAIL_MAX_PACKETS = 80
MODE3_RAW_DETAIL_MAX_PACKETS = 60
GUI_STREAM_INTERVAL_MAX = {
    "mode0_lfp": MODE0_LFP_DETAIL_MAX_PACKETS,
    "mode1_raw": 240,
    "mode2_raw": 80,
    "mode3_lfp_esa": 80,
    "mode3_raw": 60,
}
GUI_INTERVAL_DEFAULTS = {
    0: GUI_STREAM_INTERVAL_DEFAULTS["mode0_lfp"],
    1: GUI_STREAM_INTERVAL_DEFAULTS["mode1_raw"],
    2: GUI_STREAM_INTERVAL_DEFAULTS["mode2_raw"],
    3: max(GUI_STREAM_INTERVAL_DEFAULTS["mode3_lfp_esa"], GUI_STREAM_INTERVAL_DEFAULTS["mode3_raw"]),
}
GUI_INTERVAL_MIN = {
    0: GUI_STREAM_INTERVAL_MIN["mode0_lfp"],
    1: GUI_STREAM_INTERVAL_MIN["mode1_raw"],
    2: GUI_STREAM_INTERVAL_MIN["mode2_raw"],
    3: max(GUI_STREAM_INTERVAL_MIN["mode3_lfp_esa"], GUI_STREAM_INTERVAL_MIN["mode3_raw"]),
}
GUI_INTERVAL_MAX = {
    0: GUI_STREAM_INTERVAL_MAX["mode0_lfp"],
    1: GUI_STREAM_INTERVAL_MAX["mode1_raw"],
    2: GUI_STREAM_INTERVAL_MAX["mode2_raw"],
    3: max(GUI_STREAM_INTERVAL_MAX["mode3_lfp_esa"], GUI_STREAM_INTERVAL_MAX["mode3_raw"]),
}
GUI_STREAM_TARGET_FPS_DEFAULTS = {
    stream_key: GUI_STREAM_DEFAULT_TARGET_FPS
    for stream_key in GUI_STREAM_TO_MODE
}
GUI_STREAM_INTERVAL_LOSS_EPSILON = GUI_INTERVAL_LOSS_EPSILON
RELAY_CDC_FIFO_REDLINE_BYTES = 30000
READ_BATCH_PACKET_WARN_RATIO_PERCENT = 80
READ_BATCH_SERIAL_BYTES_PER_PACKET_BY_MODE = {
    0: float(MODE0_PACKET_MAX_CONFIG_WORDS * 2 + 16),
    1: 224.0,
    2: 258.0,
    3: 178.0,
}
READ_BATCH_PACKET_REDLINE_BY_MODE = {
    mode: max(1, int(RELAY_CDC_FIFO_REDLINE_BYTES // bytes_per_packet))
    for mode, bytes_per_packet in READ_BATCH_SERIAL_BYTES_PER_PACKET_BY_MODE.items()
}
READ_BATCH_PACKET_WARN_BY_MODE = {
    mode: max(1, (redline * READ_BATCH_PACKET_WARN_RATIO_PERCENT + 99) // 100)
    for mode, redline in READ_BATCH_PACKET_REDLINE_BY_MODE.items()
}
PACKET_INDEX_MODULUS = 0x10000
PACKET_INDEX_WRAP_WINDOW = 8192
PACKET_INDEX_RESET_LOW_WINDOW = 1024
PACKET_INDEX_REORDER_WINDOW = 1024

def _decode_mode2_v2_payload(packet_words):
    words = _packet_words_to_list(packet_words)
    payload_words = words[MODE2_V2_HEADER_WORDS:MODE2_V2_PACKET_WORDS]
    packet_bytes = bytes(_packet_words_to_wire_bytes(payload_words))
    return _decode_mode2_v2_payload_bytes(packet_bytes)


def _decode_mode2_v2_payload_bytes(packet_bytes):
    if len(packet_bytes) < MODE2_V2_PAYLOAD_BYTES:
        raise ValueError(
            f"Mode2 v2 payload too short: {len(packet_bytes)} < {MODE2_V2_PAYLOAD_BYTES}"
        )
    return (
        np.frombuffer(packet_bytes[:MODE2_V2_PAYLOAD_BYTES], dtype=np.int8)
        .reshape(MODE2_V2_PACKET_SAMPLES, MODE2_V2_CHANNELS)
        .astype(np.float64)
        * MODE2_V2_UV_PER_COUNT
    )


def _sign_extend(value, bits):
    value = int(value) & ((1 << int(bits)) - 1)
    sign_bit = 1 << (int(bits) - 1)
    if value & sign_bit:
        value -= 1 << int(bits)
    return value


def _decode_mode2_v2_accel_q13_bytes(packet_bytes):
    packet_bytes = bytes(packet_bytes or b"")
    if len(packet_bytes) < MODE2_V2_EXT_PACKET_WORDS * 2:
        return None
    imu_tail = packet_bytes[MODE2_V2_PACKET_WORDS * 2:MODE2_V2_EXT_PACKET_WORDS * 2]
    if len(imu_tail) < 4:
        return None
    imu_bits = int(packet_bytes[0]) | (int.from_bytes(imu_tail, "little") << 8)
    raw_values = []
    for shift in (0, 13, 26):
        q13 = (imu_bits >> shift) & 0x1FFF
        raw_values.append(int(_sign_extend(q13, 13) << 3))
    return raw_values


def _normalize_packet_expected_step(expected_step):
    try:
        normalized = int(expected_step) if expected_step else 1
    except Exception:
        normalized = 1
    if normalized <= 0:
        normalized = 1
    return normalized


def _packet_index_delta_u16(prev_idx, curr_idx):
    prev_idx = int(prev_idx) & 0xFFFF
    curr_idx = int(curr_idx) & 0xFFFF
    if curr_idx >= prev_idx:
        return int(curr_idx - prev_idx), False
    return int((PACKET_INDEX_MODULUS - prev_idx) + curr_idx), True


def _classify_packet_index_transition(prev_idx, curr_idx, expected_step=1, max_missing_packets=4096):
    """Classify packet-counter movement without mistaking stale packets for u16 wrap.

    The firmware uses one 16-bit packet counter. A smaller current value is only
    a true wrap when the previous value is near 65535 and the new value is near
    zero. Otherwise it is usually a delayed/stale packet, or a device restart.
    """
    expected_step = _normalize_packet_expected_step(expected_step)
    prev_idx = int(prev_idx) & 0xFFFF
    curr_idx = int(curr_idx) & 0xFFFF
    if curr_idx == prev_idx:
        return {
            "kind": "duplicate",
            "delta": 0,
            "missing": 0,
            "wrapped": False,
            "advance": False,
            "cap_exceeded": False,
        }

    cap = max(0, int(max_missing_packets or 0))
    if curr_idx > prev_idx:
        delta = int(curr_idx - prev_idx)
        increments = delta // expected_step if expected_step > 0 else 0
        missing = max(0, increments - 1)
        return {
            "kind": "forward",
            "delta": delta,
            "missing": missing,
            "wrapped": False,
            "advance": True,
            "cap_exceeded": cap > 0 and missing > cap,
        }

    reverse_delta = int(prev_idx - curr_idx)
    wrap_window = max(
        PACKET_INDEX_REORDER_WINDOW,
        min(PACKET_INDEX_MODULUS // 2, int(PACKET_INDEX_WRAP_WINDOW)),
    )
    plausible_wrap = (
        prev_idx >= (PACKET_INDEX_MODULUS - wrap_window)
        and curr_idx <= wrap_window
    )
    if plausible_wrap:
        delta = int((PACKET_INDEX_MODULUS - prev_idx) + curr_idx)
        increments = delta // expected_step if expected_step > 0 else 0
        missing = max(0, increments - 1)
        return {
            "kind": "wrap",
            "delta": delta,
            "missing": missing,
            "wrapped": True,
            "advance": True,
            "cap_exceeded": cap > 0 and missing > cap,
        }

    if curr_idx <= PACKET_INDEX_RESET_LOW_WINDOW and reverse_delta > wrap_window:
        return {
            "kind": "reset",
            "delta": 0,
            "missing": 0,
            "wrapped": False,
            "advance": True,
            "cap_exceeded": False,
        }

    return {
        "kind": "out_of_order",
        "delta": 0,
        "missing": 0,
        "wrapped": False,
        "advance": False,
        "cap_exceeded": False,
    }

def my_gaussian_filter1d(seq, sigma=50 ,truncate=4.0 ,order=0): 
    """allow NaN elements
    sigma: 50 windows 401
    seq's type is ndarray
    """
    if seq.ndim > 1:
        seq = np.squeeze(seq ,1)
    length = len(seq)
    ## prepare Kernel
    sd = float(sigma)
    # make the radius of the filter equal to truncate standard deviations
    lw = int(truncate * sd + 0.5) # radius 
    weights = gaussian_kernel1d(sigma, order, lw)
    #print("weights_length:" ,weights.shape[0])
    ## NaN padding
    pad = lw 
    out = np.zeros((length + pad * 2), dtype=np.float64) # 1 dim
    out[pad: pad + length] = seq.copy().astype(np.float64)
    out[0:pad] = np.nan
    out[-pad:] = np.nan
    tmp = out.copy()
    ## filtering
    for y in range(length):
        mask = np.where(np.isnan(tmp[y: y + len(weights)]) == False) # 选择不是nan的部分,并返回对应的index值
        if np.size(mask) != 0:
            out[pad + y] = np.sum(weights[mask] * tmp[y: y + len(weights)][mask]) / np.sum(weights[mask]) #TODO 再检查一下
        else:
            out[pad + y] = np.nan
    #out = np.clip(out, 0, 1) # 对于有nan值不适用
    out = out[pad:pad + length]
    return out

def gaussian_kernel1d(sigma, order, radius):
    """
    Computes a 1-D Gaussian convolution kernel.
    """
    if order < 0:
        raise ValueError('order must be non-negative')
    exponent_range = np.arange(order + 1)
    sigma2 = sigma * sigma
    x = np.arange(-radius, radius+1)
    phi_x = np.exp(-0.5 / sigma2 * x ** 2)
    phi_x = phi_x / phi_x.sum()

    if order == 0:
        return phi_x
    else:
        q[0] = 1
        D = np.diag(exponent_range[1:], 1)  # D @ q(x) = q'(x)
        P = np.diag(np.ones(order)/-sigma2, -1)  # P @ q(x) = q(x) * p'(x)
        Q_deriv = D + P
        for _ in range(order):
            q = Q_deriv.dot(q)
        q = (x[:, None] ** exponent_range).dot(q)
        return q * phi_x

# filter
def LFP_filter(data, low_cutoff="None", high_cutoff="None", fs=1000, order=4): 
    """
    对神经信号进行带通、低通或高通滤波
    
    参数:
    data: numpy数组，输入的神经信号数据
    low_cutoff: 低截止频率（Hz），如果为None则执行低通滤波
    high_cutoff: 高截止频率（Hz），如果为None则执行高通滤波
    fs: 采样频率（Hz），默认为1000Hz
    order: 滤波器阶数，默认为4
    
    返回:
    filtered_data: 滤波后的数据，与输入数据维度相同
    """
    from scipy.signal import butter, filtfilt
    import numpy as np
    
    # 检查输入数据类型
    if not isinstance(data, np.ndarray):
        data = np.array(data)
    
    # 保存原始数据形状
    original_shape = data.shape
    
    # 将数据转换为一维数组进行处理
    data_1d = data.flatten()

    # 根据提供的截止频率确定滤波器类型
    if low_cutoff != "None" and high_cutoff != "None":
        low_cutoff = float(low_cutoff)
        high_cutoff = float(high_cutoff)
        # 带通滤波
        nyq = 0.5 * fs
        low = low_cutoff / nyq
        high = high_cutoff / nyq
        b, a = butter(order, [low, high], btype='band')
    elif low_cutoff != "None":
        low_cutoff = float(low_cutoff)
        # 高通滤波
        nyq = 0.5 * fs
        cutoff = low_cutoff / nyq
        b, a = butter(order, cutoff, btype='high')
    elif high_cutoff != "None":
        high_cutoff = float(high_cutoff)
        # 低通滤波
        nyq = 0.5 * fs
        cutoff = high_cutoff / nyq
        b, a = butter(order, cutoff, btype='low')
    else:
        # 如果没有提供截止频率，则返回原始数据
        return data
    
    # 应用滤波器
    filtered_data = filtfilt(b, a, data_1d)
    
    # 将滤波后的数据恢复为原始形状
    filtered_data = filtered_data.reshape(original_shape)
    # print(filtered_data)
    
    return filtered_data

def Timestamp_neural_calibration(x): #TODO offline calibration; Mode0 and mode3 maybe need different calibration
    return x


RELAY_COMMAND_FRAME_MAGIC = b"\xA5\x5A"
RELAY_COMMAND_MAX_PAYLOAD = 16
RELAY_LOCAL_COMMAND_PREFIX = b"RLY"
RELAY_LOCAL_COMMAND_REBOOT = 0x01
RELAY_LOCAL_COMMAND_SET_ESB_CHANNEL_CODES = {
    84: 0x10,
    78: 0x11,
    67: 0x12,
    50: 0x13,
    33: 0x14,
    17: 0x15,
    2: 0x16,
}
RELAY_RECOMMENDED_ESB_CHANNELS = tuple(RELAY_LOCAL_COMMAND_SET_ESB_CHANNEL_CODES.keys())
DEVICE_RUNTIME_ESB_CHANNEL_COMMAND = 0x0B00
DEVICE_REMOTE_REBOOT_COMMAND = 0x0C00
DEVICE_REMOTE_REBOOT_MAGIC = 0xA55A
DEVICE_RUNTIME_ESB_MODE_CONFIG_COMMAND = 0x0D00
DEVICE_MODE0_QUANT_CONFIG_COMMAND = 0x0F00
ESB_RETRANSMIT_DELAY_DEFAULT_US = 1200
ESB_RETRANSMIT_DELAY_MIN_US = 450
ESB_RETRANSMIT_DELAY_MAX_US = 4000
ESB_NOACK_PERCENT_DEFAULT = 0
ESB_NOACK_PERCENT_MAX = 100
ESB_TX_POWER_CODE_TO_DBM = {
    0: 0,
    1: 4,
    2: -4,
    3: -8,
    4: -12,
    5: -16,
    6: -20,
    7: -40,
}
ESB_TX_POWER_DBM_TO_CODE = {value: key for key, value in ESB_TX_POWER_CODE_TO_DBM.items()}


def relay_command_frame_checksum(payload):
    payload = bytes(payload)
    checksum = len(payload) ^ RELAY_COMMAND_FRAME_MAGIC[0] ^ RELAY_COMMAND_FRAME_MAGIC[1]
    for value in payload:
        checksum ^= int(value) & 0xFF
    return checksum & 0xFF


def build_relay_command_frame(payload):
    payload = bytes(payload)
    payload_len = len(payload)
    if payload_len <= 0:
        raise ValueError("Relay command payload cannot be empty")
    if payload_len > RELAY_COMMAND_MAX_PAYLOAD:
        raise ValueError(
            f"Relay command payload too long: {payload_len} > {RELAY_COMMAND_MAX_PAYLOAD}"
        )
    return (
        RELAY_COMMAND_FRAME_MAGIC
        + bytes([payload_len])
        + payload
        + bytes([relay_command_frame_checksum(payload)])
    )


def build_relay_local_command_payload(command_name):
    normalized = str(command_name).strip().lower()
    if normalized == "reboot":
        return RELAY_LOCAL_COMMAND_PREFIX + bytes([RELAY_LOCAL_COMMAND_REBOOT])
    if normalized.startswith("set_esb_channel_"):
        try:
            channel = int(normalized.rsplit("_", 1)[-1])
        except ValueError as e:
            raise ValueError(f"Unsupported relay local command: {command_name}") from e
        command_code = RELAY_LOCAL_COMMAND_SET_ESB_CHANNEL_CODES.get(channel)
        if command_code is None:
            raise ValueError(f"Unsupported relay local command: {command_name}")
        return RELAY_LOCAL_COMMAND_PREFIX + bytes([command_code])
    raise ValueError(f"Unsupported relay local command: {command_name}")


def build_runtime_device_esb_channel_command(channel):
    try:
        channel_value = int(channel)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Unsupported peripheral ESB channel: {channel}") from e
    if channel_value not in RELAY_RECOMMENDED_ESB_CHANNELS:
        raise ValueError(f"Unsupported peripheral ESB channel: {channel}")
    return bytes([
        DEVICE_RUNTIME_ESB_CHANNEL_COMMAND & 0xFF,
        (DEVICE_RUNTIME_ESB_CHANNEL_COMMAND >> 8) & 0xFF,
        channel_value & 0xFF,
        0x00,
    ])


def build_runtime_device_reboot_command():
    return bytes([
        DEVICE_REMOTE_REBOOT_COMMAND & 0xFF,
        (DEVICE_REMOTE_REBOOT_COMMAND >> 8) & 0xFF,
        DEVICE_REMOTE_REBOOT_MAGIC & 0xFF,
        (DEVICE_REMOTE_REBOOT_MAGIC >> 8) & 0xFF,
    ])


def build_runtime_device_esb_mode_config_command(
    mode,
    tx_power_code,
    retransmit_count,
    ack_window_us=ESB_RETRANSMIT_DELAY_DEFAULT_US,
    noack_percent=ESB_NOACK_PERCENT_DEFAULT,
):
    try:
        mode_value = int(mode)
        tx_power_code_value = int(tx_power_code)
        retransmit_count_value = int(retransmit_count)
        ack_window_us_value = int(ack_window_us)
        noack_percent_value = int(noack_percent)
    except (TypeError, ValueError) as e:
        raise ValueError("Unsupported ESB mode config value") from e
    if mode_value not in (0, 1, 2, 3):
        raise ValueError(f"Unsupported ESB mode: {mode}")
    if tx_power_code_value not in ESB_TX_POWER_CODE_TO_DBM:
        raise ValueError(f"Unsupported ESB TX power code: {tx_power_code}")
    if not 0 <= retransmit_count_value <= 15:
        raise ValueError(f"Unsupported ESB retransmit count: {retransmit_count}")
    if not ESB_RETRANSMIT_DELAY_MIN_US <= ack_window_us_value <= ESB_RETRANSMIT_DELAY_MAX_US:
        raise ValueError(f"Unsupported ESB ACK window: {ack_window_us}")
    if not 0 <= noack_percent_value <= ESB_NOACK_PERCENT_MAX:
        raise ValueError(f"Unsupported ESB noack percent: {noack_percent}")
    return bytes([
        DEVICE_RUNTIME_ESB_MODE_CONFIG_COMMAND & 0xFF,
        (DEVICE_RUNTIME_ESB_MODE_CONFIG_COMMAND >> 8) & 0xFF,
        mode_value & 0xFF,
        tx_power_code_value & 0xFF,
        retransmit_count_value & 0xFF,
        0x00,
        ack_window_us_value & 0xFF,
        (ack_window_us_value >> 8) & 0xFF,
        noack_percent_value & 0xFF,
        0x00,
    ])


def build_mode0_quant_config_command(
    bit_depth=MODE0_QUANT_BITS_DEFAULT,
    full_scale_uv=MODE0_QUANT_FULL_SCALE_UV_DEFAULT,
):
    try:
        bits = int(bit_depth)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Unsupported Mode0 quantization bit depth: {bit_depth}") from e
    if not MODE0_QUANT_BITS_MIN <= bits <= MODE0_QUANT_BITS_MAX:
        raise ValueError(f"Unsupported Mode0 quantization bit depth: {bit_depth}")

    try:
        full_scale = float(full_scale_uv)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Unsupported Mode0 quantization range: {full_scale_uv}") from e
    if not math.isfinite(full_scale):
        raise ValueError(f"Unsupported Mode0 quantization range: {full_scale_uv}")
    full_scale_int = int(round(full_scale))
    if (
        full_scale_int < int(MODE0_QUANT_FULL_SCALE_UV_MIN)
        or full_scale_int > int(MODE0_QUANT_FULL_SCALE_UV_MAX)
        or full_scale_int % int(MODE0_QUANT_FULL_SCALE_UV_STEP) != 0
    ):
        raise ValueError(f"Unsupported Mode0 quantization range: {full_scale_uv}")

    return bytes([
        DEVICE_MODE0_QUANT_CONFIG_COMMAND & 0xFF,
        (DEVICE_MODE0_QUANT_CONFIG_COMMAND >> 8) & 0xFF,
        bits & 0xFF,
        0x00,
        full_scale_int & 0xFF,
        (full_scale_int >> 8) & 0xFF,
        0x00,
        0x00,
    ])


"""""""""""""""""""""""""""""""""""""""""""Main Class"""""""""""""""""""""""""""""""""""""""""""""""""""""""
class SerialPort(QThread):
    GUIUpdate = pyqtSignal(list) # for Recording modes
    ThresholdSamplesUpdate = pyqtSignal(object) # compact Mode1 raw samples for threshold sweep
    EmptyGUIUpdate = pyqtSignal(list) # for idle mode
    CameraGUIUpdate = pyqtSignal(list) # for camera recording
    ProgressUpdate = pyqtSignal(int, float, float) # mode, progress_percent, run_time_min
    StatusUpdate = pyqtSignal(object) # low-rate compact status for master console
    ImpedanceProgress = pyqtSignal(object)
    ImpedanceResult = pyqtSignal(object)
    SerialDisconnected = pyqtSignal(str)  # emitted with error message when serial unexpectedly disconnects

    def __init__(self ,port ,buand) -> None:
        super(SerialPort ,self).__init__()

        """ GUI system """

        """ Data stream """
        self.port = serial.Serial(port ,buand, timeout=0.2, write_timeout=1)
        self.port.close() # close the port to avoid the error at the begining of Serial initalization
        self.port_name = port
        self._io_lock = threading.Lock()
        self._data_lock = threading.RLock()
        self.last_packet_monotonic = 0.0
        self.last_write_monotonic = 0.0
        self.last_error = ""
        self._disconnect_emitted = False

        # alignment 时间对齐策略： 每次记录
        """ 
        时间对齐：60分钟一个文件
        """
        self.Timestamp_recorder_counter = 0 # for information print
        self.Timestamp_HABITS_Trial = 0 # onset of one trial
        self.Timestamp_neural_signal = 0 # Raw timestamp of neural signal
        self.alignment_counter = 0
        self.current_alignment_value = 1
        self._alignment_counters_by_stream = {}

        self.GUIUpdateInterval = int(GUI_INTERVAL_DEFAULTS[0]) # packets num
        # Limit cross-thread GUI signal rate to avoid flooding main thread.
        self.gui_emit_min_interval_s = 0.0
        self.gui_emit_force_multiplier = 6
        self._last_gui_emit_monotonic = 0.0
        self._last_gui_emit_monotonic_by_stream = {}
        self.gui_update_intervals_by_mode = dict(GUI_INTERVAL_DEFAULTS)
        self.gui_update_interval_best_by_mode = dict(GUI_INTERVAL_DEFAULTS)
        self.gui_update_interval_loss_best_by_mode = {mode: None for mode in GUI_INTERVAL_DEFAULTS}
        self.gui_update_intervals_by_stream = dict(GUI_STREAM_INTERVAL_DEFAULTS)
        self.gui_update_interval_best_by_stream = dict(GUI_STREAM_INTERVAL_DEFAULTS)
        self.gui_update_interval_loss_best_by_stream = {stream: None for stream in GUI_STREAM_INTERVAL_DEFAULTS}
        self.gui_update_interval_loss_ewma_by_stream = {stream: None for stream in GUI_STREAM_INTERVAL_DEFAULTS}
        self.gui_update_interval_stable_windows_by_stream = {stream: 0 for stream in GUI_STREAM_INTERVAL_DEFAULTS}
        self.gui_update_interval_last_change_monotonic_by_stream = {stream: 0.0 for stream in GUI_STREAM_INTERVAL_DEFAULTS}
        self.gui_update_interval_loss_windows = {
            mode: {"missing": 0, "received": 0, "last_refresh": time.monotonic()}
            for mode in GUI_INTERVAL_DEFAULTS
        }
        self.gui_update_interval_loss_windows_by_stream = {
            stream: {"missing": 0, "received": 0, "last_refresh": time.monotonic()}
            for stream in GUI_STREAM_INTERVAL_DEFAULTS
        }
        self.gui_update_interval_stats = {
            mode: {
                "interval": int(GUI_INTERVAL_DEFAULTS[mode]),
                "best_interval": int(GUI_INTERVAL_DEFAULTS[mode]),
                "best_loss_percent": None,
                "last_loss_percent": 0.0,
                "last_expected_packets": 0,
                "updated_epoch": 0.0,
            }
            for mode in GUI_INTERVAL_DEFAULTS
        }
        self.gui_update_interval_stream_stats = {
            stream: {
                "interval": int(GUI_STREAM_INTERVAL_DEFAULTS[stream]),
                "best_interval": int(GUI_STREAM_INTERVAL_DEFAULTS[stream]),
                "best_loss_percent": None,
                "last_loss_percent": 0.0,
                "loss_ewma_percent": 0.0,
                "last_expected_packets": 0,
                "stable_windows": 0,
                "updated_epoch": 0.0,
                "last_action": "init",
            }
            for stream in GUI_STREAM_INTERVAL_DEFAULTS
        }
        self.gui_update_interval_refresh_s = 1.0
        self.gui_update_interval_loss_ewma_alpha = 0.35
        self.gui_update_interval_decrease_stable_windows = 3
        self.gui_update_interval_change_cooldown_s = 3.0
        self.gui_update_interval_system_pressure_last_mono = 0.0
        self.gui_update_interval_target_fps_enabled = True
        self.gui_update_interval_target_fps_by_stream = dict(GUI_STREAM_TARGET_FPS_DEFAULTS)
        self.status_emit_interval_s = 1.0
        self._last_status_emit_monotonic = 0.0
        self.detail_enabled = False
        self.detail_emit_min_interval_s = 0.0
        self._last_detail_emit_monotonic = 0.0
        self._last_detail_emit_monotonic_by_stream = {}
        self.detail_backpressure_active = False
        self.detail_backpressure_reason = ""
        self.detail_payload_dropped_frames = 0
        self.detail_payload_throttled = False
        self.detail_payload_last_skip_reason = ""
        self.threshold_samples_enabled = False
        self.current_stream_mode = STREAM_MODE_IDLE
        self._recent_stream_mode_tokens = coll.deque(maxlen=6)
        self.last_progress_percent = 0.0
        self.last_run_time_min = 0.0
        self.last_packet_loss = 0
        self.last_packet_count = 0
        self.last_packet_loss_batch = 0
        self.last_packet_count_batch = 0
        self.last_packet_loss_percent_current = 0.0
        self.last_packet_expected_current = 0
        self.packet_metrics_sequence = 0
        self.last_packet_decode_ms = 0.0
        self.last_data_process_ms = 0.0
        self.last_save_control_ms = 0.0
        self.last_gui_update_ms = 0.0
        self.last_pipeline_process_ms = 0.0
        self.serial_backlog_event_count = 0
        self.serial_backlog_last_bytes = 0
        self.serial_backlog_peak_bytes = 0
        self.serial_backlog_last_epoch = 0.0
        self.serial_backlog_active = False
        self.serial_frame_false_marker_count = 0
        self.serial_frame_incomplete_wait_count = 0
        self.last_battery_triplet = (0.0, 0.0, 0.0)
        self.power_guard = _default_power_guard_status()
        self.mode0_power_estimator_config = _normalize_mode0_power_estimator_config({})
        self.mode0_power = _default_mode0_power_status()
        self._mode0_power_last_telemetry_seq = None
        self.mode0_quant_bits = MODE0_QUANT_BITS_DEFAULT
        self.mode0_quant_full_scale_uv = MODE0_QUANT_FULL_SCALE_UV_DEFAULT

        self.rssi = 0 # real-time rssi
        """ neural signal buffer """
        # mode 0 & 3
        self.lfptimestamp_GUI = []
        self.lfppacketindex_GUI = []
        self.lfpdata_GUI = [[] for _ in range(16)]
        self.ESAdata_GUI = [[] for _ in range(16)]
        self.mode0rawdata_GUI = []
        self.mode0rawchannel_GUI = []
        self.alignment_GUI = [] # Alignment signal buffer
        # sensor
        self.sensordata_GUI = [[] for _ in range(9)]
        #######
        ####### test modes
        # spike mode 1
        self.spiketimestamp_GUI = []
        self.spikepacketindex_GUI = []
        self.spikedata_GUI = [] # only 1 channel
        self.spikerasterdata_GUI = [[] for _ in range(16)] 
        self._spike_gui_expected_step = 1
        # spike mode 2
        self.spiketimestamp_mode2_GUI = []
        self.spikepacketindex_mode2_GUI = []
        self.spikechannel_mode2_GUI = []
        self.spikedata_mode2_GUI = [[] for _ in range(16)] # 16 channel with 4 active channels
        self.alignment_mode2_GUI = []

        """ packages processing """
        self.DAC_resolution = 1/(int('ffff' ,16)) * 1.225 * 2 # 1.225 is the reference voltage of the series of RHD2000 (bipolar ADC)

        """ raw data packets index"""
        # mode 0
        self.raw_data_per_packet_channel = 4
        self.raw_data_index_base = np.arange(9, 9 + self.raw_data_per_packet_channel, dtype=np.int64)
        # mode 3
        self.raw_data_per_packet_mode3 = 2
        self.raw_data_index_base_mode3 = np.arange(9, 9 + self.raw_data_per_packet_mode3, dtype=np.int64)
        # sensors
        self.sensor_index = np.arange(9, dtype=np.int64)
        
        """ Test Modes """
        # mode 1
        self.spike_channel_index_mode1_3 = 16 # mode 1
        self.mode1_raw_channel_gui = 0
        self.mode3_raw_channel_gui = 0
        self.mode3_reref_status = 0
        # mode 2
        self.spike_FIFO_mode2 = coll.deque(maxlen=1000)
        self.spike_raw_channel = list(range(MODE2_V2_CHANNELS)) # Mode2 v2 streams all raw channels.
        self.mode2_selected_channels_gui = list(range(MODE2_V2_CHANNELS))
         # remove duplicate packets and sorting acoording to timestamp and packets_index
        self.spike_maxlen_mode2 = 10

        """ Logging """
        self.sample_times = 0 # 记录 sample 开始的次数；也就是暂停sample 的次数
        
        """ File saving """
        # counter
        self.LFPRawCounter = 0 # Mode 0 LFP recording file counter
        self.Mode3RawCounter = 0 # Mode 3
        self.Mode2RawCounter = 0 # Mode 2
        self.SPIKERawCounter = 0 # Mode 1

        self.lfp_file_addr = ''
        self.mode3_file_addr = ''

        self.mode1_file_addr = ''
        self.mode2_file_addr = ''
        
        self.save_file_lfp_flag = False
        self.save_file_mode1_flag = False
        self.save_file_mode2_flag = False
        self.save_file_mode3_flag = False
        # LFP raw data or spike raw data
        self.raw_data = get_raw_data_container() # mode 0
        # Action potiential events & other recorded data
        self.sensors_data = get_events_data_container() 
        # Action potiential events data
        self.AP_data = spike_data_container() # mode 1
        self.AP_LFP_data = get_raw_data_container() # mode 2
        self.mode2_sensors_data = get_events_data_container()
        self.mode2_sensor_timestamps = []
        self.ESA_data = get_mode3_data_container() # mode 3
        self._sensor_keys_no_flag = [k for k in self.sensors_data.keys() if k != "UpdateFlag"]
        self._mode0_sensor_keys_no_flag = [k for k in MODE0_SENSOR_KEYS if k in self.sensors_data]
        self._mode0_pending = None
        self._mode3_pending = None
        self._mode3_encoded_pending = None
        self._mode3_raw_pending = None
        self._mode3_raw_encoded_pending = None
        self._mode0_pending_packets = 0
        self._mode3_pending_packets = 0
        self._mode3_encoded_pending_packets = 0
        self._mode3_raw_pending_packets = 0
        self._mode3_raw_encoded_pending_packets = 0
        self._save_chunk_packets_mode0 = 50
        self._save_chunk_packets_mode3 = 25
        self._save_chunk_packets_mode3_raw = 25
        self.save_queue = multiprocessing.Queue()
        self.save_process = multiprocessing.Process(target=save_process_main, args=(self.save_queue,), daemon=True)
        self.save_process.start()
        self.pipeline = self._create_data_pipeline()
        if self.pipeline is not None:
            self.pipeline.start()
        
        # Progress bucket trackers for non-continuous counters (trigger every 500 increment bucket)
        self._lfp_progress_bucket = -1
        self._mode3_progress_bucket = -1
        self._mode1_progress_bucket = -1
        self._mode2_progress_bucket = -1
        self.progress_emit_interval_s = 10.0
        self._last_progress_emit_monotonic = {}
        
        self.sensor_fs = 0
        self.file_duration_lfp = 1000 * 60 * 30 # minutes
        self.file_duration_mode1 = 1000 * 60 * 10
        self.file_duration_mode2 = 1000 * 60  * 10
        self.file_duration_mode3 = 1000 * 60 * 60 # minutes 考虑到一个trial block 最大 60分钟
        # packets; default: equal to self.GUIUpdateInterval; in 1khz LFP: it's 12s；# 这个值不能设置太大，否则会导致缓存问题
        self.file_size_lfp = self.file_duration_lfp // 4.0 # ~1khz 4points per channel one packets 这样保证每一个文件的大小都是一样的，但对应的数据duration不一定（丢包问题）
        self.file_size_mode1 = self.file_duration_mode1 // 4.32 # 90points per channel at 20833Hz, single channel one packets
        self.mode2_packet_samples = MODE2_V2_PACKET_SAMPLES
        self.mode2_sample_frequency = MODE2_V2_FS
        self.file_size_mode2 = self.file_duration_mode2 // (
            1000.0 * self.mode2_packet_samples / self.mode2_sample_frequency
        )
        self.file_size_mode3 = self.file_duration_mode3 // 2.0 # 2points per channel at 1000Hz, 16 channel LFP&ESA (1Khz + 1Khz) one packets
        
        self.overflowSignal = [0, 0] # last and current

        self.LFP_max_interval = 5 # the maximum interval between raw data packets: 1khz 
        self.Spike_max_interval = 6 # same as above but for the minimum value
        self.mode2_max_interval = 2
        self.mode3_max_interval = 3
        self.mode3_raw_max_interval = 14
        self.mode3_thresholds_uv = [DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * 16
        self.impedance_test_active = False
        self.impedance_last_result = None

        """ IMU & LC data recording """
        self.sensor_update_flag = None
        self.sensor_name = list(self.sensors_data.keys())
        self.batteryStatus = 0
        
        # Pre-allocate sensor buffer to avoid repeated creation
        self.temp_sensor_data_buffer = np.zeros(9, dtype=np.float32)
        self._transition_grace_until_ms = 0.0
        self._transition_pending_reset = False
        self._save_grace_until_ms = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0}
        self._running = False
        self._closing = False
        # Keep the serial drain path as close as possible to the legacy GUI:
        # Windows can round sub-millisecond sleeps up to ~15 ms, which is long
        # enough for the relay USB FIFO to build periodic bursts.
        self.serial_idle_sleep_s = 0.0
        self.save_queue_drop_count = 0
        self._last_save_queue_drop_status_monotonic = 0.0
        self.save_queue_put_timeout_s = 0.25
        self.save_process_shutdown_timeout_s = 5.0
        self.serial_backlog_warning_threshold_bytes = 32768
        self.serial_backlog_recovery_timeout_s = 2.0
        self._last_serial_backlog_force_emit_monotonic = 0.0
        self.serial_read_gap_warning_threshold_ms = 80.0
        self.serial_read_gap_trace_threshold_ms = 5.0
        self.serial_read_gap_trace = coll.deque(maxlen=80)
        self.serial_read_gap_trace_seq = 0
        self.serial_read_gap_event_count = 0
        self.last_serial_read_gap_ms = 0.0
        self.serial_read_gap_peak_ms = 0.0
        self.serial_read_gap_last_epoch = 0.0
        self.serial_mode2_direct_frame_count = 0
        self.serial_mode2_direct_packet_count = 0
        self.serial_mode2_resync_count = 0
        self.last_read_batch_bytes = 0
        self.read_batch_bytes_by_mode = {mode: 0.0 for mode in GUI_INTERVAL_DEFAULTS}
        self.read_batch_packets_by_mode = {mode: 0.0 for mode in GUI_INTERVAL_DEFAULTS}
        self._read_batch_ewma_windows = {
            mode: {"ewma": 0.0, "count": 0, "last_refresh": time.monotonic()}
            for mode in GUI_INTERVAL_DEFAULTS
        }
        self._read_batch_packet_ewma_windows = {
            mode: {"ewma": 0.0, "count": 0, "last_refresh": time.monotonic()}
            for mode in GUI_INTERVAL_DEFAULTS
        }
        self.read_batch_stats_refresh_s = 10.0
        self.read_batch_ewma_alpha = 0.2
        self.port_in_waiting_before_read = 0
        self.last_save_enqueue_ms = 0.0
        self.last_save_enqueue_type = ""
        self.last_save_enqueue_epoch = 0.0
        self.last_packet_gap_missing = 0
        self.last_packet_gap_summary = ""
        self.last_packet_gap_diagnostics = {}
        self.packet_gap_event_count = 0
        self.packet_out_of_order_event_count = 0
        self.last_packet_out_of_order_summary = ""
        self.packet_counter_reset_event_count = 0
        self.last_packet_counter_reset_summary = ""
        self._last_serial_read_gap_force_emit_monotonic = 0.0
        self._last_packet_gap_force_emit_monotonic = 0.0
        self._packet_loss_counters = {
            bucket: {"missing": 0, "received": 0}
            for bucket in PACKET_LOSS_ALL_BUCKETS
        }
        self._packet_loss_last_batch = {
            bucket: {"missing": 0, "received": 0}
            for bucket in PACKET_LOSS_ALL_BUCKETS
        }
        self._packet_index_trackers = {
            bucket: {"last": None, "expected_step": 1}
            for bucket in PACKET_LOSS_ALL_BUCKETS
        }
        self._packet_loss_warmup_by_bucket = {
            bucket: 0
            for bucket in PACKET_LOSS_ALL_BUCKETS
        }

    def _create_data_pipeline(self):
        if SerialDataPipeline is None:
            return None
        try:
            return SerialDataPipeline(
                self._process_pipeline_raw_frame,
                raw_queue_capacity=4096,
                event_queue_capacity=128,
                worker_poll_seconds=0.002,
                raw_queue_put_timeout_seconds=0.25,
            )
        except Exception:
            logging.error("Failed to create serial data pipeline", exc_info=True)
            return None

    def _stop_data_pipeline(self, timeout=2.0):
        pipeline = getattr(self, "pipeline", None)
        if pipeline is None or not hasattr(pipeline, "stop"):
            return
        try:
            pipeline.stop(timeout=timeout)
        except TypeError:
            try:
                pipeline.stop()
            except Exception:
                logging.error("Failed to stop serial data pipeline", exc_info=True)
        except Exception:
            logging.error("Failed to stop serial data pipeline", exc_info=True)

    def _submit_raw_frame_to_pipeline(self, frame):
        pipeline = getattr(self, "pipeline", None)
        if pipeline is None:
            self._process_complete_frame(frame)
            return True
        try:
            if hasattr(pipeline, "submit_raw_frame"):
                accepted_result = pipeline.submit_raw_frame(bytes(frame))
            else:
                accepted_result = pipeline.submit_frame(bytes(frame))
            accepted = True if accepted_result is None else bool(accepted_result)
        except Exception:
            logging.error("Failed to submit raw frame to serial data pipeline", exc_info=True)
            accepted = False
        if not accepted:
            try:
                self.serial_backlog_event_count = int(getattr(self, "serial_backlog_event_count", 0) or 0) + 1
                self.serial_backlog_active = True
                self.serial_backlog_last_epoch = float(time.time())
                self._emit_status_update(force=True)
            except Exception:
                pass
        return accepted

    def _drain_pipeline_events(self):
        pipeline = getattr(self, "pipeline", None)
        if pipeline is None or not hasattr(pipeline, "drain_events"):
            return
        try:
            events = pipeline.drain_events()
        except Exception:
            logging.error("Failed to drain serial data pipeline events", exc_info=True)
            return
        for event in events:
            self._handle_pipeline_event(event)

    def _handle_pipeline_event(self, event):
        if event is None:
            return
        event_type = ""
        frame = None
        if isinstance(event, dict):
            event_type = str(event.get("type", "") or "")
            frame = event.get("frame")
        else:
            event_type = str(getattr(event, "kind", "") or "")
            payload = getattr(event, "payload", None)
            if isinstance(payload, dict):
                frame = payload.get("frame")
        if event_type in {"raw_frame", "frame"} and frame is not None:
            self._process_complete_frame(frame)
        elif event_type == "pipeline_error":
            try:
                payload = getattr(event, "payload", None)
                if isinstance(payload, dict):
                    self.last_error = str(payload.get("error", "") or "")
            except Exception:
                pass
            self._emit_status_update(force=True)

    def _process_pipeline_raw_frame(self, raw_frame):
        frame = raw_frame.data if RawFrame is not None and isinstance(raw_frame, RawFrame) else raw_frame
        self._process_complete_frame(frame)
        return []

    def _process_complete_frame(self, frame):
        pipeline_started = time.monotonic()
        with self._data_lock:
            try:
                decode_started = time.monotonic()
                self.data_process_full(frame)
                data_process_ms = max(0.0, (time.monotonic() - decode_started) * 1000.0)
                self.last_data_process_ms = data_process_ms
                self.last_packet_decode_ms = data_process_ms
            except Exception as e:
                self.last_data_process_ms = max(0.0, (time.monotonic() - decode_started) * 1000.0)
                self.last_packet_decode_ms = self.last_data_process_ms
                logging.error(f"Error in data_process_full: {e}", exc_info=True)
            try:
                save_control_started = time.monotonic()
                self.data_file_saving_control()
                self.last_save_control_ms = max(0.0, (time.monotonic() - save_control_started) * 1000.0)
            except Exception as e:
                self.last_save_control_ms = max(0.0, (time.monotonic() - save_control_started) * 1000.0)
                logging.error(f"Error in data_file_saving_control: {e}", exc_info=True)
            try:
                gui_update_started = time.monotonic()
                self.GUIUpate_enable()
                self.last_gui_update_ms = max(0.0, (time.monotonic() - gui_update_started) * 1000.0)
            except Exception as e:
                self.last_gui_update_ms = max(0.0, (time.monotonic() - gui_update_started) * 1000.0)
                logging.error(f"Error in GUIUpate_enable: {e}", exc_info=True)
        self.last_pipeline_process_ms = max(0.0, (time.monotonic() - pipeline_started) * 1000.0)
        self._emit_status_update()

    def _port_is_open(self):
        try:
            return bool(getattr(self.port, "is_open"))
        except Exception:
            try:
                return bool(self.port.isOpen())
            except Exception:
                return False

    def _normalize_write_payload(self, data):
        if isinstance(data, bytes):
            return data
        if isinstance(data, bytearray):
            return bytes(data)
        if isinstance(data, memoryview):
            return data.tobytes()
        if isinstance(data, str):
            return data.encode("utf-8")
        if isinstance(data, (list, tuple, np.ndarray)):
            return bytes(bytearray(int(x) & 0xFF for x in data))
        raise TypeError(f"Unsupported serial payload type: {type(data)!r}")

    def _notify_disconnect(self, reason):
        if self._closing:
            return
        self.last_error = str(reason)
        self._running = False
        if self._disconnect_emitted:
            return
        self._disconnect_emitted = True
        try:
            self.SerialDisconnected.emit(self.last_error)
        except RuntimeError:
            pass

    def port_open(self):
        """ seiral ports opening """
        #### read parameters
        if not self._port_is_open():
            self.port.open()
        self._closing = False
        self._running = True
        self._disconnect_emitted = False
        now = time.monotonic()
        self.last_packet_monotonic = now
        self.last_write_monotonic = now
    
    def _shutdown_save_process(self, timeout=None):
        q = getattr(self, "save_queue", None)
        proc = getattr(self, "save_process", None)
        try:
            timeout_s = float(timeout if timeout is not None else getattr(self, "save_process_shutdown_timeout_s", 2.0))
        except Exception:
            timeout_s = 2.0
        if q is not None:
            try:
                q.put({"type": "shutdown"}, block=True, timeout=min(0.5, max(0.05, timeout_s)))
            except TypeError:
                try:
                    q.put({"type": "shutdown"}, block=False)
                except Exception:
                    pass
            except Exception:
                pass
        if proc is not None:
            try:
                proc.join(timeout=max(0.0, timeout_s))
            except Exception:
                pass
            try:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=0.5)
            except Exception:
                pass
        if q is not None:
            try:
                q.cancel_join_thread()
            except Exception:
                pass
            try:
                q.close()
            except Exception:
                pass
        self.save_queue = None
        self.save_process = None

    def port_close(self, finalize=True):
        """ serial ports closing """
        self._running = False
        self._closing = True
        self._stop_data_pipeline()
        if finalize:
            try:
                self.finalize_save_buffers()
            except Exception:
                logging.error("Failed to finalize save buffers during port close", exc_info=True)
        try:
            if self._port_is_open():
                self.port.close()
        except Exception:
            pass
        self._shutdown_save_process()

    def stop(self, finalize=True):
        self._running = False
        self._closing = True
        self._stop_data_pipeline()
        if finalize:
            try:
                self.finalize_save_buffers()
            except Exception:
                logging.error("Failed to finalize save buffers during serial stop", exc_info=True)
        try:
            if self._port_is_open():
                self.port.close()
        except Exception:
            pass

    def _save_put(self, msg):
        q = getattr(self, "save_queue", None)
        if q is None:
            return False
        enqueue_started = time.monotonic()
        msg_type = ""
        try:
            msg_type = str(msg.get("type", "") or "") if isinstance(msg, dict) else ""
        except Exception:
            msg_type = ""
        is_full_edf_task = isinstance(msg, dict) and "mode" in msg and "raw_data" in msg
        if not msg_type and is_full_edf_task:
            try:
                msg_type = f"mode_{int(msg.get('mode', -1))}_full_task"
            except Exception:
                msg_type = "full_edf_task"
        payload = pack_for_queue(msg, prefer_shared_memory=False)
        self.last_save_enqueue_type = msg_type
        try:
            save_queue_timeout_s = max(
                0.01,
                float(getattr(self, "save_queue_put_timeout_s", 0.25) or 0.25),
            )
        except Exception:
            save_queue_timeout_s = 0.25
        try:
            q.put(
                payload,
                block=True,
                timeout=save_queue_timeout_s,
            )
            self.last_save_enqueue_ms = max(0.0, (time.monotonic() - enqueue_started) * 1000.0)
            self.last_save_enqueue_epoch = float(time.time())
            return True
        except Exception:
            try:
                unpack_from_queue(payload)
            except Exception:
                pass
            self.last_save_enqueue_ms = max(0.0, (time.monotonic() - enqueue_started) * 1000.0)
            self.last_save_enqueue_epoch = float(time.time())
            self.save_queue_drop_count = int(getattr(self, "save_queue_drop_count", 0) or 0) + 1
            now_mono = time.monotonic()
            if (now_mono - float(getattr(self, "_last_save_queue_drop_status_monotonic", 0.0) or 0.0)) >= 1.0:
                self._last_save_queue_drop_status_monotonic = now_mono
                self._emit_status_update(force=True)
            return False

    def set_detail_enabled(self, enabled):
        self.detail_enabled = bool(enabled)
        self.detail_payload_dropped_frames = 0
        self.detail_payload_throttled = False
        self.detail_payload_last_skip_reason = ""
        self._last_detail_emit_monotonic = 0.0
        self._last_detail_emit_monotonic_by_stream = {}
        if not self.detail_enabled:
            return

    def configure_detail_stream(self, min_interval_ms=None, threshold_samples_enabled=None):
        if min_interval_ms is not None:
            try:
                self.detail_emit_min_interval_s = max(0.0, float(min_interval_ms) / 1000.0)
            except Exception:
                self.detail_emit_min_interval_s = 0.0
        if threshold_samples_enabled is not None:
            self.threshold_samples_enabled = bool(threshold_samples_enabled)

    def configure_save_chunks(self, mode0=None, mode3=None, mode3_raw=None):
        def _normalize(value, fallback):
            try:
                normalized = int(value)
            except Exception:
                normalized = int(fallback)
            return max(1, min(5000, normalized))

        def _fallback(attr, value):
            try:
                return getattr(self, attr)
            except Exception:
                return value

        self._save_chunk_packets_mode0 = _normalize(
            mode0,
            _fallback("_save_chunk_packets_mode0", 50),
        )
        self._save_chunk_packets_mode3 = _normalize(
            mode3,
            _fallback("_save_chunk_packets_mode3", 25),
        )
        self._save_chunk_packets_mode3_raw = _normalize(
            mode3_raw,
            _fallback("_save_chunk_packets_mode3_raw", 25),
        )

    def configure_mode0_power_estimator(self, config=None):
        self.mode0_power_estimator_config = _normalize_mode0_power_estimator_config(config or {})
        self._set_mode0_power_last_telemetry_seq(None)
        if not isinstance(self._mode0_power_runtime_attr("mode0_power"), dict):
            self.mode0_power = _default_mode0_power_status()

    def _mode0_power_runtime_attr(self, attr, default=None):
        try:
            return self.__dict__.get(attr, default)
        except Exception:
            return default

    def _set_mode0_power_last_telemetry_seq(self, value):
        try:
            self.__dict__["_mode0_power_last_telemetry_seq"] = value
        except Exception:
            try:
                self._mode0_power_last_telemetry_seq = value
            except Exception:
                pass

    def _update_mode0_power_telemetry(self, telemetry_words):
        words = _packet_words_to_list(telemetry_words)
        if len(words) != MODE0_POWER_TELEMETRY_WORDS:
            self._set_mode0_power_last_telemetry_seq(None)
            self.mode0_power = _default_mode0_power_status()
            return self.mode0_power
        try:
            telemetry_bytes = _packet_words_to_wire_bytes(words)
            telemetry_seq = int(telemetry_bytes[0])
        except Exception:
            self._set_mode0_power_last_telemetry_seq(None)
            self.mode0_power = _default_mode0_power_status()
            return self.mode0_power
        if telemetry_seq == self._mode0_power_runtime_attr("_mode0_power_last_telemetry_seq"):
            return self.mode0_power
        self.mode0_power = _parse_mode0_power_telemetry_words(
            words,
            self._mode0_power_runtime_attr("mode0_power_estimator_config", {}),
            config_normalized=True,
        )
        self._set_mode0_power_last_telemetry_seq(telemetry_seq)
        return self.mode0_power

    @staticmethod
    def _mode_json_key(mode):
        try:
            return f"mode{int(mode)}"
        except Exception:
            return "mode0"

    @staticmethod
    def _coerce_mode_int(mode, fallback=0):
        try:
            normalized = int(mode)
        except Exception:
            text = str(mode or "").strip().lower().replace("_", "")
            if text.startswith("mode"):
                try:
                    normalized = int(text[4:])
                except Exception:
                    normalized = int(fallback)
            else:
                normalized = int(fallback)
        if normalized not in GUI_INTERVAL_DEFAULTS:
                normalized = int(fallback) if int(fallback) in GUI_INTERVAL_DEFAULTS else int(fallback)
        return int(normalized)

    @staticmethod
    def _coerce_gui_stream_key(stream_key, fallback="mode0_lfp"):
        text = str(stream_key or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "0": "mode0_lfp",
            "lfp": "mode0_lfp",
            "mode0": "mode0_lfp",
            "mode0lfp": "mode0_lfp",
            "mode_0_lfp": "mode0_lfp",
            "mode0_lfp_mand_raw": "mode0_lfp",
            "mode0_lfp+mand+raw": "mode0_lfp",
            "mode0_lfp_mand": "mode0_lfp",
            "1": "mode1_raw",
            "raw": "mode1_raw",
            "spike": "mode1_raw",
            "mode1": "mode1_raw",
            "mode1raw": "mode1_raw",
            "mode_1_raw": "mode1_raw",
            "2": "mode2_raw",
            "mode2": "mode2_raw",
            "mode2raw": "mode2_raw",
            "mode_2_raw": "mode2_raw",
            "3": "mode3_lfp_esa",
            "mode3": "mode3_lfp_esa",
            "mode3lfp": "mode3_lfp_esa",
            "mode3esa": "mode3_lfp_esa",
            "mode3lfpesa": "mode3_lfp_esa",
            "mode_3_lfp_esa": "mode3_lfp_esa",
            "mode3_lfp": "mode3_lfp_esa",
            "mode3_esa": "mode3_lfp_esa",
            "mode3_rawdata": "mode3_raw",
            "mode3raw": "mode3_raw",
            "mode_3_raw": "mode3_raw",
        }
        normalized = aliases.get(text, text)
        if normalized in GUI_STREAM_INTERVAL_DEFAULTS:
            return normalized
        fallback = str(fallback or "mode0_lfp")
        return fallback if fallback in GUI_STREAM_INTERVAL_DEFAULTS else "mode0_lfp"

    @staticmethod
    def _normalize_gui_interval_value(mode, value):
        mode = SerialPort._coerce_mode_int(mode)
        try:
            interval = int(value)
        except Exception:
            interval = int(GUI_INTERVAL_DEFAULTS[mode])
        return max(int(GUI_INTERVAL_MIN[mode]), min(int(GUI_INTERVAL_MAX[mode]), interval))

    @staticmethod
    def _normalize_gui_stream_interval_value(stream_key, value):
        stream_key = SerialPort._coerce_gui_stream_key(stream_key)
        try:
            interval = int(value)
        except Exception:
            interval = int(GUI_STREAM_INTERVAL_DEFAULTS[stream_key])
        return max(
            int(GUI_STREAM_INTERVAL_MIN[stream_key]),
            min(int(GUI_STREAM_INTERVAL_MAX[stream_key]), interval),
        )

    @staticmethod
    def _normalize_gui_interval_mapping(mapping):
        normalized = {
            mode: int(GUI_INTERVAL_DEFAULTS[mode])
            for mode in GUI_INTERVAL_DEFAULTS
        }
        if isinstance(mapping, dict):
            for key, value in mapping.items():
                mode = SerialPort._coerce_mode_int(key, fallback=-1)
                if mode in normalized:
                    normalized[mode] = SerialPort._normalize_gui_interval_value(mode, value)
        return normalized

    @staticmethod
    def _fallback_gui_interval_mapping_from_self(owner):
        intervals = getattr(owner, "gui_update_intervals_by_mode", None)
        if isinstance(intervals, dict):
            return SerialPort._normalize_gui_interval_mapping(intervals)
        try:
            fallback_interval = int(getattr(owner, "GUIUpdateInterval", GUI_INTERVAL_DEFAULTS[0]) or GUI_INTERVAL_DEFAULTS[0])
        except Exception:
            fallback_interval = int(GUI_INTERVAL_DEFAULTS[0])
        return {
            mode: SerialPort._normalize_gui_interval_value(mode, fallback_interval)
            for mode in GUI_INTERVAL_DEFAULTS
        }

    @staticmethod
    def _normalize_gui_stream_interval_mapping(mapping, mode_intervals=None):
        explicit_mode_intervals = {}
        if isinstance(mode_intervals, dict):
            for key, value in mode_intervals.items():
                mode = SerialPort._coerce_mode_int(key, fallback=-1)
                if mode in GUI_INTERVAL_DEFAULTS:
                    explicit_mode_intervals[mode] = SerialPort._normalize_gui_interval_value(mode, value)
        normalized = {}
        for stream_key in GUI_STREAM_INTERVAL_DEFAULTS:
            mode = GUI_STREAM_TO_MODE[stream_key]
            fallback = explicit_mode_intervals.get(mode, GUI_STREAM_INTERVAL_DEFAULTS[stream_key])
            normalized[stream_key] = SerialPort._normalize_gui_stream_interval_value(stream_key, fallback)
        if isinstance(mapping, dict):
            for key, value in mapping.items():
                stream_key = SerialPort._coerce_gui_stream_key(key, fallback="")
                if stream_key in normalized:
                    normalized[stream_key] = SerialPort._normalize_gui_stream_interval_value(stream_key, value)
        return normalized

    @staticmethod
    def _json_gui_interval_mapping(mapping):
        mapping = SerialPort._normalize_gui_interval_mapping(mapping)
        return {
            SerialPort._mode_json_key(mode): int(mapping.get(mode, GUI_INTERVAL_DEFAULTS[mode]))
            for mode in sorted(GUI_INTERVAL_DEFAULTS)
        }

    @staticmethod
    def _json_gui_stream_interval_mapping(mapping, mode_intervals=None):
        mapping = SerialPort._normalize_gui_stream_interval_mapping(mapping, mode_intervals)
        return {
            stream_key: int(mapping.get(stream_key, GUI_STREAM_INTERVAL_DEFAULTS[stream_key]))
            for stream_key in sorted(GUI_STREAM_INTERVAL_DEFAULTS)
        }

    @staticmethod
    def _mode_intervals_from_stream_intervals(stream_intervals):
        stream_intervals = SerialPort._normalize_gui_stream_interval_mapping(stream_intervals)
        return {
            0: int(stream_intervals.get("mode0_lfp", GUI_INTERVAL_DEFAULTS[0])),
            1: int(stream_intervals.get("mode1_raw", GUI_INTERVAL_DEFAULTS[1])),
            2: int(stream_intervals.get("mode2_raw", GUI_INTERVAL_DEFAULTS[2])),
            3: max(
                int(stream_intervals.get("mode3_lfp_esa", GUI_INTERVAL_DEFAULTS[3])),
                int(stream_intervals.get("mode3_raw", GUI_INTERVAL_DEFAULTS[3])),
            ),
        }

    @staticmethod
    def _default_gui_stream_stats(stream_intervals):
        return {
            stream_key: {
                "interval": int(stream_intervals[stream_key]),
                "best_interval": int(stream_intervals[stream_key]),
                "best_loss_percent": None,
                "last_loss_percent": 0.0,
                "loss_ewma_percent": 0.0,
                "last_expected_packets": 0,
                "stable_windows": 0,
                "updated_epoch": 0.0,
                "last_action": "init",
            }
            for stream_key in GUI_STREAM_INTERVAL_DEFAULTS
        }

    def _ensure_gui_stream_interval_state(self):
        mode_intervals = SerialPort._fallback_gui_interval_mapping_from_self(self)
        stream_intervals = getattr(self, "gui_update_intervals_by_stream", None)
        stream_intervals = SerialPort._normalize_gui_stream_interval_mapping(stream_intervals, mode_intervals)
        self.gui_update_intervals_by_stream = stream_intervals
        self.gui_update_intervals_by_mode = SerialPort._mode_intervals_from_stream_intervals(stream_intervals)

        best_by_stream = getattr(self, "gui_update_interval_best_by_stream", None)
        if not isinstance(best_by_stream, dict):
            best_by_stream = dict(stream_intervals)
        self.gui_update_interval_best_by_stream = {
            stream_key: SerialPort._normalize_gui_stream_interval_value(
                stream_key,
                best_by_stream.get(stream_key, stream_intervals[stream_key]),
            )
            for stream_key in GUI_STREAM_INTERVAL_DEFAULTS
        }
        self.gui_update_interval_best_by_mode = SerialPort._mode_intervals_from_stream_intervals(
            self.gui_update_interval_best_by_stream
        )

        best_loss_by_stream = getattr(self, "gui_update_interval_loss_best_by_stream", None)
        if not isinstance(best_loss_by_stream, dict):
            best_loss_by_stream = {}
        normalized_best_loss = {}
        for stream_key in GUI_STREAM_INTERVAL_DEFAULTS:
            try:
                value = best_loss_by_stream.get(stream_key)
                normalized_best_loss[stream_key] = None if value is None else float(value)
            except Exception:
                normalized_best_loss[stream_key] = None
        self.gui_update_interval_loss_best_by_stream = normalized_best_loss

        ewma_by_stream = getattr(self, "gui_update_interval_loss_ewma_by_stream", None)
        if not isinstance(ewma_by_stream, dict):
            ewma_by_stream = {}
        self.gui_update_interval_loss_ewma_by_stream = {
            stream_key: (None if ewma_by_stream.get(stream_key) is None else float(ewma_by_stream.get(stream_key, 0.0) or 0.0))
            for stream_key in GUI_STREAM_INTERVAL_DEFAULTS
        }

        stable_by_stream = getattr(self, "gui_update_interval_stable_windows_by_stream", None)
        if not isinstance(stable_by_stream, dict):
            stable_by_stream = {}
        self.gui_update_interval_stable_windows_by_stream = {
            stream_key: max(0, int(stable_by_stream.get(stream_key, 0) or 0))
            for stream_key in GUI_STREAM_INTERVAL_DEFAULTS
        }

        last_change_by_stream = getattr(self, "gui_update_interval_last_change_monotonic_by_stream", None)
        if not isinstance(last_change_by_stream, dict):
            last_change_by_stream = {}
        self.gui_update_interval_last_change_monotonic_by_stream = {
            stream_key: max(0.0, float(last_change_by_stream.get(stream_key, 0.0) or 0.0))
            for stream_key in GUI_STREAM_INTERVAL_DEFAULTS
        }

        windows = getattr(self, "gui_update_interval_loss_windows_by_stream", None)
        now_mono = time.monotonic()
        if not isinstance(windows, dict):
            old_windows = getattr(self, "gui_update_interval_loss_windows", None)
            windows = {}
            if isinstance(old_windows, dict):
                for stream_key, mode in GUI_STREAM_TO_MODE.items():
                    if isinstance(old_windows.get(mode), dict):
                        windows[stream_key] = dict(old_windows.get(mode))
        self.gui_update_interval_loss_windows_by_stream = {
            stream_key: dict(windows.get(stream_key, {"missing": 0, "received": 0, "last_refresh": now_mono}))
            for stream_key in GUI_STREAM_INTERVAL_DEFAULTS
        }

        stats = getattr(self, "gui_update_interval_stream_stats", None)
        if not isinstance(stats, dict):
            stats = SerialPort._default_gui_stream_stats(stream_intervals)
        self.gui_update_interval_stream_stats = {
            stream_key: dict(stats.get(stream_key, SerialPort._default_gui_stream_stats(stream_intervals)[stream_key]))
            for stream_key in GUI_STREAM_INTERVAL_DEFAULTS
        }
        SerialPort._sync_gui_mode_interval_state_from_stream(self)

    def configure_gui_update_intervals(self, intervals=None, stats=None, stream_intervals=None, stream_stats=None):
        mode_intervals = SerialPort._normalize_gui_interval_mapping(intervals)
        mode_interval_source = intervals
        if (
            not isinstance(stream_intervals, dict)
            or len(stream_intervals) == 0
        ) and mode_intervals == dict(GUI_INTERVAL_DEFAULTS):
            mode_interval_source = {}
        stream_intervals = SerialPort._normalize_gui_stream_interval_mapping(stream_intervals, mode_interval_source)
        self.gui_update_intervals_by_stream = dict(stream_intervals)
        self.gui_update_interval_best_by_stream = dict(stream_intervals)
        self.gui_update_interval_loss_best_by_stream = {stream: None for stream in GUI_STREAM_INTERVAL_DEFAULTS}
        self.gui_update_interval_loss_ewma_by_stream = {stream: None for stream in GUI_STREAM_INTERVAL_DEFAULTS}
        self.gui_update_interval_stable_windows_by_stream = {stream: 0 for stream in GUI_STREAM_INTERVAL_DEFAULTS}
        self.gui_update_interval_last_change_monotonic_by_stream = {stream: 0.0 for stream in GUI_STREAM_INTERVAL_DEFAULTS}

        mode_stats = stats if isinstance(stats, dict) else {}
        incoming_stream_stats = stream_stats if isinstance(stream_stats, dict) else {}
        for stream_key in GUI_STREAM_INTERVAL_DEFAULTS:
            mode_key = SerialPort._mode_json_key(GUI_STREAM_TO_MODE[stream_key])
            item = incoming_stream_stats.get(stream_key, {})
            if not isinstance(item, dict):
                item = {}
            fallback_item = mode_stats.get(mode_key, mode_stats.get(GUI_STREAM_TO_MODE[stream_key], {}))
            if not isinstance(fallback_item, dict):
                fallback_item = {}
            best_interval = item.get("best_interval", fallback_item.get("best_interval", stream_intervals[stream_key]))
            self.gui_update_interval_best_by_stream[stream_key] = SerialPort._normalize_gui_stream_interval_value(stream_key, best_interval)
            best_loss = item.get("best_loss_percent", fallback_item.get("best_loss_percent"))
            try:
                self.gui_update_interval_loss_best_by_stream[stream_key] = None if best_loss is None else float(best_loss)
            except Exception:
                self.gui_update_interval_loss_best_by_stream[stream_key] = None
            ewma = item.get("loss_ewma_percent", item.get("last_loss_percent", fallback_item.get("last_loss_percent")))
            try:
                self.gui_update_interval_loss_ewma_by_stream[stream_key] = None if ewma is None else float(ewma)
            except Exception:
                self.gui_update_interval_loss_ewma_by_stream[stream_key] = None
            try:
                self.gui_update_interval_stable_windows_by_stream[stream_key] = max(0, int(item.get("stable_windows", 0) or 0))
            except Exception:
                self.gui_update_interval_stable_windows_by_stream[stream_key] = 0

        now_mono = time.monotonic()
        self.gui_update_interval_loss_windows_by_stream = {
            stream: {"missing": 0, "received": 0, "last_refresh": now_mono}
            for stream in GUI_STREAM_INTERVAL_DEFAULTS
        }
        self.gui_update_interval_stream_stats = SerialPort._default_gui_stream_stats(stream_intervals)
        for stream_key in GUI_STREAM_INTERVAL_DEFAULTS:
            self.gui_update_interval_stream_stats[stream_key].update({
                "best_interval": int(self.gui_update_interval_best_by_stream[stream_key]),
                "best_loss_percent": self.gui_update_interval_loss_best_by_stream.get(stream_key),
                "loss_ewma_percent": float(self.gui_update_interval_loss_ewma_by_stream.get(stream_key) or 0.0),
                "stable_windows": int(self.gui_update_interval_stable_windows_by_stream.get(stream_key, 0) or 0),
            })
        SerialPort._sync_gui_mode_interval_state_from_stream(self)
        self.GUIUpdateInterval = int(self.gui_update_intervals_by_mode.get(0, self.GUIUpdateInterval))

    def configure_gui_update_interval_control(self, control=None):
        control = control if isinstance(control, dict) else {}
        enabled = control.get("target_fps_enabled", control.get("enabled", True))
        self.gui_update_interval_target_fps_enabled = bool(enabled)
        targets = dict(GUI_STREAM_TARGET_FPS_DEFAULTS)
        incoming_targets = control.get("target_fps_by_stream", {})
        if not isinstance(incoming_targets, dict):
            incoming_targets = {}
        aliases = {
            "mode0": "mode0_lfp",
            "mode0_lfp": "mode0_lfp",
            "lfp": "mode0_lfp",
            "mode1": "mode1_raw",
            "mode1_raw": "mode1_raw",
            "raw": "mode1_raw",
            "spike": "mode1_raw",
            "mode2": "mode2_raw",
            "mode2_raw": "mode2_raw",
            "mode3": "mode3_lfp_esa",
            "mode3_lfp": "mode3_lfp_esa",
            "mode3_esa": "mode3_lfp_esa",
            "mode3_lfp_esa": "mode3_lfp_esa",
            "mode3_raw": "mode3_raw",
        }
        for key, value in {**control, **incoming_targets}.items():
            stream_key = aliases.get(str(key).strip().lower())
            if stream_key not in targets:
                continue
            try:
                target_fps = max(0.0, float(value))
                targets[stream_key] = min(float(GUI_STREAM_MAX_REFRESH_FPS), target_fps)
            except Exception:
                continue
        self.gui_update_interval_target_fps_by_stream = targets

    def _gui_interval_for_mode(self, mode):
        mode = SerialPort._coerce_mode_int(mode)
        SerialPort._ensure_gui_stream_interval_state(self)
        intervals = getattr(self, "gui_update_intervals_by_mode", {})
        return SerialPort._normalize_gui_interval_value(mode, intervals.get(mode, GUI_INTERVAL_DEFAULTS[mode]))

    def _gui_interval_for_stream(self, stream_key):
        stream_key = SerialPort._coerce_gui_stream_key(stream_key)
        SerialPort._ensure_gui_stream_interval_state(self)
        intervals = getattr(self, "gui_update_intervals_by_stream", {})
        return SerialPort._normalize_gui_stream_interval_value(
            stream_key,
            intervals.get(stream_key, GUI_STREAM_INTERVAL_DEFAULTS[stream_key]),
        )

    def _active_lfp_gui_stream(self):
        try:
            if int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE)) == STREAM_MODE_MODE3:
                return "mode3_lfp_esa"
        except Exception:
            pass
        return "mode0_lfp"

    def _active_spike_gui_stream(self):
        try:
            if int(getattr(self, "_spike_gui_expected_step", 1) or 1) == 4:
                return "mode3_raw"
        except Exception:
            pass
        try:
            if int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE)) == STREAM_MODE_MODE3:
                return "mode3_raw"
        except Exception:
            pass
        return "mode1_raw"

    def _active_lfp_gui_mode(self):
        try:
            return 3 if int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE)) == STREAM_MODE_MODE3 else 0
        except Exception:
            return 0

    def _active_spike_gui_mode(self):
        try:
            if int(getattr(self, "_spike_gui_expected_step", 1) or 1) == 4:
                return 3
        except Exception:
            pass
        try:
            return 3 if int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE)) == STREAM_MODE_MODE3 else 1
        except Exception:
            return 1

    def _mode_for_loss_bucket(self, bucket_key):
        return GUI_STREAM_TO_MODE[SerialPort._stream_for_loss_bucket(self, bucket_key)]

    def _stream_for_loss_bucket(self, bucket_key):
        bucket_key = str(bucket_key or "").strip().lower()
        if bucket_key in GUI_STREAM_TO_MODE:
            return bucket_key
        if bucket_key == "mode2":
            return "mode2_raw"
        if bucket_key == "spike":
            return SerialPort._active_spike_gui_stream(self)
        return SerialPort._active_lfp_gui_stream(self)

    def _record_gui_interval_loss_sample(self, bucket_key, missing_packets, received_packets):
        try:
            attr_dict = object.__getattribute__(self, "__dict__")
        except Exception:
            attr_dict = {}
        if not (bool(attr_dict.get("detail_enabled", False)) or bool(attr_dict.get("threshold_samples_enabled", False))):
            return
        stream_key = SerialPort._stream_for_loss_bucket(self, bucket_key)
        SerialPort._ensure_gui_stream_interval_state(self)
        windows = getattr(self, "gui_update_interval_loss_windows_by_stream", {})
        window = windows.setdefault(stream_key, {"missing": 0, "received": 0, "last_refresh": time.monotonic()})
        window["missing"] = max(0, int(window.get("missing", 0) or 0)) + max(0, int(missing_packets or 0))
        window["received"] = max(0, int(window.get("received", 0) or 0)) + max(0, int(received_packets or 0))
        SerialPort._maybe_refresh_gui_stream_interval(self, stream_key)

    def _maybe_refresh_gui_interval(self, mode):
        stream_key = {
            0: "mode0_lfp",
            1: "mode1_raw",
            2: "mode2_raw",
            3: "mode3_lfp_esa",
        }.get(SerialPort._coerce_mode_int(mode), "mode0_lfp")
        SerialPort._maybe_refresh_gui_stream_interval(self, stream_key)

    def _mode3_stream_loss_snapshot(self, stream_key, active_metrics=None):
        stream_key = SerialPort._coerce_gui_stream_key(stream_key)
        if isinstance(active_metrics, dict) and active_metrics.get("stream_key") == stream_key:
            return dict(active_metrics)
        windows = getattr(self, "gui_update_interval_loss_windows_by_stream", {})
        window = windows.get(stream_key)
        if isinstance(window, dict):
            missing = max(0, int(window.get("missing", 0) or 0))
            received = max(0, int(window.get("received", 0) or 0))
            expected = missing + received
            if expected > 0:
                loss_percent = float(100.0 * missing / expected)
                alpha = max(0.01, min(1.0, float(getattr(self, "gui_update_interval_loss_ewma_alpha", 0.35) or 0.35)))
                previous_ewma = getattr(self, "gui_update_interval_loss_ewma_by_stream", {}).get(stream_key)
                loss_ewma = loss_percent if previous_ewma is None else (alpha * loss_percent + (1.0 - alpha) * float(previous_ewma))
                snapshot = {
                    "stream_key": stream_key,
                    "loss_percent": float(loss_percent),
                    "loss_ewma": float(loss_ewma),
                    "expected": int(expected),
                    "from_window": True,
                }
                snapshot.update(SerialPort._gui_interval_pressure_snapshot(self, stream_key))
                return snapshot
        item = dict(getattr(self, "gui_update_interval_stream_stats", {}).get(stream_key, {}) or {})
        last_loss = float(item.get("last_loss_percent", 0.0) or 0.0)
        snapshot = {
            "stream_key": stream_key,
            "loss_percent": last_loss,
            "loss_ewma": float(item.get("loss_ewma_percent", last_loss) or 0.0),
            "expected": int(item.get("last_expected_packets", 0) or 0),
            "from_window": False,
        }
        snapshot.update(SerialPort._gui_interval_pressure_snapshot(self, stream_key))
        return snapshot

    def _mode3_stream_loss_reference(self, stream_key):
        stream_key = SerialPort._coerce_gui_stream_key(stream_key)
        best_loss = getattr(self, "gui_update_interval_loss_best_by_stream", {}).get(stream_key)
        if best_loss is None:
            item = dict(getattr(self, "gui_update_interval_stream_stats", {}).get(stream_key, {}) or {})
            best_loss = item.get("best_loss_percent")
        try:
            return None if best_loss is None else float(best_loss)
        except Exception:
            return None

    def _gui_interval_pressure_snapshot(self, stream_key):
        reason = SerialPort._gui_interval_system_pressure_reason(self, stream_key=stream_key)
        return {
            "pressure_score": 1.0 if reason else 0.0,
            "pressure_reason": str(reason or ""),
        }

    def _mode3_metric_is_bad(self, stream_key, snapshot):
        if int(snapshot.get("expected", 0) or 0) <= 0:
            return False
        return bool(snapshot.get("pressure_reason")) or float(snapshot.get("pressure_score", 0.0) or 0.0) > 0.0

    def _mode3_metric_is_stable(self, stream_key, snapshot):
        if int(snapshot.get("expected", 0) or 0) <= 0:
            return False
        return not bool(snapshot.get("pressure_reason")) and float(snapshot.get("pressure_score", 0.0) or 0.0) <= 0.0

    def _update_mode3_stream_stat(
        self,
        stream_key,
        *,
        interval,
        best_interval,
        best_loss,
        snapshot,
        stable_windows,
        action,
        pressure_reason="",
    ):
        self.gui_update_interval_stream_stats[stream_key] = {
            "interval": int(interval),
            "best_interval": int(best_interval),
            "best_loss_percent": None if best_loss is None else float(best_loss),
            "last_loss_percent": float(snapshot.get("loss_percent", 0.0) or 0.0),
            "loss_ewma_percent": float(snapshot.get("loss_ewma", 0.0) or 0.0),
            "last_expected_packets": int(snapshot.get("expected", 0) or 0),
            "stable_windows": int(stable_windows),
            "updated_epoch": float(time.time()),
            "last_action": str(action or "hold"),
            "pressure_reason": str(pressure_reason or ""),
            "pressure_score": float(snapshot.get("pressure_score", 0.0) or 0.0),
        }

    def _mode3_target_interval_for_refresh(self, stream_key, snapshot):
        if not bool(getattr(self, "gui_update_interval_target_fps_enabled", True)):
            return None
        stream_key = SerialPort._coerce_gui_stream_key(stream_key)
        targets = getattr(self, "gui_update_interval_target_fps_by_stream", {})
        if not isinstance(targets, dict):
            targets = dict(GUI_STREAM_TARGET_FPS_DEFAULTS)
        try:
            target_fps = float(targets.get(stream_key, 0.0) or 0.0)
        except Exception:
            target_fps = 0.0
        if target_fps <= 0.0:
            return None
        try:
            expected = int(snapshot.get("expected", 0) or 0)
        except Exception:
            expected = 0
        if expected <= 0:
            return None
        try:
            refresh_s = max(0.1, float(getattr(self, "gui_update_interval_refresh_s", 1.0) or 1.0))
        except Exception:
            refresh_s = 1.0
        packets_per_second = float(expected) / refresh_s
        target_interval = int(math.floor(packets_per_second / target_fps))
        return SerialPort._normalize_gui_stream_interval_value(stream_key, target_interval)

    def _maybe_refresh_mode3_stream_pair_interval(self, stream_key, active_snapshot, now_mono):
        stream_key = SerialPort._coerce_gui_stream_key(stream_key)
        if stream_key not in {"mode3_lfp_esa", "mode3_raw"}:
            return False
        sibling_key = "mode3_raw" if stream_key == "mode3_lfp_esa" else "mode3_lfp_esa"
        SerialPort._ensure_gui_stream_interval_state(self)

        snapshots = {
            stream_key: dict(active_snapshot),
            sibling_key: SerialPort._mode3_stream_loss_snapshot(self, sibling_key, active_snapshot),
        }
        pair_pressure_reason = str(snapshots[stream_key].get("pressure_reason", "") or "")
        if not pair_pressure_reason:
            pair_pressure_reason = SerialPort._gui_interval_system_pressure_reason(self, stream_key=stream_key)
        if pair_pressure_reason:
            for key in ("mode3_lfp_esa", "mode3_raw"):
                snapshots[key]["pressure_reason"] = pair_pressure_reason
                snapshots[key]["pressure_score"] = 1.0
                snapshots[key]["expected"] = max(1, int(snapshots[key].get("expected", 0) or 0))
        current_intervals = {
            key: SerialPort._gui_interval_for_stream(self, key)
            for key in ("mode3_lfp_esa", "mode3_raw")
        }
        best_intervals = {
            key: int(getattr(self, "gui_update_interval_best_by_stream", {}).get(key, current_intervals[key]))
            for key in ("mode3_lfp_esa", "mode3_raw")
        }
        best_losses = {
            key: SerialPort._mode3_stream_loss_reference(self, key)
            for key in ("mode3_lfp_esa", "mode3_raw")
        }
        for key in ("mode3_lfp_esa", "mode3_raw"):
            loss_ewma = float(snapshots[key].get("loss_ewma", 0.0) or 0.0)
            if best_losses[key] is None:
                if loss_ewma <= float(GUI_STREAM_INTERVAL_LOSS_EPSILON):
                    best_losses[key] = loss_ewma
                    best_intervals[key] = current_intervals[key]
                else:
                    best_losses[key] = 0.0
            elif loss_ewma < float(best_losses[key]) - float(GUI_STREAM_INTERVAL_LOSS_EPSILON):
                best_losses[key] = loss_ewma
                best_intervals[key] = current_intervals[key]
            elif loss_ewma <= float(best_losses[key]) + float(GUI_STREAM_INTERVAL_LOSS_EPSILON) and current_intervals[key] < best_intervals[key]:
                best_intervals[key] = current_intervals[key]

        bad_streams = {
            key for key in ("mode3_lfp_esa", "mode3_raw")
            if SerialPort._mode3_metric_is_bad(self, key, snapshots[key])
        }
        stable_by_stream = {
            key: max(0, int(getattr(self, "gui_update_interval_stable_windows_by_stream", {}).get(key, 0) or 0))
            for key in ("mode3_lfp_esa", "mode3_raw")
        }
        if SerialPort._mode3_metric_is_stable(self, stream_key, snapshots[stream_key]):
            stable_by_stream[stream_key] += 1
        else:
            stable_by_stream[stream_key] = 0
        if bad_streams:
            for key in ("mode3_lfp_esa", "mode3_raw"):
                stable_by_stream[key] = 0

        cooldown_s = max(0.0, float(getattr(self, "gui_update_interval_change_cooldown_s", 3.0) or 0.0))
        last_change = max(
            float(getattr(self, "gui_update_interval_last_change_monotonic_by_stream", {}).get("mode3_lfp_esa", 0.0) or 0.0),
            float(getattr(self, "gui_update_interval_last_change_monotonic_by_stream", {}).get("mode3_raw", 0.0) or 0.0),
        )
        can_change = cooldown_s <= 0.0 or last_change <= 0.0 or (now_mono - last_change) >= cooldown_s

        next_intervals = dict(current_intervals)
        actions = {key: "hold_pair" for key in ("mode3_lfp_esa", "mode3_raw")}
        if bad_streams and can_change:
            losing_interval = max(current_intervals[key] for key in bad_streams)
            for key in ("mode3_lfp_esa", "mode3_raw"):
                if key in bad_streams:
                    step = max(1, int(round(current_intervals[key] * 0.25)))
                    next_intervals[key] = SerialPort._normalize_gui_stream_interval_value(key, current_intervals[key] + step)
                    actions[key] = "increase_pair_pressure"
                elif current_intervals[key] <= losing_interval:
                    step = max(1, int(round(current_intervals[key] * 0.10)))
                    next_intervals[key] = SerialPort._normalize_gui_stream_interval_value(key, current_intervals[key] + step)
                    actions[key] = "increase_pair_budget"
            for key in ("mode3_lfp_esa", "mode3_raw"):
                if next_intervals[key] != current_intervals[key]:
                    self.gui_update_interval_last_change_monotonic_by_stream[key] = now_mono
        elif not bad_streams:
            required_stable = max(1, int(getattr(self, "gui_update_interval_decrease_stable_windows", 3) or 3))
            target_adjusted = False
            if can_change:
                for key in ("mode3_lfp_esa", "mode3_raw"):
                    target_interval = SerialPort._mode3_target_interval_for_refresh(self, key, snapshots[key])
                    if target_interval is None:
                        continue
                    if (
                        stable_by_stream[key] >= required_stable
                        and current_intervals[key] > int(target_interval)
                    ):
                        floor_interval = int(target_interval)
                        try:
                            best_interval = int(best_intervals.get(key, floor_interval) or floor_interval)
                        except Exception:
                            best_interval = floor_interval
                        best_loss = best_losses.get(key)
                        if best_loss is not None and float(best_loss) <= float(GUI_STREAM_INTERVAL_LOSS_EPSILON):
                            floor_interval = max(1, min(floor_interval, best_interval))
                        step = max(1, int(round(current_intervals[key] * 0.25)))
                        next_intervals[key] = SerialPort._normalize_gui_stream_interval_value(
                            key,
                            max(int(floor_interval), current_intervals[key] - step),
                        )
                        if next_intervals[key] != current_intervals[key]:
                            actions[key] = "decrease_pair_target_fps"
                            self.gui_update_interval_last_change_monotonic_by_stream[key] = now_mono
                            stable_by_stream[key] = 0
                            target_adjusted = True
            if (
                not target_adjusted
                and all(stable_by_stream[key] >= required_stable for key in ("mode3_lfp_esa", "mode3_raw"))
                and can_change
            ):
                decrease_key = "mode3_lfp_esa"
                if current_intervals["mode3_raw"] > current_intervals["mode3_lfp_esa"]:
                    decrease_key = "mode3_raw"
                elif current_intervals["mode3_raw"] == current_intervals["mode3_lfp_esa"]:
                    decrease_key = stream_key
                step = max(1, int(round(current_intervals[decrease_key] * 0.10)))
                next_intervals[decrease_key] = SerialPort._normalize_gui_stream_interval_value(
                    decrease_key,
                    current_intervals[decrease_key] - step,
                )
                if next_intervals[decrease_key] != current_intervals[decrease_key]:
                    actions[decrease_key] = "decrease_pair_stable"
                    self.gui_update_interval_last_change_monotonic_by_stream[decrease_key] = now_mono
                    stable_by_stream = {"mode3_lfp_esa": 0, "mode3_raw": 0}

        for key in ("mode3_lfp_esa", "mode3_raw"):
            self.gui_update_intervals_by_stream[key] = int(next_intervals[key])
            self.gui_update_interval_best_by_stream[key] = int(best_intervals[key])
            self.gui_update_interval_loss_best_by_stream[key] = best_losses[key]
            self.gui_update_interval_stable_windows_by_stream[key] = int(stable_by_stream[key])
            if bool(snapshots[key].get("from_window", False)):
                self.gui_update_interval_loss_ewma_by_stream[key] = float(snapshots[key].get("loss_ewma", 0.0) or 0.0)
            SerialPort._update_mode3_stream_stat(
                self,
                key,
                interval=next_intervals[key],
                best_interval=best_intervals[key],
                best_loss=best_losses[key],
                snapshot=snapshots[key],
                stable_windows=stable_by_stream[key],
                action=actions[key],
            )
        windows = getattr(self, "gui_update_interval_loss_windows_by_stream", {})
        for key in ("mode3_lfp_esa", "mode3_raw"):
            if key == stream_key or bool(snapshots[key].get("from_window", False)):
                item = windows.get(key)
                if isinstance(item, dict):
                    item["missing"] = 0
                    item["received"] = 0
                    item["last_refresh"] = now_mono
        SerialPort._sync_gui_mode_interval_state_from_stream(self)
        self.GUIUpdateInterval = int(self.gui_update_intervals_by_mode.get(0, self.GUIUpdateInterval))
        return True

    def _maybe_refresh_gui_stream_interval(self, stream_key):
        stream_key = SerialPort._coerce_gui_stream_key(stream_key)
        SerialPort._ensure_gui_stream_interval_state(self)
        windows = getattr(self, "gui_update_interval_loss_windows_by_stream", {})
        window = windows.get(stream_key)
        if not isinstance(window, dict):
            return
        now_mono = time.monotonic()
        refresh_s = max(1.0, float(getattr(self, "gui_update_interval_refresh_s", 1.0) or 1.0))
        last_refresh = float(window.get("last_refresh", now_mono) or now_mono)
        if (now_mono - last_refresh) < refresh_s:
            return
        missing = max(0, int(window.get("missing", 0) or 0))
        received = max(0, int(window.get("received", 0) or 0))
        expected = missing + received
        loss_percent = 0.0 if expected <= 0 else float(100.0 * missing / expected)
        current_interval = SerialPort._gui_interval_for_stream(self, stream_key)
        best_loss = getattr(self, "gui_update_interval_loss_best_by_stream", {}).get(stream_key)
        best_interval = int(getattr(self, "gui_update_interval_best_by_stream", {}).get(stream_key, current_interval))
        alpha = max(0.01, min(1.0, float(getattr(self, "gui_update_interval_loss_ewma_alpha", 0.35) or 0.35)))
        previous_ewma = getattr(self, "gui_update_interval_loss_ewma_by_stream", {}).get(stream_key)
        loss_ewma = loss_percent if previous_ewma is None else (alpha * loss_percent + (1.0 - alpha) * float(previous_ewma))
        self.gui_update_interval_loss_ewma_by_stream[stream_key] = float(loss_ewma)
        pressure_snapshot = SerialPort._gui_interval_pressure_snapshot(self, stream_key)
        if stream_key in {"mode3_lfp_esa", "mode3_raw"}:
            active_snapshot = {
                "stream_key": stream_key,
                "loss_percent": float(loss_percent),
                "loss_ewma": float(loss_ewma),
                "expected": int(expected),
                "from_window": True,
            }
            active_snapshot.update(pressure_snapshot)
            handled = SerialPort._maybe_refresh_mode3_stream_pair_interval(
                self,
                stream_key,
                active_snapshot,
                now_mono,
            )
            if handled:
                return
        epsilon = float(GUI_STREAM_INTERVAL_LOSS_EPSILON)
        stable_windows = max(0, int(getattr(self, "gui_update_interval_stable_windows_by_stream", {}).get(stream_key, 0) or 0))
        action = "hold"
        if best_loss is None:
            best_loss = loss_ewma
            best_interval = current_interval
            stable_windows = 0
            action = "baseline"
        elif loss_ewma < (float(best_loss) - epsilon):
            best_loss = loss_ewma
            best_interval = current_interval
        elif loss_ewma <= float(best_loss) + epsilon and current_interval < best_interval:
            best_interval = current_interval
        increase_step = max(1, int(round(current_interval * 0.25)))
        decrease_step = max(1, int(round(current_interval * 0.10)))
        cooldown_s = max(0.0, float(getattr(self, "gui_update_interval_change_cooldown_s", 3.0) or 0.0))
        last_change = float(getattr(self, "gui_update_interval_last_change_monotonic_by_stream", {}).get(stream_key, 0.0) or 0.0)
        can_change = cooldown_s <= 0.0 or last_change <= 0.0 or (now_mono - last_change) >= cooldown_s
        pressure_reason = str(pressure_snapshot.get("pressure_reason", "") or "")
        is_bad = bool(pressure_reason)
        is_stable = not is_bad
        if is_stable and expected > 0:
            stable_windows += 1
        else:
            stable_windows = 0
        if expected <= 0:
            next_interval = current_interval
            action = "hold_empty"
        elif is_bad and can_change:
            next_interval = current_interval + increase_step
            stable_windows = 0
            action = "increase_pressure"
            self.gui_update_interval_last_change_monotonic_by_stream[stream_key] = now_mono
        elif (
            is_stable
            and stable_windows >= max(1, int(getattr(self, "gui_update_interval_decrease_stable_windows", 3) or 3))
            and can_change
        ):
            target_interval = SerialPort._mode3_target_interval_for_refresh(
                self,
                stream_key,
                {
                    "stream_key": stream_key,
                    "loss_percent": float(loss_percent),
                    "loss_ewma": float(loss_ewma),
                    "expected": int(expected),
                },
            )
            if target_interval is not None and current_interval > int(target_interval):
                next_interval = max(int(target_interval), current_interval - max(increase_step, decrease_step))
                action = "decrease_target_fps"
            else:
                next_interval = current_interval - decrease_step
                action = "decrease_stable"
            stable_windows = 0
            self.gui_update_interval_last_change_monotonic_by_stream[stream_key] = now_mono
        else:
            next_interval = current_interval
        next_interval = SerialPort._normalize_gui_stream_interval_value(stream_key, next_interval)
        self.gui_update_intervals_by_stream[stream_key] = int(next_interval)
        self.gui_update_interval_best_by_stream[stream_key] = int(best_interval)
        self.gui_update_interval_loss_best_by_stream[stream_key] = best_loss
        self.gui_update_interval_stable_windows_by_stream[stream_key] = int(stable_windows)
        self.gui_update_interval_stream_stats[stream_key] = {
            "interval": int(next_interval),
            "best_interval": int(best_interval),
            "best_loss_percent": None if best_loss is None else float(best_loss),
            "last_loss_percent": float(loss_percent),
            "loss_ewma_percent": float(loss_ewma),
            "last_expected_packets": int(expected),
            "stable_windows": int(stable_windows),
            "updated_epoch": float(time.time()),
            "last_action": action,
            "pressure_reason": pressure_reason,
            "pressure_score": float(pressure_snapshot.get("pressure_score", 0.0) or 0.0),
        }
        window["missing"] = 0
        window["received"] = 0
        window["last_refresh"] = now_mono
        SerialPort._sync_gui_mode_interval_state_from_stream(self)
        self.GUIUpdateInterval = int(self.gui_update_intervals_by_mode.get(0, self.GUIUpdateInterval))

    def _sync_gui_mode_interval_state_from_stream(self):
        stream_intervals = getattr(self, "gui_update_intervals_by_stream", {})
        self.gui_update_intervals_by_mode = SerialPort._mode_intervals_from_stream_intervals(stream_intervals)
        stream_best = getattr(self, "gui_update_interval_best_by_stream", {})
        self.gui_update_interval_best_by_mode = SerialPort._mode_intervals_from_stream_intervals(stream_best)
        stream_best_loss = getattr(self, "gui_update_interval_loss_best_by_stream", {})
        self.gui_update_interval_loss_best_by_mode = {
            0: stream_best_loss.get("mode0_lfp"),
            1: stream_best_loss.get("mode1_raw"),
            2: stream_best_loss.get("mode2_raw"),
            3: max(
                [
                    value for value in (
                        stream_best_loss.get("mode3_lfp_esa"),
                        stream_best_loss.get("mode3_raw"),
                    )
                    if value is not None
                ] or [None]
            ),
        }
        stream_stats = getattr(self, "gui_update_interval_stream_stats", {})
        mode_stats = {}
        for mode, stream_key in ((0, "mode0_lfp"), (1, "mode1_raw"), (2, "mode2_raw")):
            item = dict(stream_stats.get(stream_key, {}) or {})
            item["interval"] = int(self.gui_update_intervals_by_mode[mode])
            item["best_interval"] = int(self.gui_update_interval_best_by_mode[mode])
            mode_stats[mode] = item
        mode3_lfp = dict(stream_stats.get("mode3_lfp_esa", {}) or {})
        mode3_raw = dict(stream_stats.get("mode3_raw", {}) or {})
        mode_stats[3] = {
            "interval": int(self.gui_update_intervals_by_mode[3]),
            "best_interval": int(self.gui_update_interval_best_by_mode[3]),
            "best_loss_percent": max(
                [
                    value for value in (
                        mode3_lfp.get("best_loss_percent"),
                        mode3_raw.get("best_loss_percent"),
                    )
                    if value is not None
                ] or [None]
            ),
            "last_loss_percent": max(float(mode3_lfp.get("last_loss_percent", 0.0) or 0.0), float(mode3_raw.get("last_loss_percent", 0.0) or 0.0)),
            "loss_ewma_percent": max(float(mode3_lfp.get("loss_ewma_percent", 0.0) or 0.0), float(mode3_raw.get("loss_ewma_percent", 0.0) or 0.0)),
            "last_expected_packets": int(mode3_lfp.get("last_expected_packets", 0) or 0) + int(mode3_raw.get("last_expected_packets", 0) or 0),
            "updated_epoch": max(float(mode3_lfp.get("updated_epoch", 0.0) or 0.0), float(mode3_raw.get("updated_epoch", 0.0) or 0.0)),
        }
        self.gui_update_interval_stats = mode_stats

    def _apply_gui_interval_system_pressure(self, reason, stream_key=None):
        reason = str(reason or "").strip()
        if not reason:
            return False
        try:
            attr_dict = object.__getattribute__(self, "__dict__")
        except Exception:
            attr_dict = {}
        if not (bool(attr_dict.get("detail_enabled", False)) or bool(attr_dict.get("threshold_samples_enabled", False))):
            return False
        SerialPort._ensure_gui_stream_interval_state(self)
        now_mono = time.monotonic()
        cooldown_s = max(0.0, float(getattr(self, "gui_update_interval_change_cooldown_s", 3.0) or 0.0))
        last_pressure = float(getattr(self, "gui_update_interval_system_pressure_last_mono", 0.0) or 0.0)
        if cooldown_s > 0.0 and last_pressure > 0.0 and (now_mono - last_pressure) < cooldown_s:
            return False
        changed = False
        target_streams = []
        if stream_key is not None:
            coerced_stream = SerialPort._coerce_gui_stream_key(stream_key)
            if coerced_stream in {"mode3_lfp_esa", "mode3_raw"}:
                target_streams = ["mode3_lfp_esa", "mode3_raw"]
            else:
                target_streams = [coerced_stream]
        else:
            try:
                active_mode = int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE) or STREAM_MODE_IDLE)
            except Exception:
                active_mode = STREAM_MODE_IDLE
            if active_mode == STREAM_MODE_MODE3:
                target_streams = ["mode3_lfp_esa", "mode3_raw"]
            elif active_mode == STREAM_MODE_MODE2:
                target_streams = ["mode2_raw"]
            elif active_mode == STREAM_MODE_MODE1:
                target_streams = ["mode1_raw"]
            else:
                target_streams = [SerialPort._active_lfp_gui_stream(self)]
        for target_stream in target_streams:
            current_interval = SerialPort._gui_interval_for_stream(self, target_stream)
            next_interval = SerialPort._normalize_gui_stream_interval_value(
                target_stream,
                current_interval + max(1, int(round(current_interval * 0.25))),
            )
            if next_interval != current_interval:
                self.gui_update_intervals_by_stream[target_stream] = int(next_interval)
                self.gui_update_interval_last_change_monotonic_by_stream[target_stream] = now_mono
                item = dict(getattr(self, "gui_update_interval_stream_stats", {}).get(target_stream, {}) or {})
                item.update({
                    "interval": int(next_interval),
                    "updated_epoch": float(time.time()),
                    "last_action": "increase_pressure",
                    "pressure_reason": reason,
                })
                self.gui_update_interval_stream_stats[target_stream] = item
                changed = True
        if changed:
            self.gui_update_interval_system_pressure_last_mono = now_mono
            SerialPort._sync_gui_mode_interval_state_from_stream(self)
        return changed

    def apply_gui_interval_pressure(self, reason, stream_key=None):
        return SerialPort._apply_gui_interval_system_pressure(self, reason, stream_key=stream_key)

    def _gui_interval_system_pressure_reason(self, stream_key=None):
        stream_mode = STREAM_MODE_IDLE
        if stream_key is not None:
            try:
                stream_mode = int(GUI_STREAM_TO_MODE[SerialPort._coerce_gui_stream_key(stream_key)])
            except Exception:
                stream_mode = STREAM_MODE_IDLE
        else:
            try:
                stream_mode = int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE) or STREAM_MODE_IDLE)
            except Exception:
                stream_mode = STREAM_MODE_IDLE
        if bool(getattr(self, "serial_backlog_active", False)):
            return "serial_backlog"
        try:
            bytes_per_packet = float(READ_BATCH_SERIAL_BYTES_PER_PACKET_BY_MODE.get(stream_mode, 0.0) or 0.0)
            warn_packets = float(READ_BATCH_PACKET_WARN_BY_MODE.get(stream_mode, 0) or 0)
            if bytes_per_packet > 0.0 and warn_packets > 0.0:
                batch_packets = float(getattr(self, "last_read_batch_bytes", 0) or 0) / bytes_per_packet
                waiting_packets = float(getattr(self, "port_in_waiting_before_read", 0) or 0) / bytes_per_packet
                if waiting_packets >= warn_packets:
                    return "serial_waiting_bytes"
                if batch_packets >= warn_packets:
                    return "read_batch_packets"
        except Exception:
            pass
        try:
            read_gap_ms = float(getattr(self, "last_serial_read_gap_ms", 0.0) or 0.0)
            read_gap_epoch = float(getattr(self, "serial_read_gap_last_epoch", 0.0) or 0.0)
            waiting_bytes = int(getattr(self, "port_in_waiting_before_read", 0) or 0)
            if read_gap_ms > 80.0 and waiting_bytes > 0 and read_gap_epoch > 0.0 and (time.time() - read_gap_epoch) <= 2.0:
                return "serial_read_gap"
        except Exception:
            pass
        try:
            packet_stats = getattr(self, "read_batch_packets_by_mode", {})
            if isinstance(packet_stats, dict):
                value = float(packet_stats.get(stream_mode, packet_stats.get(SerialPort._mode_json_key(stream_mode), 0.0)) or 0.0)
                warn_packets = float(READ_BATCH_PACKET_WARN_BY_MODE.get(stream_mode, 0) or 0)
                if warn_packets > 0.0 and value >= warn_packets:
                    return "read_batch_packets_avg"
        except Exception:
            pass
        try:
            if float(getattr(self, "last_packet_decode_ms", 0.0) or 0.0) > 40.0:
                return "packet_decode_slow"
        except Exception:
            pass
        try:
            if float(getattr(self, "last_save_enqueue_ms", 0.0) or 0.0) > 20.0:
                return "edf_enqueue_slow"
        except Exception:
            pass
        try:
            if int(self._estimate_writer_lag()) > 100:
                return "edf_writer_lag"
        except Exception:
            pass
        return ""

    def _gui_interval_status_payload(self):
        SerialPort._ensure_gui_stream_interval_state(self)
        intervals = SerialPort._json_gui_interval_mapping(getattr(self, "gui_update_intervals_by_mode", {}))
        best = SerialPort._json_gui_interval_mapping(getattr(self, "gui_update_interval_best_by_mode", {}))
        stats = {}
        raw_stats = getattr(self, "gui_update_interval_stats", {})
        if isinstance(raw_stats, dict):
            for mode in sorted(GUI_INTERVAL_DEFAULTS):
                item = raw_stats.get(mode, {})
                if not isinstance(item, dict):
                    item = {}
                stats[SerialPort._mode_json_key(mode)] = {
                    "interval": int(intervals[SerialPort._mode_json_key(mode)]),
                    "best_interval": int(best[SerialPort._mode_json_key(mode)]),
                    "best_loss_percent": item.get("best_loss_percent"),
                    "last_loss_percent": float(item.get("last_loss_percent", 0.0) or 0.0),
                    "last_expected_packets": int(item.get("last_expected_packets", 0) or 0),
                    "updated_epoch": float(item.get("updated_epoch", 0.0) or 0.0),
                }
        return intervals, best, stats

    def _gui_stream_interval_status_payload(self):
        SerialPort._ensure_gui_stream_interval_state(self)
        intervals = SerialPort._json_gui_stream_interval_mapping(
            getattr(self, "gui_update_intervals_by_stream", {}),
            getattr(self, "gui_update_intervals_by_mode", {}),
        )
        best = SerialPort._json_gui_stream_interval_mapping(
            getattr(self, "gui_update_interval_best_by_stream", {}),
            getattr(self, "gui_update_interval_best_by_mode", {}),
        )
        stats = {}
        raw_stats = getattr(self, "gui_update_interval_stream_stats", {})
        if isinstance(raw_stats, dict):
            for stream_key in sorted(GUI_STREAM_INTERVAL_DEFAULTS):
                item = raw_stats.get(stream_key, {})
                if not isinstance(item, dict):
                    item = {}
                stats[stream_key] = {
                    "interval": int(intervals[stream_key]),
                    "best_interval": int(best[stream_key]),
                    "best_loss_percent": item.get("best_loss_percent"),
                    "last_loss_percent": float(item.get("last_loss_percent", 0.0) or 0.0),
                    "loss_ewma_percent": float(item.get("loss_ewma_percent", 0.0) or 0.0),
                    "last_expected_packets": int(item.get("last_expected_packets", 0) or 0),
                    "stable_windows": int(item.get("stable_windows", 0) or 0),
                    "updated_epoch": float(item.get("updated_epoch", 0.0) or 0.0),
                    "last_action": str(item.get("last_action", "") or ""),
                    "pressure_reason": str(item.get("pressure_reason", "") or ""),
                }
        return intervals, best, stats

    def _record_read_batch_size(self, batch_bytes):
        try:
            mode = int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE))
        except Exception:
            mode = STREAM_MODE_IDLE
        if mode not in GUI_INTERVAL_DEFAULTS:
            return
        windows = getattr(self, "_read_batch_ewma_windows", None)
        if not isinstance(windows, dict):
            now_mono = time.monotonic()
            windows = {
                mode_key: {"ewma": 0.0, "count": 0, "last_refresh": now_mono}
                for mode_key in GUI_INTERVAL_DEFAULTS
            }
            self._read_batch_ewma_windows = windows
        window = windows.setdefault(mode, {"ewma": 0.0, "count": 0, "last_refresh": time.monotonic()})
        alpha = max(0.01, min(1.0, float(getattr(self, "read_batch_ewma_alpha", 0.2) or 0.2)))
        value = max(0.0, float(batch_bytes or 0.0))
        window["ewma"] = alpha * value + (1.0 - alpha) * float(window.get("ewma", 0.0) or 0.0)
        window["count"] = int(window.get("count", 0) or 0) + 1
        SerialPort._maybe_refresh_read_batch_stats(self, mode)

    def _maybe_refresh_read_batch_stats(self, mode):
        mode = SerialPort._coerce_mode_int(mode)
        windows = getattr(self, "_read_batch_ewma_windows", {})
        window = windows.get(mode)
        if not isinstance(window, dict):
            return
        now_mono = time.monotonic()
        refresh_s = max(1.0, float(getattr(self, "read_batch_stats_refresh_s", 10.0) or 10.0))
        last_refresh = float(window.get("last_refresh", now_mono) or now_mono)
        if (now_mono - last_refresh) < refresh_s:
            return
        count = max(0, int(window.get("count", 0) or 0))
        if count > 0:
            alpha = max(0.01, min(1.0, float(getattr(self, "read_batch_ewma_alpha", 0.2) or 0.2)))
            correction = 1.0 - ((1.0 - alpha) ** count)
            corrected = float(window.get("ewma", 0.0) or 0.0)
            if correction > 1e-9:
                corrected = corrected / correction
            read_stats = getattr(self, "read_batch_bytes_by_mode", None)
            if not isinstance(read_stats, dict):
                read_stats = {mode_key: 0.0 for mode_key in GUI_INTERVAL_DEFAULTS}
                self.read_batch_bytes_by_mode = read_stats
            read_stats[mode] = float(corrected)
        window["ewma"] = 0.0
        window["count"] = 0
        window["last_refresh"] = now_mono

    def _read_batch_status_payload(self):
        for mode in sorted(GUI_INTERVAL_DEFAULTS):
            SerialPort._maybe_refresh_read_batch_stats(self, mode)
        raw = getattr(self, "read_batch_bytes_by_mode", {})
        if not isinstance(raw, dict):
            raw = {}
        return {
            SerialPort._mode_json_key(mode): float(raw.get(mode, 0.0) or 0.0)
            for mode in sorted(GUI_INTERVAL_DEFAULTS)
        }

    def _record_read_batch_packet_counts(self, counts_by_mode):
        if not isinstance(counts_by_mode, dict):
            return
        windows = getattr(self, "_read_batch_packet_ewma_windows", None)
        if not isinstance(windows, dict):
            now_mono = time.monotonic()
            windows = {
                mode_key: {"ewma": 0.0, "count": 0, "last_refresh": now_mono}
                for mode_key in GUI_INTERVAL_DEFAULTS
            }
            self._read_batch_packet_ewma_windows = windows
        alpha = max(0.01, min(1.0, float(getattr(self, "read_batch_ewma_alpha", 0.2) or 0.2)))
        for raw_mode, raw_count in counts_by_mode.items():
            mode = SerialPort._coerce_mode_int(raw_mode, fallback=-1)
            if mode not in GUI_INTERVAL_DEFAULTS:
                continue
            value = max(0.0, float(raw_count or 0.0))
            if value <= 0.0:
                continue
            window = windows.setdefault(mode, {"ewma": 0.0, "count": 0, "last_refresh": time.monotonic()})
            window["ewma"] = alpha * value + (1.0 - alpha) * float(window.get("ewma", 0.0) or 0.0)
            window["count"] = int(window.get("count", 0) or 0) + 1
            SerialPort._maybe_refresh_read_batch_packet_stats(self, mode)

    def _maybe_refresh_read_batch_packet_stats(self, mode):
        mode = SerialPort._coerce_mode_int(mode)
        windows = getattr(self, "_read_batch_packet_ewma_windows", {})
        window = windows.get(mode) if isinstance(windows, dict) else None
        if not isinstance(window, dict):
            return
        now_mono = time.monotonic()
        refresh_s = max(1.0, float(getattr(self, "read_batch_stats_refresh_s", 10.0) or 10.0))
        last_refresh = float(window.get("last_refresh", now_mono) or now_mono)
        if (now_mono - last_refresh) < refresh_s:
            return
        count = max(0, int(window.get("count", 0) or 0))
        if count > 0:
            alpha = max(0.01, min(1.0, float(getattr(self, "read_batch_ewma_alpha", 0.2) or 0.2)))
            correction = 1.0 - ((1.0 - alpha) ** count)
            corrected = float(window.get("ewma", 0.0) or 0.0)
            if correction > 1e-9:
                corrected = corrected / correction
            read_stats = getattr(self, "read_batch_packets_by_mode", None)
            if not isinstance(read_stats, dict):
                read_stats = {mode_key: 0.0 for mode_key in GUI_INTERVAL_DEFAULTS}
                self.read_batch_packets_by_mode = read_stats
            read_stats[mode] = float(corrected)
        window["ewma"] = 0.0
        window["count"] = 0
        window["last_refresh"] = now_mono

    def _read_batch_packet_status_payload(self):
        for mode in sorted(GUI_INTERVAL_DEFAULTS):
            SerialPort._maybe_refresh_read_batch_packet_stats(self, mode)
        raw = getattr(self, "read_batch_packets_by_mode", {})
        if not isinstance(raw, dict):
            raw = {}
        return {
            SerialPort._mode_json_key(mode): float(raw.get(mode, 0.0) or 0.0)
            for mode in sorted(GUI_INTERVAL_DEFAULTS)
        }

    @staticmethod
    def _read_batch_packet_redline_status_payload():
        return {
            SerialPort._mode_json_key(mode): int(READ_BATCH_PACKET_REDLINE_BY_MODE.get(mode, 0) or 0)
            for mode in sorted(GUI_INTERVAL_DEFAULTS)
        }

    @staticmethod
    def _read_batch_packet_warn_status_payload():
        return {
            SerialPort._mode_json_key(mode): int(READ_BATCH_PACKET_WARN_BY_MODE.get(mode, 0) or 0)
            for mode in sorted(GUI_INTERVAL_DEFAULTS)
        }

    def set_detail_backpressure(self, active, reason=""):
        self.detail_backpressure_active = bool(active)
        self.detail_backpressure_reason = str(reason or "")
        if self.detail_backpressure_active:
            self.detail_payload_throttled = True
            self.detail_payload_last_skip_reason = self.detail_backpressure_reason or "backpressure"

    def _record_detail_payload_skip(self, reason):
        if not bool(getattr(self, "detail_enabled", False)):
            return
        self.detail_payload_dropped_frames = int(getattr(self, "detail_payload_dropped_frames", 0) or 0) + 1
        self.detail_payload_throttled = True
        self.detail_payload_last_skip_reason = str(reason or "throttled")

    def _should_emit_detail_payload(self, now_mono, stream_key="default"):
        if not bool(getattr(self, "detail_enabled", False)):
            return False
        return True

    def _mark_detail_payload_emitted(self, now_mono, stream_key="default"):
        last_by_stream = getattr(self, "_last_detail_emit_monotonic_by_stream", None)
        if not isinstance(last_by_stream, dict):
            last_by_stream = {}
            self._last_detail_emit_monotonic_by_stream = last_by_stream
        last_by_stream[str(stream_key or "default")] = float(now_mono)
        self._last_detail_emit_monotonic = float(now_mono)
        if not bool(getattr(self, "detail_backpressure_active", False)):
            self.detail_payload_throttled = False
            self.detail_payload_last_skip_reason = ""

    def _estimate_writer_lag(self):
        q = getattr(self, "save_queue", None)
        if q is None:
            return 0
        try:
            return int(q.qsize())
        except Exception:
            return 0

    @staticmethod
    def _packet_loss_percent(missing_packets, received_packets):
        missing_packets = max(0, int(missing_packets or 0))
        received_packets = max(0, int(received_packets or 0))
        expected_packets = missing_packets + received_packets
        if expected_packets <= 0:
            return 0.0, 0
        return float(100.0 * missing_packets / expected_packets), int(expected_packets)

    def _record_packet_loss_sample(self, bucket, missing_packets, received_packets):
        bucket_key = str(bucket or "").strip().lower()
        if not bucket_key:
            bucket_key = "lfp"
        missing_packets = max(0, int(missing_packets or 0))
        received_packets = max(0, int(received_packets or 0))
        counters_by_bucket = getattr(self, "_packet_loss_counters", {})
        if not isinstance(counters_by_bucket, dict):
            counters_by_bucket = {}
            self._packet_loss_counters = counters_by_bucket
        counters = dict(counters_by_bucket.get(bucket_key, {}))
        counters["missing"] = max(0, int(counters.get("missing", 0) or 0)) + missing_packets
        counters["received"] = max(0, int(counters.get("received", 0) or 0)) + received_packets

        reset_threshold = 0
        if bucket_key in {"lfp", "mode0_lfp"}:
            try:
                reset_threshold = max(0, int(getattr(self, "file_size_lfp", 0) or 0))
            except RuntimeError:
                reset_threshold = 0
        elif bucket_key in {"spike", "mode1_raw"}:
            try:
                reset_threshold = max(0, int(getattr(self, "file_size_mode1", 0) or 0))
            except RuntimeError:
                reset_threshold = 0
        elif bucket_key in {"mode2", "mode2_raw"}:
            try:
                reset_threshold = max(0, int(getattr(self, "file_size_mode2", 0) or 0))
            except RuntimeError:
                reset_threshold = 0

        if reset_threshold > 0 and counters["received"] >= reset_threshold:
            counters["missing"] = 0
            counters["received"] = 0

        counters_by_bucket[bucket_key] = counters
        current_missing = int(counters["missing"])
        current_received = int(counters["received"])
        current_percent, current_expected = SerialPort._packet_loss_percent(current_missing, current_received)

        self.last_packet_loss_batch = missing_packets
        self.last_packet_count_batch = received_packets
        self.last_packet_loss = current_missing
        self.last_packet_count = current_received
        self.last_packet_loss_percent_current = current_percent
        self.last_packet_expected_current = current_expected
        try:
            previous_sequence = int(getattr(self, "packet_metrics_sequence", 0) or 0)
        except RuntimeError:
            previous_sequence = 0
        self.packet_metrics_sequence = previous_sequence + 1
        try:
            last_batches = getattr(self, "_packet_loss_last_batch", None)
        except RuntimeError:
            last_batches = None
        if not isinstance(last_batches, dict):
            last_batches = {}
            self._packet_loss_last_batch = last_batches
        last_batches[bucket_key] = {
            "missing": missing_packets,
            "received": received_packets,
        }
        SerialPort._record_gui_interval_loss_sample(self, bucket_key, missing_packets, received_packets)
        return {
            "missing": current_missing,
            "received": current_received,
            "percent": current_percent,
            "expected": current_expected,
            "batch_missing": missing_packets,
            "batch_received": received_packets,
            "sequence": int(self.packet_metrics_sequence),
        }

    def _packet_loss_status_snapshot(self, bucket):
        bucket_key = str(bucket or "").strip().lower()
        try:
            counters_by_bucket = getattr(self, "_packet_loss_counters", {})
        except RuntimeError:
            counters_by_bucket = {}
        if not isinstance(counters_by_bucket, dict):
            counters_by_bucket = {}
            self._packet_loss_counters = counters_by_bucket
        if not bucket_key:
            bucket_key = "lfp"
        counters = dict(counters_by_bucket.get(bucket_key, {}) or {})
        try:
            last_batches = getattr(self, "_packet_loss_last_batch", {})
        except RuntimeError:
            last_batches = {}
        last_batch = dict(last_batches.get(bucket_key, {}) or {})
        current_missing = max(0, int(counters.get("missing", 0) or 0))
        current_received = max(0, int(counters.get("received", 0) or 0))
        current_percent, current_expected = SerialPort._packet_loss_percent(current_missing, current_received)
        return {
            "missing": current_missing,
            "received": current_received,
            "percent": current_percent,
            "expected": current_expected,
            "batch_missing": max(0, int(last_batch.get("missing", 0) or 0)),
            "batch_received": max(0, int(last_batch.get("received", 0) or 0)),
            "sequence": int(getattr(self, "packet_metrics_sequence", 0) or 0),
        }

    def _packet_loss_streams_for_save_mode(self, mode_name):
        text = str(mode_name or "").strip().lower()
        if text in {"0", "lfp", "mode0", "mode0_lfp"}:
            return ("mode0_lfp",)
        if text in {"1", "mode1", "mode1_raw", "spike"}:
            return ("mode1_raw",)
        if text in {"2", "mode2", "mode2_raw"}:
            return ("mode2_raw",)
        if text in {"3", "mode3", "mode3_lfp_esa", "mode3_raw"}:
            return ("mode3_lfp_esa", "mode3_raw")
        return tuple(PACKET_LOSS_STREAM_BUCKETS)

    def _reset_packet_loss_streams(self, streams=None, warmup_packets=PACKET_LOSS_DEFAULT_WARMUP_PACKETS, clear_display=True):
        if streams is None:
            stream_keys = list(PACKET_LOSS_STREAM_BUCKETS)
        elif isinstance(streams, str):
            stream_keys = list(SerialPort._packet_loss_streams_for_save_mode(self, streams))
        else:
            stream_keys = []
            for item in list(streams or []):
                text = str(item or "").strip().lower()
                if text in GUI_STREAM_TO_MODE:
                    stream_keys.append(text)
                else:
                    stream_keys.extend(SerialPort._packet_loss_streams_for_save_mode(self, text))
        if not stream_keys:
            stream_keys = list(PACKET_LOSS_STREAM_BUCKETS)
        seen = set()
        stream_keys = [key for key in stream_keys if not (key in seen or seen.add(key))]
        try:
            counters_by_bucket = getattr(self, "_packet_loss_counters", {})
        except RuntimeError:
            counters_by_bucket = {}
        if not isinstance(counters_by_bucket, dict):
            counters_by_bucket = {}
            self._packet_loss_counters = counters_by_bucket
        try:
            last_batches = getattr(self, "_packet_loss_last_batch", {})
        except RuntimeError:
            last_batches = {}
        if not isinstance(last_batches, dict):
            last_batches = {}
            self._packet_loss_last_batch = last_batches
        try:
            trackers = getattr(self, "_packet_index_trackers", {})
        except RuntimeError:
            trackers = {}
        if not isinstance(trackers, dict):
            trackers = {}
            self._packet_index_trackers = trackers
        try:
            warmups = getattr(self, "_packet_loss_warmup_by_bucket", {})
        except RuntimeError:
            warmups = {}
        if not isinstance(warmups, dict):
            warmups = {}
            self._packet_loss_warmup_by_bucket = warmups
        warmup_count = max(0, int(warmup_packets or 0))
        for key in stream_keys:
            counters_by_bucket[key] = {"missing": 0, "received": 0}
            last_batches[key] = {"missing": 0, "received": 0}
            tracker = dict(trackers.get(key, {}) or {})
            tracker["last"] = None
            tracker["expected_step"] = _normalize_packet_expected_step(tracker.get("expected_step", 1))
            trackers[key] = tracker
            warmups[key] = warmup_count
        if clear_display:
            self.last_packet_loss = 0
            self.last_packet_count = 0
            self.last_packet_loss_batch = 0
            self.last_packet_count_batch = 0
            self.last_packet_loss_percent_current = 0.0
            self.last_packet_expected_current = 0
            try:
                self.packet_metrics_sequence = int(getattr(self, "packet_metrics_sequence", 0) or 0) + 1
            except RuntimeError:
                self.packet_metrics_sequence = 1

    def _reset_packet_index_trackers(self):
        trackers = getattr(self, "_packet_index_trackers", None)
        if not isinstance(trackers, dict):
            return
        for tracker in trackers.values():
            if isinstance(tracker, dict):
                tracker["last"] = None

    def _should_accept_packet_index_sample(self, bucket, packet_index, expected_step=1):
        snapshot = SerialPort._record_packet_index_sample(
            self,
            bucket,
            packet_index,
            expected_step=expected_step,
        )
        try:
            return int(snapshot.get("batch_received", 1) or 0) > 0
        except Exception:
            return True

    def _record_packet_index_sample(self, bucket, packet_index, expected_step=1):
        bucket_key = str(bucket or "").strip().lower()
        try:
            counters_by_bucket = getattr(self, "_packet_loss_counters", {})
        except RuntimeError:
            counters_by_bucket = {}
        if not isinstance(counters_by_bucket, dict):
            counters_by_bucket = {}
        if not counters_by_bucket:
            counters_by_bucket = {
                bucket: {"missing": 0, "received": 0}
                for bucket in PACKET_LOSS_ALL_BUCKETS
            }
            self._packet_loss_counters = counters_by_bucket
        if not bucket_key:
            bucket_key = "lfp"
        expected_step = _normalize_packet_expected_step(expected_step)
        try:
            trackers = getattr(self, "_packet_index_trackers", None)
        except RuntimeError:
            trackers = None
        if not isinstance(trackers, dict):
            trackers = {}
            self._packet_index_trackers = trackers
        tracker = trackers.setdefault(bucket_key, {"last": None, "expected_step": expected_step})
        previous_step = _normalize_packet_expected_step(tracker.get("expected_step", expected_step))
        previous = tracker.get("last")
        if previous_step != expected_step:
            previous = None
        try:
            current = int(packet_index) & 0xFFFF
        except Exception:
            return SerialPort._packet_loss_status_snapshot(self, bucket_key)

        try:
            warmups = getattr(self, "_packet_loss_warmup_by_bucket", {})
        except RuntimeError:
            warmups = {}
        if isinstance(warmups, dict):
            remaining_warmup = max(0, int(warmups.get(bucket_key, 0) or 0))
        else:
            remaining_warmup = 0
        if remaining_warmup > 0:
            tracker["last"] = current
            tracker["expected_step"] = expected_step
            warmups[bucket_key] = remaining_warmup - 1
            snapshot = SerialPort._packet_loss_status_snapshot(self, bucket_key)
            snapshot["batch_missing"] = 0
            snapshot["batch_received"] = 1
            return snapshot

        missing_packets = 0
        should_advance_tracker = True
        received_packets = 1
        if previous is not None:
            transition = _classify_packet_index_transition(
                int(previous) & 0xFFFF,
                current,
                expected_step=expected_step,
                max_missing_packets=4096,
            )
            kind = str(transition.get("kind", "") or "")
            if kind == "out_of_order":
                should_advance_tracker = False
                received_packets = 0
                self.packet_out_of_order_event_count = int(getattr(self, "packet_out_of_order_event_count", 0) or 0) + 1
                self.last_packet_out_of_order_summary = (
                    f"{bucket_key}: stale/backward packet prev={int(previous) & 0xFFFF} curr={current}"
                )
            elif kind == "duplicate":
                should_advance_tracker = False
                received_packets = 0
            elif kind == "reset":
                self.packet_counter_reset_event_count = int(getattr(self, "packet_counter_reset_event_count", 0) or 0) + 1
                self.last_packet_counter_reset_summary = (
                    f"{bucket_key}: counter reset prev={int(previous) & 0xFFFF} curr={current}"
                )
            else:
                missing_packets = max(0, int(transition.get("missing", 0) or 0))
                missing_packets = min(missing_packets, 4096)

        if should_advance_tracker:
            tracker["last"] = current
        tracker["expected_step"] = expected_step
        snapshot = SerialPort._record_packet_loss_sample(self, bucket_key, missing_packets, received_packets)
        if missing_packets > 0:
            self._record_packet_gap_event(
                bucket_key,
                missing_packets,
                [int(previous) & 0xFFFF, current] if previous is not None else [current],
                expected_step=expected_step,
            )
        return snapshot

    def _record_serial_backlog_event(self, backlog_bytes):
        backlog_bytes = max(0, int(backlog_bytes or 0))
        if backlog_bytes < int(getattr(self, "serial_backlog_warning_threshold_bytes", 32768) or 32768):
            return False
        self.serial_backlog_event_count = int(getattr(self, "serial_backlog_event_count", 0) or 0) + 1
        self.serial_backlog_last_bytes = backlog_bytes
        self.serial_backlog_peak_bytes = max(
            int(getattr(self, "serial_backlog_peak_bytes", 0) or 0),
            backlog_bytes,
        )
        self.serial_backlog_last_epoch = float(time.time())
        self.serial_backlog_active = True
        now_mono = time.monotonic()
        last_force_mono = float(getattr(self, "_last_serial_backlog_force_emit_monotonic", 0.0) or 0.0)
        if (
            last_force_mono <= 0.0 or
            (now_mono - last_force_mono)
        ) >= 0.5:
            self._last_serial_backlog_force_emit_monotonic = now_mono
            SerialPort._emit_status_update(self, force=True)
        return True

    def _record_serial_read_gap_event(self, gap_ms, waiting_bytes=0, batch_bytes=0):
        gap_ms = max(0.0, float(gap_ms or 0.0))
        self.last_serial_read_gap_ms = gap_ms
        self.serial_read_gap_peak_ms = max(
            float(getattr(self, "serial_read_gap_peak_ms", 0.0) or 0.0),
            gap_ms,
        )
        self.port_in_waiting_before_read = max(0, int(waiting_bytes or 0))
        self.last_read_batch_bytes = max(0, int(batch_bytes or 0))
        SerialPort._append_serial_read_gap_trace(
            self,
            gap_ms=gap_ms,
            waiting_bytes=waiting_bytes,
            batch_bytes=batch_bytes,
        )
        threshold_ms = float(getattr(self, "serial_read_gap_warning_threshold_ms", 80.0) or 80.0)
        if threshold_ms <= 0.0 or gap_ms < threshold_ms:
            return False
        self.serial_read_gap_event_count = int(getattr(self, "serial_read_gap_event_count", 0) or 0) + 1
        self.serial_read_gap_last_epoch = float(time.time())
        return True

    def _append_serial_read_gap_trace(self, gap_ms, waiting_bytes=0, batch_bytes=0):
        try:
            threshold_ms = float(getattr(self, "serial_read_gap_trace_threshold_ms", 5.0) or 5.0)
        except Exception:
            threshold_ms = 5.0
        if threshold_ms > 0.0 and float(gap_ms or 0.0) < threshold_ms:
            return False
        try:
            trace = self.serial_read_gap_trace
        except Exception:
            trace = None
        if trace is None or not hasattr(trace, "append"):
            trace = coll.deque(maxlen=80)
            self.serial_read_gap_trace = trace
        try:
            seq = int(getattr(self, "serial_read_gap_trace_seq", 0) or 0) + 1
        except Exception:
            seq = 1
        self.serial_read_gap_trace_seq = seq
        try:
            stream_intervals = getattr(self, "gui_update_intervals_by_stream", {})
        except Exception:
            stream_intervals = {}
        if not isinstance(stream_intervals, dict):
            stream_intervals = {}
        def _float_attr(name):
            try:
                return float(getattr(self, name, 0.0) or 0.0)
            except Exception:
                return 0.0
        entry = {
            "seq": int(seq),
            "timestamp_epoch": float(time.time()),
            "gap_ms": float(gap_ms or 0.0),
            "waiting_bytes": max(0, int(waiting_bytes or 0)),
            "batch_bytes": max(0, int(batch_bytes or 0)),
            "stream_mode": int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE) or STREAM_MODE_IDLE),
            "detail_enabled": bool(getattr(self, "detail_enabled", False)),
            "threshold_samples_enabled": bool(getattr(self, "threshold_samples_enabled", False)),
            "mode3_save_enabled": bool(getattr(self, "save_file_mode3_flag", False)),
            "save_enqueue_ms": float(getattr(self, "last_save_enqueue_ms", 0.0) or 0.0),
            "save_enqueue_type": str(getattr(self, "last_save_enqueue_type", "") or ""),
            "save_queue_drop_count": int(getattr(self, "save_queue_drop_count", 0) or 0),
            "packet_decode_ms": _float_attr("last_packet_decode_ms"),
            "data_process_ms": _float_attr("last_data_process_ms"),
            "save_control_ms": _float_attr("last_save_control_ms"),
            "gui_update_ms": _float_attr("last_gui_update_ms"),
            "pipeline_process_ms": _float_attr("last_pipeline_process_ms"),
            "mode3_lfp_esa_interval": int(stream_intervals.get("mode3_lfp_esa", 0) or 0),
            "mode3_raw_interval": int(stream_intervals.get("mode3_raw", 0) or 0),
        }
        trace.append(entry)
        return True

    def _serial_read_gap_trace_payload(self, limit=20):
        try:
            trace = list(getattr(self, "serial_read_gap_trace", []) or [])
        except Exception:
            trace = []
        try:
            limit = max(0, int(limit or 0))
        except Exception:
            limit = 20
        if limit > 0:
            trace = trace[-limit:]
        payload = []
        for item in trace:
            if isinstance(item, dict):
                payload.append(dict(item))
        return payload

    def _record_packet_gap_event(self, bucket, missing_packets, packet_indices, expected_step=1):
        missing_packets = max(0, int(missing_packets or 0))
        if missing_packets <= 0:
            return False
        packet_indices = list(packet_indices or [])
        expected_step = _normalize_packet_expected_step(expected_step)
        gap_parts = []
        for j in range(max(0, len(packet_indices) - 1)):
            if len(gap_parts) >= 4:
                break
            try:
                prev_idx = int(packet_indices[j]) & 0xFFFF
                curr_idx = int(packet_indices[j + 1]) & 0xFFFF
            except Exception:
                continue
            delta, wrapped = _packet_index_delta_u16(prev_idx, curr_idx)
            increments = delta // expected_step if expected_step > 0 else 0
            missing = max(0, increments - 1)
            if missing <= 0:
                continue
            suffix = " wrap" if wrapped else ""
            gap_parts.append(f"{prev_idx}->{curr_idx} miss={missing} step={expected_step}{suffix}")
        summary = "; ".join(gap_parts) if gap_parts else f"{missing_packets} missing packet(s)"
        self.packet_gap_event_count = int(getattr(self, "packet_gap_event_count", 0) or 0) + 1
        self.last_packet_gap_missing = missing_packets
        self.last_packet_gap_summary = f"{str(bucket or 'stream')}: {summary}"
        self.last_packet_gap_diagnostics = SerialPort._packet_gap_diagnostic_payload(
            self,
            bucket=bucket,
            missing_packets=missing_packets,
            packet_indices=packet_indices,
            expected_step=expected_step,
            summary=summary,
        )
        return True

    def _packet_gap_diagnostic_payload(self, bucket, missing_packets, packet_indices, expected_step=1, summary=""):
        now_epoch = float(time.time())
        try:
            pipeline = getattr(self, "pipeline", None)
            pipeline_status = pipeline.status() if pipeline is not None and hasattr(pipeline, "status") else None
        except Exception:
            pipeline_status = None
        try:
            writer_lag = int(self._estimate_writer_lag())
        except Exception:
            writer_lag = 0
        try:
            last_enqueue_epoch = float(getattr(self, "last_save_enqueue_epoch", 0.0) or 0.0)
        except Exception:
            last_enqueue_epoch = 0.0
        time_since_enqueue_ms = 0.0
        if last_enqueue_epoch > 0.0:
            time_since_enqueue_ms = max(0.0, (now_epoch - last_enqueue_epoch) * 1000.0)
        stream_intervals = getattr(self, "gui_update_intervals_by_stream", {})
        if not isinstance(stream_intervals, dict):
            stream_intervals = {}
        def _float_attr(name):
            try:
                return float(getattr(self, name, 0.0) or 0.0)
            except Exception:
                return 0.0
        return {
            "timestamp_epoch": now_epoch,
            "bucket": str(bucket or "stream"),
            "missing_packets": int(max(0, int(missing_packets or 0))),
            "expected_step": int(_normalize_packet_expected_step(expected_step)),
            "packet_indices": [int(value) & 0xFFFF for value in list(packet_indices or [])[-4:]],
            "summary": str(summary or ""),
            "stream_mode": int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE) or STREAM_MODE_IDLE),
            "detail_enabled": bool(getattr(self, "detail_enabled", False)),
            "threshold_samples_enabled": bool(getattr(self, "threshold_samples_enabled", False)),
            "mode3_save_enabled": bool(getattr(self, "save_file_mode3_flag", False)),
            "mode3_counter": int(getattr(self, "Mode3RawCounter", 0) or 0),
            "mode3_pending_packets": (
                int(getattr(self, "_mode3_pending_packets", 0) or 0)
                + int(getattr(self, "_mode3_encoded_pending_packets", 0) or 0)
            ),
            "mode3_raw_pending_packets": (
                int(getattr(self, "_mode3_raw_pending_packets", 0) or 0)
                + int(getattr(self, "_mode3_raw_encoded_pending_packets", 0) or 0)
            ),
            "save_chunk_packets_mode3": int(getattr(self, "_save_chunk_packets_mode3", 0) or 0),
            "save_chunk_packets_mode3_raw": int(getattr(self, "_save_chunk_packets_mode3_raw", 0) or 0),
            "save_enqueue_ms": float(getattr(self, "last_save_enqueue_ms", 0.0) or 0.0),
            "save_enqueue_type": str(getattr(self, "last_save_enqueue_type", "") or ""),
            "save_queue_drop_count": int(getattr(self, "save_queue_drop_count", 0) or 0),
            "time_since_save_enqueue_ms": float(time_since_enqueue_ms),
            "writer_lag": int(writer_lag),
            "serial_read_gap_ms": float(getattr(self, "last_serial_read_gap_ms", 0.0) or 0.0),
            "serial_read_gap_event_count": int(getattr(self, "serial_read_gap_event_count", 0) or 0),
            "serial_read_gap_last_epoch": float(getattr(self, "serial_read_gap_last_epoch", 0.0) or 0.0),
            "serial_read_gap_trace_seq": int(getattr(self, "serial_read_gap_trace_seq", 0) or 0),
            "serial_read_gap_trace": SerialPort._serial_read_gap_trace_payload(self, limit=8),
            "serial_backlog_active": bool(getattr(self, "serial_backlog_active", False)),
            "serial_backlog_last_bytes": int(getattr(self, "serial_backlog_last_bytes", 0) or 0),
            "read_batch_bytes": int(getattr(self, "last_read_batch_bytes", 0) or 0),
            "port_in_waiting_before_read": int(getattr(self, "port_in_waiting_before_read", 0) or 0),
            "packet_decode_ms": _float_attr("last_packet_decode_ms"),
            "data_process_ms": _float_attr("last_data_process_ms"),
            "save_control_ms": _float_attr("last_save_control_ms"),
            "gui_update_ms": _float_attr("last_gui_update_ms"),
            "pipeline_process_ms": _float_attr("last_pipeline_process_ms"),
            "mode3_lfp_esa_interval": int(stream_intervals.get("mode3_lfp_esa", 0) or 0),
            "mode3_raw_interval": int(stream_intervals.get("mode3_raw", 0) or 0),
            "pipeline_raw_queue_depth": int(getattr(pipeline_status, "raw_queue_depth", 0) or 0),
            "pipeline_event_queue_depth": int(getattr(pipeline_status, "event_queue_depth", 0) or 0),
            "pipeline_dropped_raw_frames": int(getattr(pipeline_status, "dropped_raw_frames", 0) or 0),
        }

    def _infer_stream_mode_from_recent_packets(self):
        tokens = [int(token) for token in list(getattr(self, "_recent_stream_mode_tokens", []))]
        if len(tokens) >= 2 and tokens[-1] == STREAM_MODE_IDLE and tokens[-2] == STREAM_MODE_IDLE:
            return STREAM_MODE_IDLE

        trailing_mode0 = 0
        for token in reversed(tokens):
            if token == STREAM_MODE_MODE0:
                trailing_mode0 += 1
                continue
            break
        if trailing_mode0 >= 2:
            return STREAM_MODE_MODE0

        for token in reversed(tokens):
            if token in (STREAM_MODE_MODE3, STREAM_MODE_MODE2, STREAM_MODE_MODE1):
                return token

        try:
            return int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE))
        except Exception:
            return STREAM_MODE_IDLE

    def _record_stream_mode_packet(self, mode_token):
        try:
            normalized = int(mode_token)
        except Exception:
            try:
                return int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE))
            except Exception:
                return STREAM_MODE_IDLE
        if normalized not in (
            STREAM_MODE_IDLE,
            STREAM_MODE_MODE0,
            STREAM_MODE_MODE1,
            STREAM_MODE_MODE2,
            STREAM_MODE_MODE3,
        ):
            try:
                return int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE))
            except Exception:
                return STREAM_MODE_IDLE
        self._recent_stream_mode_tokens.append(normalized)
        self.current_stream_mode = SerialPort._infer_stream_mode_from_recent_packets(self)
        return int(self.current_stream_mode)

    def _emit_status_update(self, mode_hint=None, packet_loss=None, packet_count=None, force=False, packet_loss_batch=None, packet_count_batch=None, packet_loss_percent_current=None, packet_loss_expected_current=None, packet_metrics_seq=None):
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_status_emit_monotonic) < float(self.status_emit_interval_s):
            return
        if mode_hint is not None:
            try:
                self.current_stream_mode = int(mode_hint)
            except Exception:
                pass
        if packet_loss is not None:
            self.last_packet_loss = int(packet_loss)
        if packet_count is not None:
            self.last_packet_count = int(packet_count)
        if packet_loss_batch is not None:
            self.last_packet_loss_batch = int(packet_loss_batch)
        if packet_count_batch is not None:
            self.last_packet_count_batch = int(packet_count_batch)
        if packet_loss_percent_current is not None:
            self.last_packet_loss_percent_current = float(packet_loss_percent_current)
        if packet_loss_expected_current is not None:
            self.last_packet_expected_current = int(packet_loss_expected_current)
        if packet_metrics_seq is not None:
            self.packet_metrics_sequence = int(packet_metrics_seq)
        backlog_last_epoch = float(getattr(self, "serial_backlog_last_epoch", 0.0) or 0.0)
        if backlog_last_epoch > 0.0:
            recovery_timeout_s = float(getattr(self, "serial_backlog_recovery_timeout_s", 2.0) or 2.0)
            if (time.time() - backlog_last_epoch) >= recovery_timeout_s:
                self.serial_backlog_active = False
        pressure_reason = SerialPort._gui_interval_system_pressure_reason(self)
        if pressure_reason:
            SerialPort._apply_gui_interval_system_pressure(self, pressure_reason)
        pipeline_payload = {}
        pipeline = getattr(self, "pipeline", None)
        if pipeline is not None and hasattr(pipeline, "status"):
            try:
                pipeline_status = pipeline.status()
                pipeline_payload = {
                    "running": bool(getattr(pipeline_status, "running", False)),
                    "worker_alive": bool(getattr(pipeline_status, "worker_alive", False)),
                    "raw_queue_depth": int(getattr(pipeline_status, "raw_queue_depth", 0) or 0),
                    "event_queue_depth": int(getattr(pipeline_status, "event_queue_depth", 0) or 0),
                    "submitted_frames": int(getattr(pipeline_status, "submitted_frames", 0) or 0),
                    "processed_frames": int(getattr(pipeline_status, "processed_frames", 0) or 0),
                    "emitted_events": int(getattr(pipeline_status, "emitted_events", 0) or 0),
                    "dropped_raw_frames": int(getattr(pipeline_status, "dropped_raw_frames", 0) or 0),
                    "dropped_events": int(getattr(pipeline_status, "dropped_events", 0) or 0),
                    "coalesced_events": int(getattr(pipeline_status, "coalesced_events", 0) or 0),
                    "last_error": str(getattr(pipeline_status, "last_error", "") or ""),
                }
            except Exception:
                pipeline_payload = {}
        rsoc, stat, voltage = self.last_battery_triplet
        gui_intervals, gui_best_intervals, gui_interval_stats = SerialPort._gui_interval_status_payload(self)
        gui_stream_intervals, gui_stream_best_intervals, gui_stream_interval_stats = SerialPort._gui_stream_interval_status_payload(self)
        read_batch_by_mode = SerialPort._read_batch_status_payload(self)
        read_batch_packets_by_mode = SerialPort._read_batch_packet_status_payload(self)
        read_batch_packet_redline_by_mode = SerialPort._read_batch_packet_redline_status_payload()
        read_batch_packet_warn_by_mode = SerialPort._read_batch_packet_warn_status_payload()
        mode2_status_channels = list(
            getattr(self, "mode2_selected_channels_gui", list(range(MODE2_V2_CHANNELS)))
            or list(range(MODE2_V2_CHANNELS))
        )
        if len(mode2_status_channels) != MODE2_V2_CHANNELS:
            mode2_status_channels = list(range(MODE2_V2_CHANNELS))
        self.StatusUpdate.emit({
            "stream_mode": int(self.current_stream_mode),
            "packet_loss": int(self.last_packet_loss),
            "packet_count": int(self.last_packet_count),
            "packet_loss_batch": int(getattr(self, "last_packet_loss_batch", 0) or 0),
            "packet_count_batch": int(getattr(self, "last_packet_count_batch", 0) or 0),
            "packet_loss_percent_current": float(getattr(self, "last_packet_loss_percent_current", 0.0) or 0.0),
            "packet_loss_expected_current": int(getattr(self, "last_packet_expected_current", 0) or 0),
            "packet_metrics_seq": int(getattr(self, "packet_metrics_sequence", 0) or 0),
            "serial_backlog_event_count": int(getattr(self, "serial_backlog_event_count", 0) or 0),
            "serial_backlog_last_bytes": int(getattr(self, "serial_backlog_last_bytes", 0) or 0),
            "serial_backlog_peak_bytes": int(getattr(self, "serial_backlog_peak_bytes", 0) or 0),
            "serial_backlog_active": bool(getattr(self, "serial_backlog_active", False)),
            "serial_backlog_last_epoch": float(getattr(self, "serial_backlog_last_epoch", 0.0) or 0.0),
            "serial_read_gap_ms": float(getattr(self, "last_serial_read_gap_ms", 0.0) or 0.0),
            "serial_read_gap_peak_ms": float(getattr(self, "serial_read_gap_peak_ms", 0.0) or 0.0),
            "serial_read_gap_event_count": int(getattr(self, "serial_read_gap_event_count", 0) or 0),
            "serial_read_gap_last_epoch": float(getattr(self, "serial_read_gap_last_epoch", 0.0) or 0.0),
            "serial_read_gap_trace_seq": int(getattr(self, "serial_read_gap_trace_seq", 0) or 0),
            "serial_read_gap_trace": SerialPort._serial_read_gap_trace_payload(self, limit=20),
            "serial_frame_false_marker_count": int(getattr(self, "serial_frame_false_marker_count", 0) or 0),
            "serial_frame_incomplete_wait_count": int(getattr(self, "serial_frame_incomplete_wait_count", 0) or 0),
            "serial_mode2_direct_frame_count": int(getattr(self, "serial_mode2_direct_frame_count", 0) or 0),
            "serial_mode2_direct_packet_count": int(getattr(self, "serial_mode2_direct_packet_count", 0) or 0),
            "serial_mode2_resync_count": int(getattr(self, "serial_mode2_resync_count", 0) or 0),
            "read_batch_bytes": int(getattr(self, "last_read_batch_bytes", 0) or 0),
            "read_batch_bytes_by_mode": read_batch_by_mode,
            "read_batch_packets_by_mode": read_batch_packets_by_mode,
            "read_batch_packet_redline_by_mode": read_batch_packet_redline_by_mode,
            "read_batch_packet_warn_by_mode": read_batch_packet_warn_by_mode,
            "port_in_waiting_before_read": int(getattr(self, "port_in_waiting_before_read", 0) or 0),
            "writer_lag": int(self._estimate_writer_lag()),
            "save_queue_drop_count": int(getattr(self, "save_queue_drop_count", 0) or 0),
            "save_enqueue_ms": float(getattr(self, "last_save_enqueue_ms", 0.0) or 0.0),
            "save_enqueue_type": str(getattr(self, "last_save_enqueue_type", "") or ""),
            "packet_gap_event_count": int(getattr(self, "packet_gap_event_count", 0) or 0),
            "packet_gap_missing": int(getattr(self, "last_packet_gap_missing", 0) or 0),
            "packet_gap_summary": str(getattr(self, "last_packet_gap_summary", "") or ""),
            "packet_gap_diagnostics": dict(getattr(self, "last_packet_gap_diagnostics", {}) or {}),
            "packet_out_of_order_event_count": int(getattr(self, "packet_out_of_order_event_count", 0) or 0),
            "packet_out_of_order_summary": str(getattr(self, "last_packet_out_of_order_summary", "") or ""),
            "packet_counter_reset_event_count": int(getattr(self, "packet_counter_reset_event_count", 0) or 0),
            "packet_counter_reset_summary": str(getattr(self, "last_packet_counter_reset_summary", "") or ""),
            "packet_decode_ms": float(getattr(self, "last_packet_decode_ms", 0.0) or 0.0),
            "data_process_ms": float(getattr(self, "last_data_process_ms", 0.0) or 0.0),
            "save_control_ms": float(getattr(self, "last_save_control_ms", 0.0) or 0.0),
            "gui_update_ms": float(getattr(self, "last_gui_update_ms", 0.0) or 0.0),
            "pipeline_process_ms": float(getattr(self, "last_pipeline_process_ms", 0.0) or 0.0),
            "battery": {
                "rsoc": float(rsoc),
                "reported_rsoc": float(rsoc),
                "stat": float(stat),
                "voltage": float(voltage),
            },
            "power_guard": dict(getattr(self, "power_guard", {}) or {}),
            "mode0_power": dict(getattr(self, "mode0_power", {}) or {}),
            "mode0_quant": {
                "bit_depth": _normalize_mode0_quant_bits(getattr(self, "mode0_quant_bits", MODE0_QUANT_BITS_DEFAULT)),
                "full_scale_uv": _normalize_mode0_quant_full_scale_uv(
                    getattr(self, "mode0_quant_full_scale_uv", MODE0_QUANT_FULL_SCALE_UV_DEFAULT)
                ),
                "raw_full_scale_uv": MODE0_RAW_QUANT_FULL_SCALE_UV,
                "min_bit_depth": MODE0_QUANT_BITS_MIN,
                "max_bit_depth": MODE0_QUANT_BITS_MAX,
                "min_full_scale_uv": MODE0_QUANT_FULL_SCALE_UV_MIN,
                "max_full_scale_uv": MODE0_QUANT_FULL_SCALE_UV_MAX,
                "full_scale_step_uv": MODE0_QUANT_FULL_SCALE_UV_STEP,
            },
            "progress_percent": float(getattr(self, "last_progress_percent", 0.0) or 0.0),
            "run_time_min": float(getattr(self, "last_run_time_min", 0.0) or 0.0),
            "save_flags": {
                "lfp": bool(self.save_file_lfp_flag),
                "mode1": bool(self.save_file_mode1_flag),
                "mode2": bool(self.save_file_mode2_flag),
                "mode3": bool(self.save_file_mode3_flag),
            },
            "mode3_reref_mode": int(getattr(self, "mode3_reref_status", 0) if getattr(self, "mode3_reref_status", None) is not None else 0),
            "mode1_raw_channel": int(getattr(self, "mode1_raw_channel_gui", 0) or 0),
            "mode3_raw_channel": int(getattr(self, "mode3_raw_channel_gui", 0) or 0),
            "mode2_channels": mode2_status_channels,
            "gui_update_intervals_by_mode": gui_intervals,
            "gui_update_interval_best_by_mode": gui_best_intervals,
            "gui_update_interval_stats": gui_interval_stats,
            "gui_update_intervals_by_stream": gui_stream_intervals,
            "gui_update_interval_best_by_stream": gui_stream_best_intervals,
            "gui_update_interval_stream_stats": gui_stream_interval_stats,
            "gui_update_interval_control": {
                "target_fps_enabled": bool(getattr(self, "gui_update_interval_target_fps_enabled", True)),
                "target_fps_by_stream": {
                    key: float(value)
                    for key, value in dict(getattr(self, "gui_update_interval_target_fps_by_stream", {}) or {}).items()
                },
            },
            "detail_enabled": bool(getattr(self, "detail_enabled", False)),
            "detail_throttled": bool(
                getattr(self, "detail_enabled", False)
                and (
                    getattr(self, "detail_payload_throttled", False)
                    or getattr(self, "detail_backpressure_active", False)
                )
            ),
            "detail_dropped_frames": int(getattr(self, "detail_payload_dropped_frames", 0) or 0),
            "detail_last_skip_reason": str(getattr(self, "detail_payload_last_skip_reason", "") or ""),
            "pipeline": pipeline_payload,
            "timestamp_epoch": float(time.time()),
            "rssi": float(getattr(self, "rssi", 0.0) or 0.0),
        })
        self._last_status_emit_monotonic = now_mono

    def begin_mode_transition(self, grace_ms=150):
        now_ms = time.time() * 1000.0
        grace_ms = max(0.0, float(grace_ms))
        self._transition_grace_until_ms = now_ms + grace_ms
        self._transition_pending_reset = True
        SerialPort._reset_packet_loss_streams(self, streams=None)
        if self.save_file_lfp_flag:
            self._save_grace_until_ms[0] = max(self._save_grace_until_ms[0], self._transition_grace_until_ms)
        if self.save_file_mode1_flag:
            self._save_grace_until_ms[1] = max(self._save_grace_until_ms[1], self._transition_grace_until_ms)
        if self.save_file_mode2_flag:
            self._save_grace_until_ms[2] = max(self._save_grace_until_ms[2], self._transition_grace_until_ms)
        if self.save_file_mode3_flag:
            self._save_grace_until_ms[3] = max(self._save_grace_until_ms[3], self._transition_grace_until_ms)

    def _is_in_transition_grace(self):
        return (time.time() * 1000.0) < float(self._transition_grace_until_ms)

    def _is_in_save_grace(self, mode):
        return (time.time() * 1000.0) < float(self._save_grace_until_ms.get(int(mode), 0.0))

    def _should_save_mode(self, mode):
        mode = int(mode)
        if mode == 0:
            return self.save_file_lfp_flag or self._is_in_save_grace(0)
        if mode == 1:
            return self.save_file_mode1_flag or self._is_in_save_grace(1)
        if mode == 2:
            return self.save_file_mode2_flag or self._is_in_save_grace(2)
        if mode == 3:
            return self.save_file_mode3_flag or self._is_in_save_grace(3)
        return False

    def _should_capture_detail_gui_buffers(self):
        try:
            return bool(getattr(self, "detail_enabled", False))
        except RuntimeError:
            return False

    def _should_capture_mode1_raw_gui_buffer(self):
        try:
            detail_enabled = bool(getattr(self, "detail_enabled", False))
        except RuntimeError:
            detail_enabled = False
        try:
            threshold_enabled = bool(getattr(self, "threshold_samples_enabled", False))
        except RuntimeError:
            threshold_enabled = False
        return detail_enabled or threshold_enabled

    def _set_mode2_stream_format(self, packet_samples, sample_frequency):
        packet_samples = max(1, int(packet_samples))
        sample_frequency = max(1.0, float(sample_frequency))
        self.mode2_packet_samples = packet_samples
        self.mode2_sample_frequency = sample_frequency
        try:
            self.file_size_mode2 = self.file_duration_mode2 // (1000.0 * packet_samples / sample_frequency)
        except Exception:
            pass

    def _reset_runtime_gui_buffers(self):
        self.lfptimestamp_GUI.clear()
        self.lfppacketindex_GUI.clear()
        for ch in self.lfpdata_GUI:
            ch.clear()
        for ch in self.ESAdata_GUI:
            ch.clear()
        self.mode0rawdata_GUI.clear()
        self.mode0rawchannel_GUI.clear()
        self.spiketimestamp_GUI.clear()
        self.spikepacketindex_GUI.clear()
        self.spikedata_GUI.clear()
        self._spike_gui_expected_step = 1
        for ch in self.spikerasterdata_GUI:
            ch.clear()
        self.spiketimestamp_mode2_GUI.clear()
        self.spikepacketindex_mode2_GUI.clear()
        for ch in self.spikedata_mode2_GUI:
            ch.clear()
        self.alignment_mode2_GUI.clear()
        for ch in self.sensordata_GUI:
            ch.clear()
        self.alignment_GUI.clear()
        self._recent_stream_mode_tokens.clear()
        self.current_stream_mode = STREAM_MODE_IDLE

    def _handoff_lfp_gui_buffers(self):
        snapshot = (
            self.lfptimestamp_GUI,
            self.lfppacketindex_GUI,
            self.lfpdata_GUI,
            self.sensordata_GUI,
            self.spikerasterdata_GUI,
            self.ESAdata_GUI,
            getattr(self, "mode0rawdata_GUI", []),
            getattr(self, "mode0rawchannel_GUI", []),
        )
        self.lfptimestamp_GUI = []
        self.lfppacketindex_GUI = []
        self.lfpdata_GUI = [[] for _ in range(16)]
        self.sensordata_GUI = [[] for _ in range(9)]
        self.spikerasterdata_GUI = [[] for _ in range(16)]
        self.ESAdata_GUI = [[] for _ in range(16)]
        self.mode0rawdata_GUI = []
        self.mode0rawchannel_GUI = []
        return snapshot

    def _handoff_mode1_gui_buffers(self):
        snapshot = (
            self.spiketimestamp_GUI,
            self.spikepacketindex_GUI,
            self.spikedata_GUI,
            self.sensordata_GUI,
            self.spikerasterdata_GUI,
            self.alignment_GUI,
        )
        self.spiketimestamp_GUI = []
        self.spikepacketindex_GUI = []
        self.spikedata_GUI = []
        self.sensordata_GUI = [[] for _ in range(9)]
        self.spikerasterdata_GUI = [[] for _ in range(16)]
        self.alignment_GUI = []
        return snapshot

    def _handoff_mode3_raw_gui_buffers(self):
        """Hand off mode3 raw samples without touching LFP/ESA shared buffers.

        Mode3 raw packets do not carry IMU/battery sensor data or raster bins.
        Reusing the mode1 handoff here used to clear sensordata_GUI and
        spikerasterdata_GUI, which belong to the mode3 LFP/ESA stream. That
        cross-stream buffer ownership caused empty sensor payloads in the chart
        and expensive traceback logging during mode3.
        """
        snapshot = (
            self.spiketimestamp_GUI,
            self.spikepacketindex_GUI,
            self.spikedata_GUI,
            [],
            [],
            self.alignment_GUI,
        )
        self.spiketimestamp_GUI = []
        self.spikepacketindex_GUI = []
        self.spikedata_GUI = []
        self.alignment_GUI = []
        return snapshot

    @staticmethod
    def _sequence_len(values):
        try:
            return len(values)
        except Exception:
            return 0

    @staticmethod
    def _tail_sequence_for_detail(values, keep):
        try:
            keep = int(keep)
        except Exception:
            keep = 0
        if values is None:
            return []
        if keep <= 0:
            return []
        length = SerialPort._sequence_len(values)
        if length <= keep:
            return values
        try:
            return values[-keep:]
        except Exception:
            return list(values)[-keep:]

    @staticmethod
    def _first_nested_sequence_len(values):
        try:
            iterator = iter(values)
        except Exception:
            return 0
        for item in iterator:
            length = SerialPort._sequence_len(item)
            if length > 0:
                return int(length)
        return 0

    @staticmethod
    def _tail_nested_sequences_for_detail(values, keep):
        if values is None:
            return []
        try:
            keep = int(keep)
        except Exception:
            keep = 0
        if keep <= 0:
            return [[] for _ in values]
        changed = False
        trimmed = []
        for item in values:
            length = SerialPort._sequence_len(item)
            if length > keep:
                changed = True
                try:
                    trimmed.append(item[-keep:])
                except Exception:
                    trimmed.append(list(item)[-keep:])
            else:
                trimmed.append(item)
        return trimmed if changed else values

    @staticmethod
    def _sample_tail_len_for_packets(sample_count, packet_count, keep_packets):
        try:
            sample_count = int(sample_count)
            packet_count = int(packet_count)
            keep_packets = int(keep_packets)
        except Exception:
            return 0
        if sample_count <= 0:
            return 0
        if packet_count <= 0 or keep_packets >= packet_count:
            return sample_count
        return max(1, min(sample_count, int(round(float(sample_count) * float(keep_packets) / float(packet_count)))))

    @staticmethod
    def _trim_mode0_lfp_detail_payload(
        timestamps,
        packet_indices,
        lfpdata,
        sensordata,
        spikerasterdata,
        esadata,
        mode0rawdata,
        mode0rawchannels,
        max_packets=MODE0_LFP_DETAIL_MAX_PACKETS,
    ):
        packet_count = SerialPort._sequence_len(timestamps)
        try:
            max_packets = int(max_packets)
        except Exception:
            max_packets = int(MODE0_LFP_DETAIL_MAX_PACKETS)
        if max_packets <= 0 or packet_count <= max_packets:
            return (
                timestamps,
                packet_indices,
                lfpdata,
                sensordata,
                spikerasterdata,
                esadata,
                mode0rawdata,
                mode0rawchannels,
            )
        lfp_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._first_nested_sequence_len(lfpdata), packet_count, max_packets
        )
        sensor_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._first_nested_sequence_len(sensordata), packet_count, max_packets
        )
        raster_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._first_nested_sequence_len(spikerasterdata), packet_count, max_packets
        )
        esa_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._first_nested_sequence_len(esadata), packet_count, max_packets
        )
        raw_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._sequence_len(mode0rawdata), packet_count, max_packets
        )
        raw_channel_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._sequence_len(mode0rawchannels), packet_count, max_packets
        )
        return (
            SerialPort._tail_sequence_for_detail(timestamps, max_packets),
            SerialPort._tail_sequence_for_detail(packet_indices, max_packets),
            SerialPort._tail_nested_sequences_for_detail(lfpdata, lfp_keep),
            SerialPort._tail_nested_sequences_for_detail(sensordata, sensor_keep),
            SerialPort._tail_nested_sequences_for_detail(spikerasterdata, raster_keep),
            SerialPort._tail_nested_sequences_for_detail(esadata, esa_keep),
            SerialPort._tail_sequence_for_detail(mode0rawdata, raw_keep),
            SerialPort._tail_sequence_for_detail(mode0rawchannels, raw_channel_keep),
        )

    @staticmethod
    def _trim_mode3_lfp_detail_payload(
        timestamps,
        packet_indices,
        lfpdata,
        sensordata,
        spikerasterdata,
        esadata,
        max_packets=MODE3_LFP_ESA_DETAIL_MAX_PACKETS,
    ):
        packet_count = SerialPort._sequence_len(timestamps)
        try:
            max_packets = int(max_packets)
        except Exception:
            max_packets = int(MODE3_LFP_ESA_DETAIL_MAX_PACKETS)
        if max_packets <= 0 or packet_count <= max_packets:
            return timestamps, packet_indices, lfpdata, sensordata, spikerasterdata, esadata
        lfp_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._first_nested_sequence_len(lfpdata), packet_count, max_packets
        )
        sensor_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._first_nested_sequence_len(sensordata), packet_count, max_packets
        )
        raster_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._first_nested_sequence_len(spikerasterdata), packet_count, max_packets
        )
        esa_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._first_nested_sequence_len(esadata), packet_count, max_packets
        )
        return (
            SerialPort._tail_sequence_for_detail(timestamps, max_packets),
            SerialPort._tail_sequence_for_detail(packet_indices, max_packets),
            SerialPort._tail_nested_sequences_for_detail(lfpdata, lfp_keep),
            SerialPort._tail_nested_sequences_for_detail(sensordata, sensor_keep),
            SerialPort._tail_nested_sequences_for_detail(spikerasterdata, raster_keep),
            SerialPort._tail_nested_sequences_for_detail(esadata, esa_keep),
        )

    @staticmethod
    def _trim_mode3_raw_detail_payload(
        timestamps,
        packet_indices,
        raw_samples,
        sensordata,
        spikerasterdata,
        alignment,
        max_packets=MODE3_RAW_DETAIL_MAX_PACKETS,
    ):
        packet_count = SerialPort._sequence_len(timestamps)
        try:
            max_packets = int(max_packets)
        except Exception:
            max_packets = int(MODE3_RAW_DETAIL_MAX_PACKETS)
        if max_packets <= 0 or packet_count <= max_packets:
            return timestamps, packet_indices, raw_samples, sensordata, spikerasterdata, alignment
        sample_keep = SerialPort._sample_tail_len_for_packets(
            SerialPort._sequence_len(raw_samples), packet_count, max_packets
        )
        return (
            SerialPort._tail_sequence_for_detail(timestamps, max_packets),
            SerialPort._tail_sequence_for_detail(packet_indices, max_packets),
            SerialPort._tail_sequence_for_detail(raw_samples, sample_keep),
            sensordata,
            spikerasterdata,
            SerialPort._tail_sequence_for_detail(alignment, sample_keep),
        )

    def _handoff_mode2_gui_buffers(self):
        snapshot = (
            self.spiketimestamp_mode2_GUI,
            self.spikepacketindex_mode2_GUI,
            self.spikedata_mode2_GUI,
            self.sensordata_GUI,
            self.alignment_mode2_GUI,
        )
        self.spiketimestamp_mode2_GUI = []
        self.spikepacketindex_mode2_GUI = []
        self.spikedata_mode2_GUI = [[] for _ in range(16)]
        self.sensordata_GUI = [[] for _ in range(9)]
        self.alignment_mode2_GUI = []
        return snapshot

    def _finalize_transition_if_needed(self):
        if self._transition_pending_reset and (not self._is_in_transition_grace()):
            self._reset_runtime_gui_buffers()
            self._transition_pending_reset = False

    def _mode0_pending_init(self):
        mode0_sensor_keys = getattr(self, "_mode0_sensor_keys_no_flag", None)
        if not mode0_sensor_keys:
            mode0_sensor_keys = [k for k in MODE0_SENSOR_KEYS if k in getattr(self, "sensors_data", {})]
        if not mode0_sensor_keys:
            mode0_sensor_keys = list(MODE0_SENSOR_KEYS)
        self._mode0_pending = {
            "timestamps": [],
            "packet_indices": [],
            "channels": [[] for _ in range(16)],
            "mand_channels": [[] for _ in range(16)],
            "raw_samples": [],
            "raw_channels": [],
            "raw_alignment": [],
            "raw_alignment_gap_fills": [],
            "mode0_quant_bits": [],
            "mode0_quant_full_scale_uv": [],
            "sensor_masks": [],
            "update_flags": [],
            "sensors": {k: [] for k in mode0_sensor_keys},
        }
        self._mode0_pending_packets = 0

    def _mode3_pending_init(self):
        self._mode3_pending = {
            "timestamps": [],
            "packet_indices": [],
            "lfp_channels": [[] for _ in range(16)],
            "esa_channels": [[] for _ in range(16)],
            "raster_channels": [[] for _ in range(16)],
            "update_flags": [],
            "sensors": {k: [] for k in self._sensor_keys_no_flag},
        }
        self._mode3_pending_packets = 0

    def _mode3_encoded_pending_init(self):
        self._mode3_encoded_pending = {
            "timestamps": [],
            "packet_indices": [],
            "packet_words": [],
            "update_flags": [],
            "sensors": {k: [] for k in self._sensor_keys_no_flag},
        }
        self._mode3_encoded_pending_packets = 0

    def _mode3_raw_pending_init(self):
        self._mode3_raw_pending = {
            "timestamps": [],
            "packet_indices": [],
            "raw_samples": [],
            "raw_channels": [],
            "raw_sizes": [],
            "raw_alignment": [],
        }
        self._mode3_raw_pending_packets = 0

    def _mode3_raw_encoded_pending_init(self):
        self._mode3_raw_encoded_pending = {
            "timestamps": [],
            "packet_indices": [],
            "raw_word_packets": [],
            "raw_channels": [],
            "raw_alignment": [],
        }
        self._mode3_raw_encoded_pending_packets = 0

    def _save_mode0_append(
        self,
        timestamp,
        packet_index,
        ch16_lists,
        mand16_lists,
        raw_samples,
        raw_channel,
        raw_alignment,
        sensor_lists,
        update_flag,
        quant_bits=None,
        quant_full_scale_uv=None,
        raw_alignment_gap_fill=None,
        sensor_mask=0,
    ):
        if self._mode0_pending is None:
            self._mode0_pending_init()
        self._mode0_pending["timestamps"].append(timestamp)
        self._mode0_pending["packet_indices"].append(int(packet_index) & 0xFFFF)
        for i in range(16):
            self._mode0_pending["channels"][i].extend(ch16_lists[i])
            self._mode0_pending["mand_channels"][i].extend(mand16_lists[i])
        self._mode0_pending["raw_samples"].extend(raw_samples)
        self._mode0_pending["raw_channels"].append(int(raw_channel) & 0x0F)
        self._mode0_pending["raw_alignment"].extend(raw_alignment)
        if raw_alignment_gap_fill:
            try:
                prev_pos, values = raw_alignment_gap_fill
                self._mode0_pending["raw_alignment_gap_fills"].append((int(prev_pos), list(values or [])))
            except Exception:
                pass
        self._mode0_pending["mode0_quant_bits"].append(
            _normalize_mode0_quant_bits(MODE0_QUANT_BITS_DEFAULT if quant_bits is None else quant_bits)
        )
        self._mode0_pending["mode0_quant_full_scale_uv"].append(
            _normalize_mode0_quant_full_scale_uv(
                MODE0_QUANT_FULL_SCALE_UV_DEFAULT if quant_full_scale_uv is None else quant_full_scale_uv
            )
        )
        self._mode0_pending["sensor_masks"].append(int(sensor_mask) & 0x03)
        mode0_sensor_keys = list(self._mode0_pending["sensors"].keys())
        for i, k in enumerate(mode0_sensor_keys):
            if i < len(sensor_lists):
                self._mode0_pending["sensors"][k].extend(sensor_lists[i])
        self._mode0_pending["update_flags"].append(update_flag)
        self._mode0_pending_packets += 1
        if self._mode0_pending_packets >= self._save_chunk_packets_mode0:
            self._save_mode0_flush_pending()

    def _save_mode0_flush_pending(self):
        if not self._mode0_pending or self._mode0_pending_packets <= 0:
            return
        msg = {
            "type": "append_chunk",
            "mode": 0,
            "timestamps": self._mode0_pending["timestamps"],
            "packet_indices": self._mode0_pending["packet_indices"],
            "channels": self._mode0_pending["channels"],
            "mand_channels": self._mode0_pending["mand_channels"],
            "raw_samples": self._mode0_pending["raw_samples"],
            "raw_channels": self._mode0_pending["raw_channels"],
            "raw_alignment": self._mode0_pending["raw_alignment"],
            "raw_alignment_gap_fills": self._mode0_pending["raw_alignment_gap_fills"],
            "mode0_quant_bits": self._mode0_pending["mode0_quant_bits"],
            "mode0_quant_full_scale_uv": self._mode0_pending["mode0_quant_full_scale_uv"],
            "sensor_masks": self._mode0_pending["sensor_masks"],
            "sensors": self._mode0_pending["sensors"],
            "update_flags": self._mode0_pending["update_flags"],
        }
        self._save_put(msg)
        self._mode0_pending_init()

    def _save_mode3_append(self, timestamp, packet_index, lfp16_lists, esa16_lists, raster16_lists, sensor_lists_9, update_flag):
        if self._mode3_pending is None:
            self._mode3_pending_init()
        self._mode3_pending["timestamps"].append(timestamp)
        self._mode3_pending["packet_indices"].append(int(packet_index) & 0xFFFF)
        for i in range(16):
            self._mode3_pending["lfp_channels"][i].extend(lfp16_lists[i])
            self._mode3_pending["esa_channels"][i].extend(esa16_lists[i])
            self._mode3_pending["raster_channels"][i].extend(raster16_lists[i])
        for i, k in enumerate(self._sensor_keys_no_flag):
            self._mode3_pending["sensors"][k].extend(sensor_lists_9[i])

        self._mode3_pending["update_flags"].append(update_flag)
        self._mode3_pending_packets += 1
        if self._mode3_pending_packets >= self._save_chunk_packets_mode3:
            self._save_mode3_flush_pending()

    def _save_mode3_flush_pending(self):
        if not self._mode3_pending or self._mode3_pending_packets <= 0:
            return
        msg = {
            "type": "append_chunk",
            "mode": 3,
            "timestamps": self._mode3_pending["timestamps"],
            "packet_indices": self._mode3_pending["packet_indices"],
            "lfp_channels": self._mode3_pending["lfp_channels"],
            "esa_channels": self._mode3_pending["esa_channels"],
            "raster_channels": self._mode3_pending["raster_channels"],
            "sensors": self._mode3_pending["sensors"],
            "update_flags": self._mode3_pending["update_flags"],
        }
        self._save_put(msg)
        self._mode3_pending_init()

    def _save_mode3_encoded_append(self, timestamp, packet_index, packet_words, sensor_lists_9, update_flag):
        if self._mode3_encoded_pending is None:
            self._mode3_encoded_pending_init()
        self._mode3_encoded_pending["timestamps"].append(timestamp)
        self._mode3_encoded_pending["packet_indices"].append(int(packet_index) & 0xFFFF)
        self._mode3_encoded_pending["packet_words"].append([str(word) for word in _packet_words_to_list(packet_words)])
        for i, k in enumerate(self._sensor_keys_no_flag):
            self._mode3_encoded_pending["sensors"][k].extend(sensor_lists_9[i])
        self._mode3_encoded_pending["update_flags"].append(update_flag)
        self._mode3_encoded_pending_packets += 1
        if self._mode3_encoded_pending_packets >= self._save_chunk_packets_mode3:
            self._save_mode3_encoded_flush_pending()

    def _save_mode3_encoded_flush_pending(self):
        if not self._mode3_encoded_pending or self._mode3_encoded_pending_packets <= 0:
            return
        msg = {
            "type": "append_mode3_encoded",
            "timestamps": self._mode3_encoded_pending["timestamps"],
            "packet_indices": self._mode3_encoded_pending["packet_indices"],
            "packet_words": self._mode3_encoded_pending["packet_words"],
            "sensors": self._mode3_encoded_pending["sensors"],
            "update_flags": self._mode3_encoded_pending["update_flags"],
            "raw_data_per_packet_mode3": int(getattr(self, "raw_data_per_packet_mode3", 2) or 2),
            "dac_resolution": float(getattr(self, "DAC_resolution", DEFAULT_DAC_RESOLUTION) or DEFAULT_DAC_RESOLUTION),
        }
        self._save_put(msg)
        self._mode3_encoded_pending_init()

    def _save_mode3_raw_append(self, timestamp, packet_index, raw_samples, raw_channel, raw_alignment=None):
        if self._mode3_raw_pending is None:
            self._mode3_raw_pending_init()
        self._mode3_raw_pending["timestamps"].append(timestamp)
        self._mode3_raw_pending["packet_indices"].append(int(packet_index) & 0xFFFF)
        self._mode3_raw_pending["raw_samples"].extend(raw_samples)
        self._mode3_raw_pending["raw_channels"].append(raw_channel)
        self._mode3_raw_pending["raw_sizes"].append(len(raw_samples))
        
        if raw_alignment is not None and len(raw_alignment) > 0:
            self._mode3_raw_pending["raw_alignment"].extend(raw_alignment)
        else:
            self._mode3_raw_pending["raw_alignment"].extend([0.0] * len(raw_samples))
            
        self._mode3_raw_pending_packets += 1
        if self._mode3_raw_pending_packets >= self._save_chunk_packets_mode3_raw:
            self._save_mode3_raw_flush_pending()

    def _save_mode3_raw_flush_pending(self):
        if not self._mode3_raw_pending or self._mode3_raw_pending_packets <= 0:
            return
        msg = {
            "type": "append_mode3_raw",
            "timestamps": self._mode3_raw_pending["timestamps"],
            "packet_indices": self._mode3_raw_pending["packet_indices"],
            "raw_samples": self._mode3_raw_pending["raw_samples"],
            "raw_channels": self._mode3_raw_pending["raw_channels"],
            "raw_sizes": self._mode3_raw_pending["raw_sizes"],
            "raw_alignment": self._mode3_raw_pending["raw_alignment"],
        }
        self._save_put(msg)
        self._mode3_raw_pending_init()

    def _save_mode3_raw_encoded_append(self, timestamp, packet_index, raw_words, raw_channel, raw_alignment=None):
        if self._mode3_raw_encoded_pending is None:
            self._mode3_raw_encoded_pending_init()
        raw_words = [str(word) for word in _packet_words_to_list(raw_words)]
        self._mode3_raw_encoded_pending["timestamps"].append(timestamp)
        self._mode3_raw_encoded_pending["packet_indices"].append(int(packet_index) & 0xFFFF)
        self._mode3_raw_encoded_pending["raw_word_packets"].append(raw_words)
        self._mode3_raw_encoded_pending["raw_channels"].append(raw_channel)

        if raw_alignment is not None and len(raw_alignment) > 0:
            self._mode3_raw_encoded_pending["raw_alignment"].extend(raw_alignment)
        else:
            self._mode3_raw_encoded_pending["raw_alignment"].extend([0.0] * len(raw_words))

        self._mode3_raw_encoded_pending_packets += 1
        if self._mode3_raw_encoded_pending_packets >= self._save_chunk_packets_mode3_raw:
            self._save_mode3_raw_encoded_flush_pending()

    def _save_mode3_raw_encoded_flush_pending(self):
        if not self._mode3_raw_encoded_pending or self._mode3_raw_encoded_pending_packets <= 0:
            return
        msg = {
            "type": "append_mode3_raw_encoded",
            "timestamps": self._mode3_raw_encoded_pending["timestamps"],
            "packet_indices": self._mode3_raw_encoded_pending["packet_indices"],
            "raw_word_packets": self._mode3_raw_encoded_pending["raw_word_packets"],
            "raw_channels": self._mode3_raw_encoded_pending["raw_channels"],
            "raw_alignment": self._mode3_raw_encoded_pending["raw_alignment"],
            "dac_resolution": float(getattr(self, "DAC_resolution", DEFAULT_DAC_RESOLUTION) or DEFAULT_DAC_RESOLUTION),
        }
        self._save_put(msg)
        self._mode3_raw_encoded_pending_init()

    def _has_mode0_pending_save_data(self):
        pending = self._mode0_pending or {}
        return bool(
            int(self.LFPRawCounter or 0) > 0
            or int(self._mode0_pending_packets or 0) > 0
            or list(pending.get("timestamps", []) or [])
        )

    def _has_mode1_pending_save_data(self):
        packet_data = self.AP_data or {}
        return bool(
            int(self.SPIKERawCounter or 0) > 0
            or list(packet_data.get("Raw_timestamp", []) or [])
            or list(packet_data.get("AP_timestamp", []) or [])
            or list(packet_data.get("PacketIndex", []) or [])
        )

    def _has_mode2_pending_save_data(self):
        packet_data = self.AP_LFP_data or {}
        sensor_data = getattr(self, "mode2_sensors_data", {}) or {}
        return bool(
            int(self.Mode2RawCounter or 0) > 0
            or list(packet_data.get("TimeStamp", []) or [])
            or list(packet_data.get("PacketIndex", []) or [])
            or any(list(sensor_data.get(key, []) or []) for key in MODE2_SENSOR_KEYS)
        )

    def _has_mode3_pending_save_data(self):
        def _safe_attr(name, default=None):
            try:
                return getattr(self, name, default)
            except RuntimeError:
                return default
        pending = _safe_attr("_mode3_pending", None) or {}
        encoded_pending = _safe_attr("_mode3_encoded_pending", None) or {}
        raw_pending = _safe_attr("_mode3_raw_pending", None) or {}
        raw_encoded_pending = _safe_attr("_mode3_raw_encoded_pending", None) or {}
        return bool(
            int(self.Mode3RawCounter or 0) > 0
            or int(_safe_attr("_mode3_pending_packets", 0) or 0) > 0
            or int(_safe_attr("_mode3_encoded_pending_packets", 0) or 0) > 0
            or int(_safe_attr("_mode3_raw_pending_packets", 0) or 0) > 0
            or int(_safe_attr("_mode3_raw_encoded_pending_packets", 0) or 0) > 0
            or list(pending.get("timestamps", []) or [])
            or list(encoded_pending.get("timestamps", []) or [])
            or list(raw_pending.get("timestamps", []) or [])
            or list(raw_encoded_pending.get("timestamps", []) or [])
        )

    def finalize_save_buffers(self, modes=None):
        requested_modes = ("lfp", "mode1", "mode2", "mode3")
        if modes is not None:
            if isinstance(modes, str):
                requested_modes = (str(modes),)
            else:
                requested_modes = tuple(str(mode) for mode in list(modes or []))

        finalized = []
        with self._data_lock:
            if "lfp" in requested_modes and self._has_mode0_pending_save_data():
                self.save_lfp_file(self.lfp_file_addr, manual_save=True)
                finalized.append("lfp")
            if "mode1" in requested_modes and self._has_mode1_pending_save_data():
                self.save_spike_mode1_file(self.mode1_file_addr, manual_save=True)
                finalized.append("mode1")
            if "mode2" in requested_modes and self._has_mode2_pending_save_data():
                self.save_spike_mode2_file(self.mode2_file_addr, manual_save=True)
                finalized.append("mode2")
            if "mode3" in requested_modes and self._has_mode3_pending_save_data():
                self.save_spike_mode3_file(self.mode3_file_addr, manual_save=True)
                finalized.append("mode3")
        return finalized

    def _emit_save_progress(self, mode, percent, run_time_min, force=False):
        mode = int(mode)
        self.last_progress_percent = float(percent)
        self.last_run_time_min = float(run_time_min)
        last_by_mode = getattr(self, "_last_progress_emit_monotonic", None)
        if not isinstance(last_by_mode, dict):
            last_by_mode = {}
            self._last_progress_emit_monotonic = last_by_mode
        now_mono = time.monotonic()
        interval_s = max(0.0, float(getattr(self, "progress_emit_interval_s", 10.0) or 10.0))
        last_mono = last_by_mode.get(mode)
        if (not force) and last_mono is not None and interval_s > 0.0 and (now_mono - float(last_mono)) < interval_s:
            return False
        last_by_mode[mode] = now_mono
        self.ProgressUpdate.emit(mode, self.last_progress_percent, self.last_run_time_min)
        self._emit_status_update(force=bool(force))
        return True
    
    def send_data(self ,data):
        """ write commands to peripheral """
        try:
            payload = self._normalize_write_payload(data)
            if not payload:
                logging.warning("send_data: empty payload, skip sending")
                return 0
            if not self._port_is_open():
                logging.warning("send_data: port is closed, cannot send")
                self._notify_disconnect("Serial port is closed")
                return 0
            with self._io_lock:
                n = self.port.write(payload)
                self.port.flush()
            self.last_write_monotonic = time.monotonic()
            if n != len(payload):
                raise serial.SerialTimeoutException(
                    f"Partial serial write: expected {len(payload)} bytes, wrote {n} bytes"
                )
            return n
        except (serial.SerialException, serial.SerialTimeoutException, OSError, TypeError, ValueError) as e:
            logging.error(f"send_data failed: {e}")
            self._notify_disconnect(str(e))
            return 0

    def send_command_frame(self, data, repeats=1, retry_delay_s=0.03):
        try:
            payload = self._normalize_write_payload(data)
            frame = build_relay_command_frame(payload)
        except (TypeError, ValueError) as e:
            logging.error(f"send_command_frame failed: {e}")
            return False

        repeat_count = max(1, int(repeats))
        delay_s = max(0.0, float(retry_delay_s))
        for attempt in range(repeat_count):
            bytes_written = self.send_data(frame)
            if bytes_written != len(frame):
                return False
            if attempt + 1 < repeat_count and delay_s > 0.0:
                time.sleep(delay_s)
        return True

    def send_relay_control_command(self, command_name, repeats=1, retry_delay_s=0.03):
        try:
            payload = build_relay_local_command_payload(command_name)
        except ValueError as e:
            logging.error(f"send_relay_control_command failed: {e}")
            return False
        return self.send_command_frame(payload, repeats=repeats, retry_delay_s=retry_delay_s)

    @staticmethod
    def _mode2_v2_serial_packet_match_at(buffer, offset=0):
        try:
            view = memoryview(buffer)
        except TypeError:
            return None
        offset = max(0, int(offset or 0))
        if len(view) - offset < 2 or int(view[offset + 1]) != 0x05:
            return None
        marker = WIRELESS_PACKET_SEPARATOR_MARKER
        marker_len = len(marker)
        candidates = (
            (MODE2_V2_EXT_PACKET_WORDS * 2, MODE2_V2_EXT_SERIAL_PACKET_BYTES, False),
            (MODE2_V2_PACKET_WORDS * 2, MODE2_V2_SERIAL_PACKET_BYTES, True),
        )
        for packet_payload_bytes, serial_packet_bytes, requires_base_format in candidates:
            if offset + serial_packet_bytes > len(view):
                continue
            separator_start = offset + packet_payload_bytes + 2
            separator_end = separator_start + marker_len
            if bytes(view[separator_start:separator_end]) != marker:
                continue
            if requires_base_format and int(view[offset]) != MODE2_V2_FORMAT_BYTE:
                continue
            return {
                "payload_bytes": packet_payload_bytes,
                "serial_packet_bytes": serial_packet_bytes,
                "is_base": bool(requires_base_format),
            }
        return None

    @staticmethod
    def _find_mode2_v2_serial_packet_start(buffer, start=0, stop=None):
        try:
            view = memoryview(buffer)
        except TypeError:
            return None
        start = max(0, int(start or 0))
        stop = len(view) - 1 if stop is None else min(len(view) - 1, max(0, int(stop)))
        for idx in range(start, stop):
            if int(view[idx + 1]) != 0x05:
                continue
            if SerialPort._mode2_v2_serial_packet_match_at(view, idx) is not None:
                return idx
        return None

    @staticmethod
    def _buffer_starts_like_mode2_v2_partial(buffer):
        try:
            view = memoryview(buffer)
        except TypeError:
            return False
        if len(view) < 2:
            return False
        if bytes(view[:len(WIRELESS_GROUP_END_MARKER)]) == WIRELESS_GROUP_END_MARKER:
            if len(view) < len(WIRELESS_GROUP_END_MARKER) + 2:
                return True
            return int(view[len(WIRELESS_GROUP_END_MARKER) + 1]) == 0x05
        return int(view[1]) == 0x05

    @staticmethod
    def _extract_mode2_v2_binary_serial_frames(full_frame):
        if not isinstance(full_frame, bytearray) or not full_frame:
            return [], 0, 0

        looks_like_mode2 = SerialPort._buffer_starts_like_mode2_v2_partial(full_frame)
        if not looks_like_mode2:
            first_start = SerialPort._find_mode2_v2_serial_packet_start(full_frame, 0, min(len(full_frame), 64))
            looks_like_mode2 = first_start is not None
        if not looks_like_mode2:
            return [], 0, 0

        frames = []
        serial_packets = []
        packet_count = 0
        resync_count = 0

        def flush_packets():
            nonlocal serial_packets
            if serial_packets:
                frames.append(b"".join(serial_packets) + WIRELESS_GROUP_END_MARKER)
                serial_packets = []

        while full_frame:
            if full_frame.startswith(WIRELESS_GROUP_END_MARKER):
                del full_frame[:len(WIRELESS_GROUP_END_MARKER)]
                continue

            match = SerialPort._mode2_v2_serial_packet_match_at(full_frame, 0)
            if match is not None:
                serial_packet_bytes = int(match["serial_packet_bytes"])
                serial_packets.append(bytes(full_frame[:serial_packet_bytes]))
                del full_frame[:serial_packet_bytes]
                packet_count += 1
                if len(serial_packets) >= WIRELESS_GROUP_PACKETS:
                    flush_packets()
                continue

            if len(full_frame) < MODE2_V2_MIN_SERIAL_PACKET_BYTES:
                break

            next_start = SerialPort._find_mode2_v2_serial_packet_start(full_frame, 1)
            if next_start is None:
                keep = MODE2_V2_MAX_SERIAL_PACKET_BYTES - 1
                if len(full_frame) > keep:
                    del full_frame[:len(full_frame) - keep]
                    resync_count += 1
                break

            del full_frame[:next_start]
            resync_count += 1

        flush_packets()
        return frames, packet_count, resync_count

    def _drain_mode2_v2_binary_serial_frames(self, full_frame):
        frames, packet_count, resync_count = SerialPort._extract_mode2_v2_binary_serial_frames(full_frame)
        if not frames:
            return False
        self.serial_mode2_direct_frame_count = int(getattr(self, "serial_mode2_direct_frame_count", 0) or 0) + len(frames)
        self.serial_mode2_direct_packet_count = int(getattr(self, "serial_mode2_direct_packet_count", 0) or 0) + int(packet_count)
        if resync_count:
            self.serial_mode2_resync_count = int(getattr(self, "serial_mode2_resync_count", 0) or 0) + int(resync_count)
            self.serial_frame_false_marker_count = int(getattr(self, "serial_frame_false_marker_count", 0) or 0) + int(resync_count)
        for frame in frames:
            SerialPort._submit_raw_frame_to_pipeline(self, frame)
        return True

    def _submit_complete_wireless_frames(self, full_frame):
        submitted = False
        marker = WIRELESS_GROUP_END_MARKER
        marker_len = len(marker)
        search_start = 0
        resync_prefix_frame_end = None
        while True:
            marker_index = full_frame.find(marker, search_start)
            if marker_index < 0:
                break
            frame_end = marker_index + marker_len
            frame = bytes(full_frame[:frame_end])
            if not SerialPort._wireless_frame_candidate_is_complete(frame):
                if frame_end >= len(full_frame):
                    if resync_prefix_frame_end is not None:
                        self.serial_frame_false_marker_count = int(
                            getattr(self, "serial_frame_false_marker_count", 0) or 0
                        ) + 1
                        del full_frame[:resync_prefix_frame_end]
                        search_start = 0
                        resync_prefix_frame_end = None
                        continue
                    self.serial_frame_incomplete_wait_count = int(
                        getattr(self, "serial_frame_incomplete_wait_count", 0) or 0
                    ) + 1
                    break
                self.serial_frame_false_marker_count = int(
                    getattr(self, "serial_frame_false_marker_count", 0) or 0
                ) + 1
                if resync_prefix_frame_end is None:
                    resync_prefix_frame_end = frame_end
                search_start = marker_index + 1
                continue
            del full_frame[:frame_end]
            SerialPort._submit_raw_frame_to_pipeline(self, frame)
            submitted = True
            search_start = 0
            resync_prefix_frame_end = None
        return submitted
    
    def read_data(self):
        """ read fifo of usbd """
        try:
            self.flush()
        except Exception:
            if self._closing:
                return
            raise
        full_frame = bytearray()

        while self._running:
            try:
                try:
                    waiting_before_read = int(getattr(self.port, "in_waiting", 0) or 0)
                except Exception:
                    waiting_before_read = 0
                temp_frame = self.port.read_all()
            except (serial.SerialException, OSError) as e:
                if self._closing or (not self._running):
                    break
                logging.warning(f"Serial read terminated: {e}")
                self.last_error = str(e)
                break
            if len(temp_frame) == 0:
                SerialPort._drain_pipeline_events(self)
                idle_sleep_s = float(getattr(self, "serial_idle_sleep_s", 0.0) or 0.0)
                if idle_sleep_s > 0.0:
                    time.sleep(idle_sleep_s)
                continue
            self._record_serial_backlog_event(len(temp_frame))
            now_mono = time.monotonic()
            previous_read_mono = float(getattr(self, "last_packet_monotonic", 0.0) or 0.0)
            if previous_read_mono > 0.0:
                self._record_serial_read_gap_event(
                    (now_mono - previous_read_mono) * 1000.0,
                    waiting_bytes=waiting_before_read,
                    batch_bytes=len(temp_frame),
                )
            self.last_packet_monotonic = now_mono
            SerialPort._record_read_batch_size(self, len(temp_frame))
            full_frame += bytearray(temp_frame)
            SerialPort._drain_mode2_v2_binary_serial_frames(self, full_frame)
            if not SerialPort._buffer_starts_like_mode2_v2_partial(full_frame):
                SerialPort._submit_complete_wireless_frames(self, full_frame)
            SerialPort._drain_pipeline_events(self)

    def _emit_impedance_progress(self, packet_words):
        if len(packet_words) < 5:
            return
        stage = int(packet_words[0][0:2], 16)
        timestamp_ms = int(swap16Hex(packet_words[1]) + swap16Hex(packet_words[2]), 16)
        progress_word = int(swap16Hex(packet_words[3]), 16)
        current_channel = progress_word & 0xFF
        percent = (progress_word >> 8) & 0xFF
        total_channels = int(swap16Hex(packet_words[4]), 16)

        self.impedance_test_active = True
        self.ImpedanceProgress.emit({
            "stage": int(stage),
            "timestamp_ms": int(timestamp_ms),
            "current_channel": int(current_channel),
            "total_channels": int(total_channels),
            "percent": int(percent),
        })
        self._emit_status_update(force=True)

    def _emit_impedance_result(self, packet_words):
        if len(packet_words) < 4:
            return
        channel_count = int(packet_words[0][0:2], 16)
        timestamp_ms = int(swap16Hex(packet_words[1]) + swap16Hex(packet_words[2]), 16)
        sample_mode = int(swap16Hex(packet_words[3]), 16)

        impedance_values = []
        impedance_phase_cdeg = []
        expected_words = channel_count * 3
        value_words = packet_words[4:4 + expected_words]
        for idx in range(0, len(value_words), 3):
            if idx + 2 >= len(value_words):
                break
            high_word = int(swap16Hex(value_words[idx]), 16)
            low_word = int(swap16Hex(value_words[idx + 1]), 16)
            impedance_values.append((high_word << 16) | low_word)
            impedance_phase_cdeg.append(_decode_signed_u16_hex(value_words[idx + 2]))

        self.impedance_test_active = False
        self.impedance_last_result = {
            "impedance_ohm": impedance_values,
            "impedance_phase_cdeg": impedance_phase_cdeg,
        }
        self.ImpedanceResult.emit({
            "channel_count": int(channel_count),
            "timestamp_ms": int(timestamp_ms),
            "sample_mode": int(sample_mode),
            "impedance_ohm": impedance_values,
            "impedance_phase_cdeg": impedance_phase_cdeg,
            "impedance_phase_deg": [float(v) / 100.0 for v in impedance_phase_cdeg],
        })
        self._emit_status_update(force=True)

    @staticmethod
    def _split_mode2_v2_binary_packets(frame):
        if not frame or not frame.endswith(WIRELESS_GROUP_END_MARKER):
            return None
        payload = frame[:-len(WIRELESS_GROUP_END_MARKER)]
        if len(payload) == 0:
            return None
        packets = []
        offset = 0
        marker_len = len(WIRELESS_PACKET_SEPARATOR_MARKER)
        candidates = (
            (MODE2_V2_EXT_PACKET_WORDS * 2, MODE2_V2_EXT_SERIAL_PACKET_BYTES, False),
            (MODE2_V2_PACKET_WORDS * 2, MODE2_V2_SERIAL_PACKET_BYTES, True),
        )
        while offset < len(payload):
            matched = False
            for packet_payload_bytes, serial_packet_bytes, requires_base_format in candidates:
                if offset + serial_packet_bytes > len(payload):
                    continue
                serial_packet = payload[offset:offset + serial_packet_bytes]
                separator_start = packet_payload_bytes + 2
                separator_end = separator_start + marker_len
                if serial_packet[separator_start:separator_end] != WIRELESS_PACKET_SEPARATOR_MARKER:
                    continue
                packet_bytes = serial_packet[:packet_payload_bytes]
                if len(packet_bytes) != packet_payload_bytes or packet_bytes[1] != 0x05:
                    continue
                if requires_base_format and packet_bytes[0] != MODE2_V2_FORMAT_BYTE:
                    continue
                packets.append(packet_bytes)
                offset += serial_packet_bytes
                matched = True
                break
            if not matched:
                return None
        return packets

    @staticmethod
    def _is_mode2_v2_binary_frame_bytes(frame):
        packets = SerialPort._split_mode2_v2_binary_packets(frame)
        return bool(packets)

    @staticmethod
    def _wireless_frame_candidate_is_complete(frame):
        try:
            frame = bytes(frame)
        except Exception:
            return False
        if SerialPort._is_mode2_v2_binary_frame_bytes(frame):
            return True
        try:
            packets = split_wireless_serial_packets(frame)
        except Exception:
            return False
        if len(packets) == WIRELESS_GROUP_PACKETS:
            return True
        if len(packets) == 1:
            try:
                return int(packets[0][2:4], 16) in {3}
            except Exception:
                return False
        return False

    def _process_mode2_v2_binary_frame(self, read_data):
        try:
            frame = bytes(read_data)
        except Exception:
            return False
        packets = SerialPort._split_mode2_v2_binary_packets(frame)
        if not packets:
            return False

        read_batch_packet_counts = {mode: 0 for mode in GUI_INTERVAL_DEFAULTS}
        for packet_bytes in packets:
            read_batch_packet_counts[2] += 1
            self._record_stream_mode_packet(STREAM_MODE_MODE2)
            timestamp = (
                (int.from_bytes(packet_bytes[2:4], "little") << 16)
                | int.from_bytes(packet_bytes[4:6], "little")
            )
            packet_counter = int.from_bytes(packet_bytes[6:8], "little") & 0xFFFF
            self.Timestamp_neural_signal = timestamp
            self.overflowSignal[1] = 0
            accel_raw = _decode_mode2_v2_accel_q13_bytes(packet_bytes)
            self.sensor_update_flag = 1 if accel_raw is not None else 0

            if self.mode2_v2_raw_packet_process(
                timestamp,
                packet_counter,
                None,
                payload_bytes=packet_bytes[8:8 + MODE2_V2_PAYLOAD_BYTES],
                accel_raw=accel_raw,
                accel_timestamp=timestamp,
            ) is False:
                continue

            if self._should_save_mode(2):
                self.Mode2RawCounter += 1

        SerialPort._record_read_batch_packet_counts(self, read_batch_packet_counts)
        return True
     
    def data_process_full(self, read_data):
        self._finalize_transition_if_needed()

        if(len(read_data)!= 0):
            if self._process_mode2_v2_binary_frame(read_data):
                return
            spilt_temp = split_wireless_serial_packets(read_data) # 注意，这个正则化表达式很重要
            read_batch_packet_counts = {mode: 0 for mode in GUI_INTERVAL_DEFAULTS}
            
            for _, packets in enumerate(spilt_temp):
                # get the category of packets
                try:
                    packets_type = int(packets[2:4], 16)
                    # rssi value occupy one short
                    self.rssi = round(int(packets[-4:-2], 16) * 0.01 + self.rssi * 0.99 , 2) # averaged
                    packets = packets[0:-5]
                    packet_length = (len(packets) + 1) / 5 
                except:
                    packets_type = -1
                    packet_length = 0
                    pass
                # packet proprocessing 
                """ mode 0 """
                if(packets_type == 1): 
                    packet_words = packets.split()
                    mode0_quant_bits = _normalize_mode0_quant_bits(
                        getattr(self, "mode0_quant_bits", MODE0_QUANT_BITS_DEFAULT)
                    )
                    mode0_packet_base_words = _mode0_packet_base_words_for_bits(mode0_quant_bits)
                    if len(packet_words) < mode0_packet_base_words:
                        continue
                    header_low = int(packet_words[0][0:2], 16)
                    mode0_has_accel = bool(header_low & MODE0_HEADER_ACCEL_FLAG)
                    mode0_has_status = bool(header_low & MODE0_HEADER_STATUS_FLAG)
                    mode0_expected_words = (
                        mode0_packet_base_words
                        + (MODE0_ACCEL_WORDS if mode0_has_accel else 0)
                        + (MODE0_STATUS_WORDS if mode0_has_status else 0)
                    )
                    if len(packet_words) != mode0_expected_words:
                        continue
                    read_batch_packet_counts[0] += 1
                    self._record_stream_mode_packet(STREAM_MODE_MODE0)
                    # get timestamp ms
                    self.Timestamp_recorder_counter = int(swap16Hex(packet_words[1]) + swap16Hex(packet_words[2]), 16)
                    self.Timestamp_neural_signal = self.Timestamp_recorder_counter # int(time.time() * 1000) 
                    packet_counter = int(swap16Hex(packet_words[-1]), 16) & 0xFFFF
                    payload_words = packet_words[3:-1]
                    payload = (' '.join(payload_words) + ' ')
                    mode0_raw_channel = header_low & MODE0_HEADER_RAW_CHANNEL_MASK
                    self.mode3_raw_channel_gui = mode0_raw_channel

                    ###### Data process ######
                    _ = int(packets[0:2] ,16) # channel num of each packets
                    self.overflowSignal[1] = 1 if (header_low & MODE0_HEADER_OVERFLOW_FLAG) else 0
                    self.sensor_update_flag = 1 if (mode0_has_accel or mode0_has_status) else 0
                    # lfp packets
                    if self.lfp_packets_process(payload, packet_counter, mode0_raw_channel, mode0_has_accel, mode0_has_status) is False:
                        continue

                    # file saving
                    if(self._should_save_mode(0)):
                        self.LFPRawCounter += 1

                    """  Test Mode: mode 1 """
                elif(packets_type == 2 and packet_length == 109): 
                    read_batch_packet_counts[1] += 1
                    self._record_stream_mode_packet(STREAM_MODE_MODE1)
                    self.Timestamp_recorder_counter = int(swap16Hex(packets[5:9]) + swap16Hex(packets[10:14]), 16)
                    self.Timestamp_neural_signal = self.Timestamp_recorder_counter # int(time.time() * 1000)
                    packet_words = packets.split(' ')
                    packet_counter = int(swap16Hex(packet_words[-1]), 16) & 0xFFFF
                    payload = (' '.join(packet_words[4:-1]) + ' ')
                    
                    self.spike_channel_index_mode1_3 = int(packets[0:2] ,16) # physical RHD channel index of current packets
                    self.mode1_raw_channel_gui = int(self.spike_channel_index_mode1_3)
                    self._spike_gui_expected_step = 1
                    self.overflowSignal[1] = int(packets[15:17] ,16) # current signal
                    self.sensor_update_flag = int(packets[17:19] ,16) # sensor signal

                    if self.spike_packets_process(payload, packet_counter) is False:
                        continue
                    # file saving
                    if(self._should_save_mode(1)):
                        self.SPIKERawCounter += 1
                    pass
                
                    """  spike packets: mode 2 v2 """
                elif(
                    packets_type == 5
                    and packet_length in (MODE2_V2_PACKET_WORDS, MODE2_V2_EXT_PACKET_WORDS)
                ):
                    is_mode2_base = int(packet_length) == MODE2_V2_PACKET_WORDS
                    if is_mode2_base and int(packets[0:2], 16) != MODE2_V2_FORMAT_BYTE:
                        continue
                    read_batch_packet_counts[2] += 1
                    self._record_stream_mode_packet(STREAM_MODE_MODE2)
                    packet_words = packets.split(' ')
                    self.Timestamp_neural_signal = int(
                        swap16Hex(packet_words[1]) + swap16Hex(packet_words[2]),
                        16,
                    )
                    packet_counter = int(swap16Hex(packet_words[3]), 16) & 0xFFFF
                    self.overflowSignal[1] = 0
                    packet_bytes_for_accel = bytes(_packet_words_to_wire_bytes(packet_words))
                    accel_raw = _decode_mode2_v2_accel_q13_bytes(packet_bytes_for_accel)
                    self.sensor_update_flag = 1 if accel_raw is not None else 0

                    if self.mode2_v2_raw_packet_process(
                        self.Timestamp_neural_signal,
                        packet_counter,
                        packet_words,
                        accel_raw=accel_raw,
                        accel_timestamp=self.Timestamp_neural_signal,
                    ) is False:
                        continue

                    if(self._should_save_mode(2)):
                        self.Mode2RawCounter += 1
                    pass

                    """ ESA packets: mode 3 """
                elif(packets_type == 7 and packet_length == 81): 
                    read_batch_packet_counts[3] += 1
                    self._record_stream_mode_packet(STREAM_MODE_MODE3)
                    self.Timestamp_recorder_counter = int(swap16Hex(packets[5:9]) + swap16Hex(packets[10:14]), 16)
                    self.Timestamp_neural_signal = self.Timestamp_recorder_counter # int(time.time() * 1000)
                    packet_words = packets.split(' ')
                    packet_counter = int(swap16Hex(packet_words[-1]), 16) & 0xFFFF
                    payload = (' '.join(packet_words[4:-1]) + ' ')
                    self.mode3_reref_status = int(packets[0:2] ,16)

                    self.overflowSignal[1] = int(packets[15:17] ,16) # current signal
                    self.sensor_update_flag = int(packets[17:19] ,16)# sensor signal
                    if self.mode_3_packets_process(payload, packet_counter) is False:
                        continue

                    # file saving
                    if(self._should_save_mode(3)):
                        self.Mode3RawCounter += 1
                    pass
                
                elif(packets_type == 8 and packet_length == 105): # single channel Raw data recording
                    read_batch_packet_counts[3] += 1
                    self._record_stream_mode_packet(STREAM_MODE_MODE3)
                    self.Timestamp_recorder_counter = int(swap16Hex(packets[5:9]) + swap16Hex(packets[10:14]), 16) # timestamp
                    self.Timestamp_neural_signal = self.Timestamp_recorder_counter
                    packet_words = packets.split(' ')
                    packet_counter = int(swap16Hex(packet_words[-1]), 16) & 0xFFFF
                    payload = (' '.join(packet_words[4:-1]) + ' ')

                    self.spike_channel_index_mode1_3 = int(packets[0:2] ,16) # channel index of current packets
                    self.mode3_raw_channel_gui = int(self.spike_channel_index_mode1_3)
                    self._spike_gui_expected_step = 4
                    if self.mode3_raw_data_process(payload, packet_counter) is False:
                        continue

                    pass

                elif(packets_type == 9): # impedance progress
                    self._emit_impedance_progress(packets.split(' '))
                    pass

                elif(packets_type == 10): # impedance result
                    self._emit_impedance_result(packets.split(' '))
                    pass

                    """ for timestamp alignment """
                elif(packets_type == 4): # empty payload
                    self._record_stream_mode_packet(STREAM_MODE_IDLE)
                    # battery
                    idle_message = np.array(packets.split(' ')) # 取所有个 short
                    battery_a =  int(swap16Hex(idle_message[2]) ,16)
                    battery_b = int(swap16Hex(idle_message[3]) ,16)
                    battery_c = int(swap16Hex(idle_message[4]) ,16)
                    self.power_guard = _parse_empty_power_guard_words(idle_message)
                    self.last_battery_triplet = (float(battery_a), float(battery_b), float(battery_c))
                    self.EmptyGUIUpdate.emit([battery_a, battery_b, battery_c, dict(self.power_guard)])
                    self._emit_status_update(force=True)
                    pass

                elif(packets_type == 3): # timestamp payload
                    timestamp_message = np.array(packets.split(' ')) # 取所有个 short
                    _ = int(swap16Hex(timestamp_message[5][0:2]) ,16)
                    _ = int(swap16Hex(timestamp_message[5][2:4]) ,16)
                    pass
            SerialPort._record_read_batch_packet_counts(self, read_batch_packet_counts)
    
    def data_file_saving_control(self):
        # mode 0 
         ###### video recording ######
        if(self._should_save_mode(0)):
            self.save_lfp_file(self.lfp_file_addr)
        elif(self.LFPRawCounter > 0):
            self.save_lfp_file(self.lfp_file_addr, manual_save=True)
        # mode 3
        if(self._should_save_mode(3)):
            self.save_spike_mode3_file(self.mode3_file_addr)
        elif(self.Mode3RawCounter > 0):
            self.save_spike_mode3_file(self.mode3_file_addr, manual_save=True)
        
        # mode 1
        if(self._should_save_mode(1)):
            self.save_spike_mode1_file(self.mode1_file_addr)
        elif(self.SPIKERawCounter > 0):
            self.save_spike_mode1_file(self.mode1_file_addr, manual_save=True)
        # mode 2
        if(self._should_save_mode(2)):
            self.save_spike_mode2_file(self.mode2_file_addr)
        elif(self.Mode2RawCounter > 0):
            self.save_spike_mode2_file(self.mode2_file_addr, manual_save=True)
        pass
##############################################################
############################################################## Data processing
##############################################################
##############################################################

    def trigger_alignment(self, trial_num=1.0):
        """Trigger alignment signal (6ms duration)"""
        # Mode 3 Raw is 12.5kHz. 6ms = 0.006 * 12500 = 75 samples.
        self.alignment_counter = 75
        self.current_alignment_value = float(trial_num)
        self._alignment_counters_by_stream = {
            "raw_12500": 75,
            "mode2_v2": max(1, int(round(MODE2_ALIGNMENT_PULSE_SECONDS * float(MODE2_V2_FS)))),
        }

    def _consume_raw_alignment_values(self, num_samples):
        if isinstance(getattr(self, "_alignment_counters_by_stream", None), dict):
            return self._consume_alignment_values(num_samples, "raw_12500")
        return self._consume_alignment_values_legacy(num_samples)

    def _consume_alignment_values_legacy(self, num_samples):
        num_samples = max(0, int(num_samples or 0))
        counter = max(0, int(getattr(self, "alignment_counter", 0) or 0))
        ones_count = min(counter, num_samples)
        zeros_count = num_samples - ones_count
        align_val = float(getattr(self, "current_alignment_value", 0.0) or 0.0)
        if align_val > 65535:
            align_val = align_val % 65536
        if counter > 0:
            self.alignment_counter = max(0, counter - ones_count)
        return [align_val] * ones_count + [0.0] * zeros_count

    def _consume_alignment_values(self, num_samples, stream_key):
        num_samples = max(0, int(num_samples or 0))
        counters = getattr(self, "_alignment_counters_by_stream", None)
        if not isinstance(counters, dict):
            return self._consume_alignment_values_legacy(num_samples)
        stream_key = str(stream_key or "raw_12500")
        if stream_key not in counters:
            if stream_key == "raw_12500":
                counters[stream_key] = max(0, int(getattr(self, "alignment_counter", 0) or 0))
            else:
                counters[stream_key] = 0
        counter = max(0, int(counters.get(stream_key, 0) or 0))
        ones_count = min(counter, num_samples)
        zeros_count = num_samples - ones_count
        align_val = float(getattr(self, "current_alignment_value", 0.0) or 0.0)
        if align_val > 65535:
            align_val = align_val % 65536
        counters[stream_key] = max(0, counter - ones_count)
        if stream_key == "raw_12500":
            self.alignment_counter = counters[stream_key]
        return [align_val] * ones_count + [0.0] * zeros_count

    def _decode_mode3_sensor_values(self, mode_3_data_buffer, capture_detail=False, save_packet_sensors=None):
        temp_sensor_data = mode_3_data_buffer[self.sensor_index]
        # Reuse pre-allocated buffer.
        temp_sensor_data_float = self.temp_sensor_data_buffer

        # IMU data
        temp_sensor_data_float[0:3] = list(map(lambda x:self.LSM6DS3_accelData_in_g(self.DAC(swap16Hex(x), two_complement=True)), temp_sensor_data[0:3]))
        temp_sensor_data_float[3:6] = list(map(lambda x:self.LSM6DS3_gyroData_in_dps(self.DAC(swap16Hex(x), two_complement=True)), temp_sensor_data[3:6]))
        # LC data
        temp_sensor_data_float[6:] = list(map(lambda x:self.DAC(swap16Hex(x)), temp_sensor_data[6:]))
        for i in range(9):
            sensor_value = float(temp_sensor_data_float[i])
            if capture_detail:
                self.sensordata_GUI[i].append(sensor_value)
            if save_packet_sensors is not None:
                save_packet_sensors[i] = [sensor_value]
        self.last_battery_triplet = (
            float(temp_sensor_data_float[6]),
            float(temp_sensor_data_float[7]),
            float(temp_sensor_data_float[8]),
        )
        return temp_sensor_data_float

    def mode_3_packets_process(self, mode_3_data_buffer, packet_counter):
        capture_detail = self._should_capture_detail_gui_buffers()
        should_save = self._should_save_mode(3)
        if not self._should_accept_packet_index_sample("mode3_lfp_esa", packet_counter, expected_step=1):
            return False
        # split only after accepting the packet; stale/backward packets should be cheap to drop.
        mode_3_data_buffer = np.array(mode_3_data_buffer.split())
        if not should_save and not capture_detail:
            # Keep low-rate battery/status data, but skip waveform and raster decode.
            self._decode_mode3_sensor_values(mode_3_data_buffer, capture_detail=False, save_packet_sensors=None)
            return True
        if should_save and not capture_detail:
            try:
                decoded_pending_packets = int(getattr(self, "_mode3_pending_packets", 0) or 0)
            except Exception:
                decoded_pending_packets = 0
            if decoded_pending_packets > 0:
                self._save_mode3_flush_pending()
            save_packet_sensors = [[] for _ in range(9)]
            self._decode_mode3_sensor_values(
                mode_3_data_buffer,
                capture_detail=False,
                save_packet_sensors=save_packet_sensors,
            )
            self._save_mode3_encoded_append(
                self.Timestamp_neural_signal,
                packet_counter,
                mode_3_data_buffer,
                save_packet_sensors,
                self.sensor_update_flag,
            )
            return True
        # 1. raw data
        save_lfp_channels = None
        save_esa_channels = None
        save_raster_channels = None
        save_packet_sensors = None
        if should_save:
            try:
                encoded_pending_packets = int(getattr(self, "_mode3_encoded_pending_packets", 0) or 0)
            except Exception:
                encoded_pending_packets = 0
            if encoded_pending_packets > 0:
                self._save_mode3_encoded_flush_pending()
            save_lfp_channels = [[] for _ in range(16)]
            save_esa_channels = [[] for _ in range(16)]
            save_raster_channels = [[] for _ in range(16)]
            save_packet_sensors = [[] for _ in range(9)]
        decoded_lfp_channels, decoded_esa_channels, decoded_raster_channels = _decode_mode3_lfp_esa_packet_words(
            mode_3_data_buffer,
            raw_data_per_packet_mode3=self.raw_data_per_packet_mode3,
            dac_resolution=self.DAC_resolution,
        )
        for channel_num in range(16):
            if capture_detail:
                self.lfpdata_GUI[channel_num].extend(decoded_lfp_channels[channel_num])
                self.ESAdata_GUI[channel_num].extend(decoded_esa_channels[channel_num])
            if save_lfp_channels is not None and save_esa_channels is not None:
                save_lfp_channels[channel_num] = decoded_lfp_channels[channel_num]
                save_esa_channels[channel_num] = decoded_esa_channels[channel_num]
        
        if capture_detail:
            self.lfptimestamp_GUI.append(self.Timestamp_neural_signal)
            self.lfppacketindex_GUI.append(int(packet_counter) & 0xFFFF)
          # 3. sensor data
        self._decode_mode3_sensor_values(mode_3_data_buffer, capture_detail=capture_detail, save_packet_sensors=save_packet_sensors)

        temp_raster_data = mode_3_data_buffer[-3:]
        packet_raster_bins = [[0, 0, 0] for _ in range(16)]
        for raster_time, spike_data in enumerate(temp_raster_data):
            spike_data = format(self.DAC(swap16Hex(spike_data)) ,'#018b')[2:] # 保留 前16位
            for channel_num ,spike_num in enumerate(spike_data):
                spike_num_int = int(spike_num)
                mapped_channel = 15 - channel_num
                if capture_detail:
                    self.spikerasterdata_GUI[mapped_channel].append(spike_num_int)
                packet_raster_bins[mapped_channel][raster_time] = spike_num_int
                 # for file saving
                pass
        if save_raster_channels is not None:
            save_raster_channels = decoded_raster_channels

        if should_save and save_lfp_channels is not None and save_esa_channels is not None and save_raster_channels is not None and save_packet_sensors is not None:
            self._save_mode3_append(self.Timestamp_neural_signal, packet_counter, save_lfp_channels, save_esa_channels, save_raster_channels, save_packet_sensors, self.sensor_update_flag)
        return True

    def mode3_raw_data_process(self, spike_data_buffer, packet_counter):
        capture_detail = self._should_capture_detail_gui_buffers()
        should_save = self._should_save_mode(3)
        if not self._should_accept_packet_index_sample("mode3_raw", packet_counter, expected_step=4):
            return False
        spike_data_text = str(spike_data_buffer or "")
        if not capture_detail and not should_save:
            stripped_buffer = spike_data_text.strip()
            if stripped_buffer:
                num_samples = spike_data_text.count(" ")
                if not spike_data_text.endswith(" "):
                    num_samples += 1
            else:
                num_samples = 0
            if num_samples > 0:
                self._consume_raw_alignment_values(num_samples)
            return True
        if should_save and not capture_detail:
            try:
                raw_decoded_pending_packets = int(getattr(self, "_mode3_raw_pending_packets", 0) or 0)
            except Exception:
                raw_decoded_pending_packets = 0
            if raw_decoded_pending_packets > 0:
                self._save_mode3_raw_flush_pending()
            raw_words = spike_data_text.split()
            num_samples = len(raw_words)
            current_alignment_vals = self._consume_raw_alignment_values(num_samples)
            self._save_mode3_raw_encoded_append(
                self.Timestamp_neural_signal,
                packet_counter,
                raw_words,
                self.spike_channel_index_mode1_3,
                current_alignment_vals,
            )
            return True
        # spilt
        spike_data_buffer = np.array(spike_data_text.split())
        # 1. raw data + timestamp
        temp_channel = spike_data_buffer 

        temp_channel_DAC = list(map(lambda x:self.DAC(swap16Hex(x), raw=False), temp_channel)) #注意： mode3 中所有的原始数据在下位机上就已经做了前后8字节的对调,所以这里需要再对调一次
        # 2. give to GUI buffer
        try:
            raw_encoded_pending_packets = int(getattr(self, "_mode3_raw_encoded_pending_packets", 0) or 0)
        except Exception:
            raw_encoded_pending_packets = 0
        if should_save and raw_encoded_pending_packets > 0:
            self._save_mode3_raw_encoded_flush_pending()
        if capture_detail:
            self.spikedata_GUI.extend(temp_channel_DAC)
            self.spiketimestamp_GUI.append(self.Timestamp_neural_signal)
            self.spikepacketindex_GUI.append(int(packet_counter) & 0xFFFF)
        
        # alignment signal
        num_samples = len(temp_channel_DAC)
        current_alignment_vals = self._consume_raw_alignment_values(num_samples)
        
        if capture_detail:
            self.alignment_GUI.extend(current_alignment_vals)
        
        # 3. save to file
        if should_save:
            self._save_mode3_raw_append(self.Timestamp_neural_signal, packet_counter, temp_channel_DAC, self.spike_channel_index_mode1_3, current_alignment_vals)
        return True
         

    def mode2_v2_raw_packet_process(
        self,
        timestamp,
        packet_counter,
        packet_words,
        payload_bytes=None,
        samples_uv=None,
        accel_raw=None,
        accel_timestamp=None,
    ):
        SerialPort._set_mode2_stream_format(self, MODE2_V2_PACKET_SAMPLES, MODE2_V2_FS)
        self.spike_raw_channel = list(range(MODE2_V2_CHANNELS))
        self.mode2_selected_channels_gui = list(range(MODE2_V2_CHANNELS))
        packet_snapshot = SerialPort._record_packet_index_sample(
            self,
            "mode2_raw",
            packet_counter,
            expected_step=1,
        )
        try:
            received_packet = int(packet_snapshot.get("batch_received", 1) or 0) > 0
        except Exception:
            received_packet = True
        if not received_packet:
            return False
        try:
            missing_packets = max(0, int(packet_snapshot.get("batch_missing", 0) or 0))
        except Exception:
            missing_packets = 0

        capture_detail = self._should_capture_detail_gui_buffers()
        should_save = self._should_save_mode(2)
        if capture_detail and not hasattr(self, "alignment_mode2_GUI"):
            self.alignment_mode2_GUI = []
        alignment_timeline = self._consume_alignment_values(
            (missing_packets + 1) * MODE2_V2_PACKET_SAMPLES,
            "mode2_v2",
        )
        missing_alignment = alignment_timeline[:missing_packets * MODE2_V2_PACKET_SAMPLES]
        current_alignment_vals = alignment_timeline[missing_packets * MODE2_V2_PACKET_SAMPLES:]
        has_accel = accel_raw is not None
        if not capture_detail and not should_save and not has_accel:
            return True

        if samples_uv is None:
            if payload_bytes is not None:
                samples_uv = _decode_mode2_v2_payload_bytes(payload_bytes)
            else:
                samples_uv = _decode_mode2_v2_payload(packet_words)

        if capture_detail:
            if missing_packets > 0:
                filler_len = missing_packets * MODE2_V2_PACKET_SAMPLES
                filler = [MODE2_V2_GUI_MISSING_SAMPLE] * filler_len
                for channel_num in range(MODE2_V2_CHANNELS):
                    self.spikedata_mode2_GUI[channel_num].extend(filler)
                self.alignment_mode2_GUI.extend(missing_alignment)
            for channel_num in range(MODE2_V2_CHANNELS):
                self.spikedata_mode2_GUI[channel_num].extend(samples_uv[:, channel_num].tolist())
            self.alignment_mode2_GUI.extend(current_alignment_vals)
            self.spiketimestamp_mode2_GUI.append(timestamp)
            self.spikepacketindex_mode2_GUI.append(int(packet_counter) & 0xFFFF)
        if has_accel:
            accel_values = [
                float(self.LSM6DS3_accelData_in_g(int(value)))
                for value in list(accel_raw)[:3]
            ]
            if len(accel_values) == 3:
                if capture_detail:
                    for i, sensor_value in enumerate(accel_values):
                        self.sensordata_GUI[i].append(sensor_value)
                if should_save:
                    if not hasattr(self, "mode2_sensors_data"):
                        self.mode2_sensors_data = get_events_data_container()
                    if not hasattr(self, "mode2_sensor_timestamps"):
                        self.mode2_sensor_timestamps = []
                    for sensor_key, sensor_value in zip(MODE2_SENSOR_KEYS, accel_values):
                        self.mode2_sensors_data.setdefault(sensor_key, []).append(sensor_value)
                    self.mode2_sensor_timestamps.append(float(accel_timestamp if accel_timestamp is not None else timestamp))

        if should_save:
            if missing_packets > 0 and missing_alignment:
                prev_pos = len(self.AP_LFP_data.get("PacketIndex", [])) - 1
                if prev_pos >= 0:
                    self.AP_LFP_data.setdefault("Raw_alignment_gap_fills", []).append(
                        (int(prev_pos), list(missing_alignment))
                    )
            for channel_num in range(MODE2_V2_CHANNELS):
                self.AP_LFP_data["Channel_{}".format(channel_num)].extend(samples_uv[:, channel_num].tolist())
            self.AP_LFP_data.setdefault("Raw_alignment", []).extend(current_alignment_vals)
            self.AP_LFP_data.setdefault("SensorMask", []).append(1 if has_accel else 0)
            self.AP_LFP_data["TimeStamp"].append(timestamp)
            self.AP_LFP_data["PacketIndex"].append(int(packet_counter) & 0xFFFF)

        return True

    def lfp_packets_process(self, lfp_data_buffer, packet_counter, raw_channel=0, has_accel=False, has_status=False):
        # spilt
        lfp_data_buffer = np.array(lfp_data_buffer.split())
        raw_channel = max(0, min(15, int(raw_channel or 0)))
        # 1. Packed neural data
        mode0_quant_bits = _normalize_mode0_quant_bits(
            getattr(self, "mode0_quant_bits", MODE0_QUANT_BITS_DEFAULT)
        )
        mode0_quant_full_scale_uv = _normalize_mode0_quant_full_scale_uv(
            getattr(self, "mode0_quant_full_scale_uv", MODE0_QUANT_FULL_SCALE_UV_DEFAULT)
        )
        capture_detail = self._should_capture_detail_gui_buffers()
        should_save = self._should_save_mode(0)
        save_packet_channels = None
        save_packet_mand = None
        save_packet_raw = None
        save_packet_sensors = None
        if should_save:
            save_packet_channels = [[] for _ in range(16)]
            save_packet_mand = [[] for _ in range(16)]
            save_packet_raw = []
            save_packet_sensors = [[] for _ in MODE0_SENSOR_KEYS]
        neural_start = 0
        neural_end = neural_start + _mode0_neural_packed_words_for_bits(mode0_quant_bits)
        if len(lfp_data_buffer) < neural_end:
            return False
        if not self._should_accept_packet_index_sample("mode0_lfp", packet_counter, expected_step=1):
            return False
        try:
            last_batch = getattr(self, "_packet_loss_last_batch", {})
            if not isinstance(last_batch, dict):
                last_batch = {}
            missing_packets = max(0, int(dict(last_batch.get("mode0_lfp", {}) or {}).get("missing", 0) or 0))
        except Exception:
            missing_packets = 0
        lfp_channels, mand_channels, raw_samples = _decode_mode0_quantized_neural_words(
            lfp_data_buffer[neural_start:neural_end],
            bit_depth=mode0_quant_bits,
            full_scale_uv=mode0_quant_full_scale_uv,
        )
        if capture_detail and not hasattr(self, "ESAdata_GUI"):
            self.ESAdata_GUI = [[] for _ in range(16)]
        for channel_num in range(16):
            temp_channel_DAC = lfp_channels[channel_num]
            if capture_detail:
                self.lfpdata_GUI[channel_num].extend(temp_channel_DAC)
                mand_values = mand_channels[channel_num]
                if mand_values:
                    repeat_count = max(1, len(temp_channel_DAC) // max(1, len(mand_values)))
                    mand_display = []
                    for value in mand_values:
                        mand_display.extend([float(value)] * repeat_count)
                    if len(mand_display) < len(temp_channel_DAC):
                        mand_display.extend([float(mand_values[-1])] * (len(temp_channel_DAC) - len(mand_display)))
                    self.ESAdata_GUI[channel_num].extend(mand_display[:len(temp_channel_DAC)])
            if save_packet_channels is not None:
                save_packet_channels[channel_num] = temp_channel_DAC
            if save_packet_mand is not None:
                save_packet_mand[channel_num] = mand_channels[channel_num]
        if capture_detail:
            if not hasattr(self, "mode0rawdata_GUI"):
                self.mode0rawdata_GUI = []
            if not hasattr(self, "mode0rawchannel_GUI"):
                self.mode0rawchannel_GUI = []
            if missing_packets > 0:
                self.mode0rawdata_GUI.extend([float("nan")] * (missing_packets * MODE0_RAW_POINTS_PER_PACKET))
            self.mode0rawdata_GUI.extend(raw_samples)
            self.mode0rawchannel_GUI.append(raw_channel)
        alignment_timeline = self._consume_raw_alignment_values(
            missing_packets * MODE0_RAW_POINTS_PER_PACKET + len(raw_samples)
        )
        missing_alignment = alignment_timeline[:missing_packets * MODE0_RAW_POINTS_PER_PACKET]
        raw_alignment = alignment_timeline[missing_packets * MODE0_RAW_POINTS_PER_PACKET:]
        if save_packet_raw is not None:
            save_packet_raw = list(raw_samples)
        
        # 2. timestamp
        if capture_detail:
            self.lfptimestamp_GUI.append(self.Timestamp_neural_signal)
            self.lfppacketindex_GUI.append(int(packet_counter) & 0xFFFF)

        # 3. Optional variable-length telemetry.
        cursor = neural_end
        if has_accel:
            accel_end = cursor + MODE0_ACCEL_WORDS
            if len(lfp_data_buffer) < accel_end:
                return False
            accel_words = lfp_data_buffer[cursor:accel_end]
            cursor = accel_end
            accel_values = [
                float(self.LSM6DS3_accelData_in_g(self.DAC(swap16Hex(word), two_complement=True)))
                for word in accel_words
            ]
            for i, sensor_value in enumerate(accel_values[:3]):
                if capture_detail and i < len(self.sensordata_GUI):
                    self.sensordata_GUI[i].append(sensor_value)
                if save_packet_sensors is not None and i < len(save_packet_sensors):
                    save_packet_sensors[i] = [sensor_value]

        if has_status:
            status_end = cursor + MODE0_STATUS_WORDS
            if len(lfp_data_buffer) < status_end:
                return False
            battery_words = lfp_data_buffer[cursor:cursor + 3]
            compact_power_words = lfp_data_buffer[cursor + 3:status_end]
            cursor = status_end
            battery_values = [
                float(self.DAC(swap16Hex(word)))
                for word in battery_words
            ]
            for i, sensor_value in enumerate(battery_values[:3]):
                gui_index = 6 + i
                save_index = 3 + i
                if capture_detail and gui_index < len(self.sensordata_GUI):
                    self.sensordata_GUI[gui_index].append(sensor_value)
                if save_packet_sensors is not None and save_index < len(save_packet_sensors):
                    save_packet_sensors[save_index] = [sensor_value]
            self.last_battery_triplet = (
                float(battery_values[0]),
                float(battery_values[1]),
                float(battery_values[2]),
            )
            self._update_mode0_power_telemetry(compact_power_words)

        if should_save and save_packet_channels is not None and save_packet_mand is not None and save_packet_raw is not None and save_packet_sensors is not None:
            sensor_mask = (0x01 if has_accel else 0x00) | (0x02 if has_status else 0x00)
            pending_packets_before_append = max(0, int(getattr(self, "_mode0_pending_packets", 0) or 0))
            raw_alignment_gap_fill = None
            if missing_packets > 0 and missing_alignment:
                raw_alignment_gap_fill = (pending_packets_before_append - 1, list(missing_alignment))
            self._save_mode0_append(
                self.Timestamp_neural_signal,
                packet_counter,
                save_packet_channels,
                save_packet_mand,
                save_packet_raw,
                raw_channel,
                raw_alignment,
                save_packet_sensors,
                self.sensor_update_flag,
                quant_bits=mode0_quant_bits,
                quant_full_scale_uv=mode0_quant_full_scale_uv,
                raw_alignment_gap_fill=raw_alignment_gap_fill,
                sensor_mask=sensor_mask,
            )
        return True

    def spike_packets_process(self, spike_data_buffer, packet_counter):
        # spilt
        spike_data_buffer = np.array(spike_data_buffer.split(' '))[0:-1]
        # 1. raw data + timestamp
        temp_channel = spike_data_buffer[9:-5] 
        temp_channel_DAC = list(map(lambda x:self.DAC(x, raw=False), temp_channel))
        # 2. give to GUI buffer
        capture_raw_gui = self._should_capture_mode1_raw_gui_buffer()
        capture_detail = self._should_capture_detail_gui_buffers()
        should_save = self._should_save_mode(1)
        if not self._should_accept_packet_index_sample("mode1_raw", packet_counter, expected_step=1):
            return False
        if capture_raw_gui:
            self.spikedata_GUI.extend(temp_channel_DAC)
            self.spiketimestamp_GUI.append(self.Timestamp_neural_signal)
            self.spikepacketindex_GUI.append(int(packet_counter) & 0xFFFF)
        # 3. save to file
        if should_save:
            self.AP_data["Raw_data"].extend(temp_channel_DAC)
            self.AP_data["Raw_timestamp"].append(self.Timestamp_neural_signal)
            self.AP_data["PacketIndex"].append(packet_counter)
            self.AP_data["Raw_channel"].append(self.spike_channel_index_mode1_3) 
            self.sensors_data["UpdateFlag"].append(self.sensor_update_flag)
            # Log
            if(len(temp_channel_DAC) != 90):
                print("spike data length error", len(temp_channel_DAC))
            
         # 3. sensor data
        temp_sensor_data = spike_data_buffer[self.sensor_index]
        # Reuse pre-allocated buffer
        temp_sensor_data_float = self.temp_sensor_data_buffer
        
        # IMU data
        temp_sensor_data_float[0:3] = list(map(lambda x:self.LSM6DS3_accelData_in_g(self.DAC(swap16Hex(x), two_complement=True)), temp_sensor_data[0:3]))
        temp_sensor_data_float[3:6] = list(map(lambda x:self.LSM6DS3_gyroData_in_dps(self.DAC(swap16Hex(x), two_complement=True)), temp_sensor_data[3:6]))
        # LC data
        temp_sensor_data_float[6:] = list(map(lambda x:self.DAC(swap16Hex(x)), temp_sensor_data[6:]))
        for i in range(9):
            sensor_value = float(temp_sensor_data_float[i])
            if capture_detail:
                self.sensordata_GUI[i].append(sensor_value)
            # save to file
            if should_save:
                self.sensors_data[self.sensor_name[i]].append(sensor_value)
        self.last_battery_triplet = (
            float(temp_sensor_data_float[6]),
            float(temp_sensor_data_float[7]),
            float(temp_sensor_data_float[8]),
        )
        
        # 5. raster data
        temp_raster_data = spike_data_buffer[-5:]
        for raster_time, spike_data in enumerate(temp_raster_data):
            spike_data = format(self.DAC(swap16Hex(spike_data)) ,'#018b')[2:] # 保留 前16位
            for channel_num ,spike_num in enumerate(spike_data):
                fw_channel = 15 - channel_num
                gui_channel = fw_channel
                if capture_detail:
                    self.spikerasterdata_GUI[gui_channel].append(int(spike_num))
                # for file saving
                if int(spike_num) and should_save:
                    self.AP_data["AP_timestamp"].append(raster_time * 0.864 + self.Timestamp_neural_signal) # 一个 raster bin 大概为0.864ms
                    self.AP_data["Electrode"].append(fw_channel)
        return True


##############################################################
############################################################## Data Saving
##############################################################
##############################################################
    def save_lfp_file(self, addr, manual_save=False):
        """ save data to a file"""
        curr_bucket = self.LFPRawCounter // 5000
        if(curr_bucket != self._lfp_progress_bucket):
            # print file collection progress once per 500-count bucket
            # print("Mode0 file preparing", round(self.LFPRawCounter/self.file_size_lfp * 100, 2) , "%", "have run ", round(self.Timestamp_recorder_counter / 1000 / 60, 2), "min")
            progress_percent = round(self.LFPRawCounter/self.file_size_lfp * 100, 2)
            run_time_min = round(self.Timestamp_recorder_counter / 1000 / 60, 2)
            self._emit_save_progress(0, progress_percent, run_time_min)
            self._lfp_progress_bucket = curr_bucket

        if(self.LFPRawCounter >= self.file_size_lfp or manual_save): 
            self.LFPRawCounter = 0
            self._lfp_progress_bucket = -1
            run_time_min = round(self.Timestamp_recorder_counter / 1000 / 60, 2)
            self._emit_save_progress(0, 0.0, run_time_min, force=True) # Reset progress
            end_timestamp = float(time.time() * 1000)
            self._save_mode0_flush_pending()
            self._save_put({
                "type": "flush",
                "mode": 0,
                "addr": addr,
                "end_timestamp": end_timestamp,
                "params": {
                    "LFP_max_interval": self.LFP_max_interval,
                    "raw_data_per_packet_channel": self.raw_data_per_packet_channel,
                    "sensor_name": list(MODE0_SENSOR_KEYS),
                    "sensor_fs": list(MODE0_SENSOR_SAMPLE_RATES),
                },
            })

            # 3.5 camera
            self.CameraGUIUpdate.emit([]) 
            if not manual_save:
                SerialPort._reset_packet_loss_streams(self, streams=("mode0_lfp",))
            self._emit_status_update(force=True)
            # 4. resample detection
            if(sum(self.overflowSignal) == 1): #  重新开始sample
                self.sample_times += 1
                self.overflowSignal[0] = self.overflowSignal[1]
                # print("sample begin!")

    def save_spike_mode3_file(self, addr, manual_save=False):
        """ save data to a file"""
        curr_bucket = self.Mode3RawCounter // 500
        if(curr_bucket != self._mode3_progress_bucket):
            # print file collection progress once per 500-count bucket
            # print("Mode3 file preparing", round(self.Mode3RawCounter/self.file_size_mode3 * 100, 2) , "%", "have run ", round(self.Timestamp_recorder_counter / 1000 / 60, 2), "min")
            progress_percent = round(self.Mode3RawCounter/self.file_size_mode3 * 100, 2)
            run_time_min = round(self.Timestamp_recorder_counter / 1000 / 60, 2)
            self._emit_save_progress(3, progress_percent, run_time_min)
            self._mode3_progress_bucket = curr_bucket

        if(self.Mode3RawCounter >= self.file_size_mode3 or manual_save): 
            self.Mode3RawCounter = 0
            self._mode3_progress_bucket = -1
            run_time_min = round(self.Timestamp_recorder_counter / 1000 / 60, 2)
            self._emit_save_progress(3, 0.0, run_time_min, force=True) # Reset progress
            end_timestamp = float(time.time() * 1000)
            self._save_mode3_flush_pending()
            self._save_mode3_encoded_flush_pending()
            self._save_mode3_raw_flush_pending()
            self._save_mode3_raw_encoded_flush_pending()
            self._save_put({
                "type": "flush",
                "mode": 3,
                "addr": addr,
                "end_timestamp": end_timestamp,
                "params": {
                    "mode3_max_interval": self.mode3_max_interval,
                    "raw_data_per_packet_mode3": self.raw_data_per_packet_mode3,
                    "sensor_name": self.sensor_name,
                    "sensor_fs": int(1000/2), #self.sensor_fs = 1000 / 2
                    "mode3_raw_max_interval": self.mode3_raw_max_interval,
                    "mode3_thresholds": list(self.mode3_thresholds_uv),
                },
            })

            # 3.5 camera
            self.CameraGUIUpdate.emit([]) 
            if not manual_save:
                SerialPort._reset_packet_loss_streams(self, streams=("mode3_lfp_esa", "mode3_raw"))
            self._emit_status_update(force=True)

            # 4. resample detection
            if(sum(self.overflowSignal) == 1): #  重新开始sample
                self.sample_times += 1
                self.overflowSignal[0] = self.overflowSignal[1]
                # print("sample begin!")
           
    def save_spike_mode1_file(self, addr, manual_save=False):
        curr_bucket = self.SPIKERawCounter // 500
        if(curr_bucket != self._mode1_progress_bucket):
            # print("file preparing",  round(self.SPIKERawCounter/self.file_size_mode1 * 100, 2) , "%" ,"have run ", round(self.Timestamp_recorder_counter / 1000 / 60, 2), "min")
            progress_percent = round(self.SPIKERawCounter/self.file_size_mode1 * 100, 2)
            run_time_min = round(self.Timestamp_recorder_counter / 1000 / 60, 2)
            self._emit_save_progress(1, progress_percent, run_time_min)
            self._mode1_progress_bucket = curr_bucket
        
        """ save data to a file"""
        if(self.SPIKERawCounter == self.file_size_mode1 or manual_save):
            self.SPIKERawCounter = 0
            self._mode1_progress_bucket = -1
            run_time_min = round(self.Timestamp_recorder_counter / 1000 / 60, 2)
            self._emit_save_progress(1, 0.0, run_time_min, force=True) # Reset progress
            # 1. detect if there exist one or mutiple miss packets when save raw data to a structured file
            packet_indices = self.AP_data.get("PacketIndex", [])
            if packet_indices is not None and len(packet_indices) > 1:
                miss_packets, miss_packets_num = _calc_miss_packets_from_indices(packet_indices, expected_step=1)
                self.AP_data["MissPacketsIndex"] = np.asarray(miss_packets, dtype=np.int64)
                self.AP_data["MissPackets"] = float(miss_packets_num)
            else:
                self.AP_data["MissPacketsIndex"] = np.asarray([], dtype=np.int64)
                self.AP_data["MissPackets"] = 0.0
                logging.critical(f"Mode1 PacketIndex unavailable when saving: size={0 if packet_indices is None else len(packet_indices)}")
            # 2. save the file (Offload to process)
            end_timestamp = float(time.time() * 1000)
            
            # Calculate start_time from duration
            timestamps = self.AP_data.get("Raw_timestamp", [])
            if len(timestamps) > 0:
                # 4.32ms per packet for Mode 1 (20833Hz / 90 samples per packet)
                duration_ms = timestamps[-1] - timestamps[0] + 4.32 
            else:
                duration_ms = 0
            
            start_timestamp_ms = end_timestamp - duration_ms
            start_time = datetime.datetime.fromtimestamp(start_timestamp_ms / 1000.0)
            
            task = {
                'mode': 1,
                'raw_data': self.AP_data,
                'sensors_data': self.sensors_data,
                'addr': addr,
                'start_time': start_time,
                'end_timestamp': end_timestamp,
                'params': {
                    'Spike_max_interval': self.Spike_max_interval,
                    'sensor_name': self.sensor_name,
                    'sensor_fs': int(20833 / 90), #self.sensor_fs = 20833 / 90
                }
            }
            self._save_put(task)
            if not manual_save:
                SerialPort._reset_packet_loss_streams(self, streams=("mode1_raw",))
            self._emit_status_update(force=True)
            
            # 3. reinit the temp array
            self.AP_data = spike_data_container()
            self.sensors_data = get_events_data_container()
            # 4. resample detection
            if(sum(self.overflowSignal) == 1): #  重新开始sample
                self.sample_times += 1
                self.overflowSignal[0] = self.overflowSignal[1]
                # print("sample begin!")
        pass

    def save_spike_mode2_file(self, addr, manual_save=False):
        curr_bucket = self.Mode2RawCounter // 500
        if(curr_bucket != self._mode2_progress_bucket):
            # print("file preparing", round(self.Mode2RawCounter/self.file_size_mode2 * 100, 2) , "%")
            # Note: mode2 didn't print run time originally, but we can send it anyway or send -1 if not available/relevant
            progress_percent = round(self.Mode2RawCounter/self.file_size_mode2 * 100, 2)
            run_time_min = round(self.Timestamp_recorder_counter / 1000 / 60, 2)
            self._emit_save_progress(2, progress_percent, run_time_min)
            self._mode2_progress_bucket = curr_bucket
        """ save data to a file"""
        if(self.Mode2RawCounter == self.file_size_mode2 or manual_save):
            self.Mode2RawCounter = 0
            self._mode2_progress_bucket = -1
            run_time_min = round(self.Timestamp_recorder_counter / 1000 / 60, 2)
            self._emit_save_progress(2, 0.0, run_time_min, force=True) # Reset progress
            # 1. detect if there exist one or mutiple miss packets when save raw data to a structured file  
            packet_indices = self.AP_LFP_data.get("PacketIndex", [])
            if packet_indices is not None and len(packet_indices) > 1:
                miss_packets, miss_packets_num = _calc_miss_packets_from_indices(packet_indices, expected_step=1)
                self.AP_LFP_data["MissPacketsIndex"] = np.asarray(miss_packets, dtype=np.int64)
                self.AP_LFP_data["MissPackets"] = float(miss_packets_num)
            else:
                self.AP_LFP_data["MissPacketsIndex"] = np.asarray([], dtype=np.int64)
                self.AP_LFP_data["MissPackets"] = 0.0
                logging.critical(f"Mode2 PacketIndex unavailable when saving: size={0 if packet_indices is None else len(packet_indices)}")

            # 2. save the file (Offload to process)
            end_timestamp = float(time.time() * 1000)
            
            # Calculate start_time from duration
            timestamps = self.AP_LFP_data.get("TimeStamp", [])
            mode2_packet_samples = max(1, int(getattr(self, "mode2_packet_samples", MODE2_V2_PACKET_SAMPLES) or MODE2_V2_PACKET_SAMPLES))
            mode2_sample_frequency = max(1.0, float(getattr(self, "mode2_sample_frequency", MODE2_V2_FS) or MODE2_V2_FS))
            mode2_tail_ms = 1000.0 * mode2_packet_samples / mode2_sample_frequency
            if len(timestamps) > 0:
                duration_ms = timestamps[-1] - timestamps[0] + mode2_tail_ms
            else:
                duration_ms = 0
            
            start_timestamp_ms = end_timestamp - duration_ms
            start_time = datetime.datetime.fromtimestamp(start_timestamp_ms / 1000.0)
            
            task = {
                'mode': 2,
                'raw_data': self.AP_LFP_data,
                'sensors_data': getattr(self, "mode2_sensors_data", get_events_data_container()),
                'sensor_timestamps': list(getattr(self, "mode2_sensor_timestamps", []) or []),
                'addr': addr,
                'start_time': start_time,
                'end_timestamp': end_timestamp,
                'params': {
                    'mode2_max_interval': self.mode2_max_interval,
                    'mode2_packet_samples': mode2_packet_samples,
                    'mode2_sample_frequency': mode2_sample_frequency,
                    'sensor_name': list(MODE2_SENSOR_KEYS),
                    'sensor_fs': list(MODE2_SENSOR_SAMPLE_RATES),
                }
            }
            self._save_put(task)
            if not manual_save:
                SerialPort._reset_packet_loss_streams(self, streams=("mode2_raw",))
            self._emit_status_update(force=True)
            
            # 3. reinit the temp array
            self.AP_LFP_data = get_raw_data_container()
            self.mode2_sensors_data = get_events_data_container()
            self.mode2_sensor_timestamps = []
            
            # 4. resample detection 
            if(sum(self.overflowSignal) == 1): #  重新开始sample
                self.sample_times += 1
                self.overflowSignal[0] = self.overflowSignal[1]
                # print("sample begin!")
        pass


##############################################################
############################################################## GUI Data real update
##############################################################
##############################################################

    def GUIUpate_enable(self):
        """ GUI update """ 
        if self._is_in_transition_grace():
            return
        now_mono = time.monotonic()
        last_gui_by_stream = getattr(self, "_last_gui_emit_monotonic_by_stream", None)
        if not isinstance(last_gui_by_stream, dict):
            last_gui_by_stream = {}
            self._last_gui_emit_monotonic_by_stream = last_gui_by_stream
        lfp_can_emit_now = (now_mono - float(last_gui_by_stream.get("lfp", 0.0) or 0.0)) >= self.gui_emit_min_interval_s
        spike_can_emit_now = (now_mono - float(last_gui_by_stream.get("spike", 0.0) or 0.0)) >= self.gui_emit_min_interval_s
        mode2_can_emit_now = (now_mono - float(last_gui_by_stream.get("mode2", 0.0) or 0.0)) >= self.gui_emit_min_interval_s
        lfp_threshold = max(1, int(SerialPort._gui_interval_for_stream(self, SerialPort._active_lfp_gui_stream(self))))
        spike_threshold = max(1, int(SerialPort._gui_interval_for_stream(self, SerialPort._active_spike_gui_stream(self))))
        mode2_threshold = max(1, int(SerialPort._gui_interval_for_stream(self, "mode2_raw")))

        if(len(self.lfptimestamp_GUI) >= lfp_threshold and (
            lfp_can_emit_now or len(self.lfptimestamp_GUI) >= lfp_threshold * self.gui_emit_force_multiplier
        )): # mode 0 & 3
            packet_loss_snapshot = SerialPort._packet_loss_status_snapshot(
                self,
                SerialPort._active_lfp_gui_stream(self),
            )
            if self.sensordata_GUI[6] and self.sensordata_GUI[7] and self.sensordata_GUI[8]:
                self.last_battery_triplet = (
                    float(self.sensordata_GUI[6][-1]),
                    float(self.sensordata_GUI[7][-1]),
                    float(self.sensordata_GUI[8][-1]),
                )
            try:
                emit_detail = self._should_emit_detail_payload(now_mono, stream_key="lfp")
            except TypeError:
                emit_detail = self._should_emit_detail_payload(now_mono)
            if emit_detail:
                (
                    lfptimestamp_payload,
                    lfppacketindex_payload,
                    lfpdata_payload,
                    sensordata_payload,
                    spikerasterdata_payload,
                    esadata_payload,
                    mode0rawdata_payload,
                    mode0rawchannel_payload,
                ) = self._handoff_lfp_gui_buffers()
                header = [0, self.mode3_reref_status] if int(getattr(self, "current_stream_mode", STREAM_MODE_IDLE)) == STREAM_MODE_MODE3 else [0]
                mode0_raw_channel = mode0rawchannel_payload[-1] if mode0rawchannel_payload else getattr(self, "mode3_raw_channel_gui", 0)
                self.GUIUpdate.emit([
                    header,
                    lfptimestamp_payload,
                    lfpdata_payload,
                    sensordata_payload,
                    spikerasterdata_payload,
                    esadata_payload,
                    lfppacketindex_payload,
                    mode0rawdata_payload,
                    mode0_raw_channel,
                ]) # mode 0 / mode 3 LFP
                try:
                    self._mark_detail_payload_emitted(now_mono, stream_key="lfp")
                except TypeError:
                    self._mark_detail_payload_emitted(now_mono)
            else:
                if not bool(getattr(self, "detail_enabled", False)):
                    self.lfptimestamp_GUI.clear()
                    self.lfppacketindex_GUI.clear()
                    for ch in self.lfpdata_GUI: ch.clear()
                    for ch in self.sensordata_GUI: ch.clear()
                    for ch in self.spikerasterdata_GUI: ch.clear()
                    for ch in self.ESAdata_GUI: ch.clear()
                    if hasattr(self, "mode0rawdata_GUI"):
                        self.mode0rawdata_GUI.clear()
                    if hasattr(self, "mode0rawchannel_GUI"):
                        self.mode0rawchannel_GUI.clear()
            last_gui_by_stream["lfp"] = now_mono
            self._last_gui_emit_monotonic = now_mono
            self._emit_status_update(
                packet_loss=packet_loss_snapshot["missing"],
                packet_count=packet_loss_snapshot["received"],
                packet_loss_batch=packet_loss_snapshot["batch_missing"],
                packet_count_batch=packet_loss_snapshot["batch_received"],
                packet_loss_percent_current=packet_loss_snapshot["percent"],
                packet_loss_expected_current=packet_loss_snapshot["expected"],
                packet_metrics_seq=packet_loss_snapshot["sequence"],
            )

        if(len(self.spiketimestamp_GUI) >= spike_threshold and (
            spike_can_emit_now or len(self.spiketimestamp_GUI) >= spike_threshold * self.gui_emit_force_multiplier
        )): # mode 1
            packet_loss_snapshot = SerialPort._packet_loss_status_snapshot(
                self,
                SerialPort._active_spike_gui_stream(self),
            )
            if self.sensordata_GUI[6] and self.sensordata_GUI[7] and self.sensordata_GUI[8]:
                self.last_battery_triplet = (
                    float(self.sensordata_GUI[6][-1]),
                    float(self.sensordata_GUI[7][-1]),
                    float(self.sensordata_GUI[8][-1]),
                )
            emit_threshold_samples = bool(getattr(self, "threshold_samples_enabled", False))
            try:
                emit_detail = self._should_emit_detail_payload(now_mono, stream_key="spike")
            except TypeError:
                emit_detail = self._should_emit_detail_payload(now_mono)
            is_mode3_raw_stream = int(getattr(self, "_spike_gui_expected_step", 1) or 1) == 4
            if emit_detail:
                if is_mode3_raw_stream:
                    (
                        spiketimestamp_payload,
                        spikepacketindex_payload,
                        spikedata_payload,
                        sensordata_payload,
                        spikerasterdata_payload,
                        alignment_payload,
                    ) = SerialPort._handoff_mode3_raw_gui_buffers(self)
                else:
                    (
                        spiketimestamp_payload,
                        spikepacketindex_payload,
                        spikedata_payload,
                        sensordata_payload,
                        spikerasterdata_payload,
                        alignment_payload,
                    ) = self._handoff_mode1_gui_buffers()
            elif emit_threshold_samples:
                spiketimestamp_payload = list(self.spiketimestamp_GUI)
                spikepacketindex_payload = list(self.spikepacketindex_GUI)
                spikedata_payload = list(self.spikedata_GUI)
                sensordata_payload = [list(ch) for ch in self.sensordata_GUI]
                spikerasterdata_payload = [list(ch) for ch in self.spikerasterdata_GUI]
                alignment_payload = list(self.alignment_GUI)
            else:
                spiketimestamp_payload = []
                spikepacketindex_payload = []
                spikedata_payload = []
                sensordata_payload = []
                spikerasterdata_payload = []
                alignment_payload = []
            if emit_threshold_samples:
                self.ThresholdSamplesUpdate.emit({
                    "reported_fw_channel": int(getattr(self, "spike_channel_index_mode1_3", 0) or 0),
                    "samples": spikedata_payload,
                })
            if emit_detail:
                self.GUIUpdate.emit([[1, self.spike_channel_index_mode1_3], spiketimestamp_payload, spikedata_payload, sensordata_payload, spikerasterdata_payload,
                alignment_payload, spikepacketindex_payload]) # mode 1
                try:
                    self._mark_detail_payload_emitted(now_mono, stream_key="spike")
                except TypeError:
                    self._mark_detail_payload_emitted(now_mono)
            if not emit_threshold_samples and not emit_detail and not bool(getattr(self, "detail_enabled", False)):
                self.spiketimestamp_GUI.clear()
                self.spikepacketindex_GUI.clear()
                self.spikedata_GUI.clear()
                if not is_mode3_raw_stream:
                    for ch in self.spikerasterdata_GUI: ch.clear()
                    for ch in self.sensordata_GUI: ch.clear()
                self.alignment_GUI.clear()
            elif emit_threshold_samples and not emit_detail and not bool(getattr(self, "detail_enabled", False)):
                self.spiketimestamp_GUI.clear()
                self.spikepacketindex_GUI.clear()
                self.spikedata_GUI.clear()
                if not is_mode3_raw_stream:
                    for ch in self.spikerasterdata_GUI: ch.clear()
                    for ch in self.sensordata_GUI: ch.clear()
                self.alignment_GUI.clear()
            last_gui_by_stream["spike"] = now_mono
            self._last_gui_emit_monotonic = now_mono
            self._emit_status_update(
                packet_loss=packet_loss_snapshot["missing"],
                packet_count=packet_loss_snapshot["received"],
                packet_loss_batch=packet_loss_snapshot["batch_missing"],
                packet_count_batch=packet_loss_snapshot["batch_received"],
                packet_loss_percent_current=packet_loss_snapshot["percent"],
                packet_loss_expected_current=packet_loss_snapshot["expected"],
                packet_metrics_seq=packet_loss_snapshot["sequence"],
            )

        if(len(self.spiketimestamp_mode2_GUI) >= mode2_threshold and (
            mode2_can_emit_now or len(self.spiketimestamp_mode2_GUI) >= mode2_threshold * self.gui_emit_force_multiplier
        )):  # mode 2
            packet_loss_snapshot = SerialPort._packet_loss_status_snapshot(self, "mode2_raw")
            if self.sensordata_GUI[6] and self.sensordata_GUI[7] and self.sensordata_GUI[8]:
                self.last_battery_triplet = (
                    float(self.sensordata_GUI[6][-1]),
                    float(self.sensordata_GUI[7][-1]),
                    float(self.sensordata_GUI[8][-1]),
                )
            try:
                emit_detail = self._should_emit_detail_payload(now_mono, stream_key="mode2")
            except TypeError:
                emit_detail = self._should_emit_detail_payload(now_mono)
            if emit_detail:
                (
                    spiketimestamp_mode2_payload,
                    spikepacketindex_mode2_payload,
                    spikedata_mode2_payload,
                    sensordata_payload,
                    alignment_mode2_payload,
                ) = self._handoff_mode2_gui_buffers()
                self.GUIUpdate.emit([
                    [2],
                    spiketimestamp_mode2_payload,
                    spikedata_mode2_payload,
                    sensordata_payload,
                    spikepacketindex_mode2_payload,
                    dict(packet_loss_snapshot),
                    alignment_mode2_payload,
                    {
                        "mode2_sample_frequency": float(getattr(self, "mode2_sample_frequency", MODE2_V2_FS) or MODE2_V2_FS),
                        "mode2_packet_samples": int(getattr(self, "mode2_packet_samples", MODE2_V2_PACKET_SAMPLES) or MODE2_V2_PACKET_SAMPLES),
                        "sensor_sample_frequency": MODE2_SENSOR_SAMPLE_RATES[0],
                    },
                ]) # mode 2
                try:
                    self._mark_detail_payload_emitted(now_mono, stream_key="mode2")
                except TypeError:
                    self._mark_detail_payload_emitted(now_mono)
            else:
                if not bool(getattr(self, "detail_enabled", False)):
                    self.spiketimestamp_mode2_GUI.clear()
                    self.spikepacketindex_mode2_GUI.clear()
                    for ch in self.spikedata_mode2_GUI:
                        ch.clear()
                    self.alignment_mode2_GUI.clear()
                    for ch in self.sensordata_GUI: ch.clear()
            last_gui_by_stream["mode2"] = now_mono
            self._last_gui_emit_monotonic = now_mono
            self._emit_status_update(
                packet_loss=packet_loss_snapshot["missing"],
                packet_count=packet_loss_snapshot["received"],
                packet_loss_batch=packet_loss_snapshot["batch_missing"],
                packet_count_batch=packet_loss_snapshot["batch_received"],
                packet_loss_percent_current=packet_loss_snapshot["percent"],
                packet_loss_expected_current=packet_loss_snapshot["expected"],
                packet_metrics_seq=packet_loss_snapshot["sequence"],
            )
      
    def LSM6DS3_accelData_in_g(self, x): # 2g range
        return float(x * LSM6DS3_ACCEL_LSB_G)

    def LSM6DS3_gyroData_in_dps(self, x): # 500 range
        return float(x * LSM6DS3_GYRO_LSB_DPS)

    def DAC(self ,x ,raw=True, two_complement=False): 
        if raw:
            if(two_complement):
                if (int(x, 16) < int('8000', 16)): # positive value
                    return int(x, 16)
                else: # negative value
                    a = int(x, 16) 
                    return a - 2**(len(x) * 4)
            else:
                return int(x ,16) # n
        else:
            return (float(int(x ,16) * self.DAC_resolution) - 1.225) / 192 * 1000 * 1000  # uV 放大 192 倍 use unsigned offset 


    def flush(self):
        with self._io_lock:
            self.port.flushInput()

    def calc_SpikeThreshold(self ,x):
        """的数据计算一次threshold"""
        x = np.array(x ,dtype=np.float32)
        return 4 * np.median(np.abs(x) / 0.6745)

    def run(self): # re-write the run method of Qthread
        self._running = True
        self._closing = False
        disconnect_reason = ""
        try:
            self.read_data()
        except (serial.SerialException, OSError) as e:
            if not self._closing:
                disconnect_reason = str(e)
                logging.warning(f"Serial thread stopped due to communication error: {e}")
        except Exception as e:
            if not self._closing:
                disconnect_reason = str(e)
                logging.error(f"Serial thread crashed: {e}", exc_info=True)
        finally:
            self._running = False
            if disconnect_reason and not self._closing:
                self._notify_disconnect(disconnect_reason)
