import numpy as np

MODE0_DEEP_TO_SHALLOW_CHANNELS = [3, 12, 0, 15, 1, 14, 2, 13, 4, 11, 5, 10, 6, 9, 7, 8]
MODE3_DEEP_TO_SHALLOW_CHANNELS = [5, 14, 2, 1, 3, 0, 4, 15, 6, 13, 7, 12, 8, 11, 9, 10]
MODE0_SHALLOW_TO_DEEP_CHANNELS = list(reversed(MODE0_DEEP_TO_SHALLOW_CHANNELS))
MODE3_SHALLOW_TO_DEEP_CHANNELS = list(reversed(MODE3_DEEP_TO_SHALLOW_CHANNELS))
UNIFIED_PROBE_CHANNEL_COUNT = 16
CHANNEL_ORDER_SEMANTICS = "probe_shallow_to_deep_renumbered_0_to_15"


def get_shallow_to_deep_source_order(file_type):
    if file_type == "mode0_lfp":
        return MODE0_SHALLOW_TO_DEEP_CHANNELS
    if file_type == "mode3_lfp":
        return MODE3_SHALLOW_TO_DEEP_CHANNELS
    return list(range(UNIFIED_PROBE_CHANNEL_COUNT))


def reorder_lfp_to_unified_probe_order(data_matrix, file_type="mode3_lfp", fill_value=np.nan):
    data = np.asarray(data_matrix, dtype=np.float64)
    if data.ndim != 2:
        raise ValueError("LFP data must be 2D [channels, samples] before channel reordering.")
    source_order = get_shallow_to_deep_source_order(file_type)
    reordered = np.full(
        (UNIFIED_PROBE_CHANNEL_COUNT, data.shape[1]),
        fill_value,
        dtype=data.dtype,
    )
    for dst_idx, src_idx in enumerate(source_order):
        if src_idx < data.shape[0]:
            reordered[dst_idx] = data[src_idx]
    return reordered
