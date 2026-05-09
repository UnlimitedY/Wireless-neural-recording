import math


IMPEDANCE_MODEL_VERSION = "rhd2132_channel_to_ref_v2"
IMPEDANCE_MODEL_NAME = "rhd2132_channel_to_ref_board_rc_removed"

DEFAULT_SIGNAL_INPUT_CAP_PF = 12.0
DEFAULT_SIGNAL_INPUT_RESISTANCE_OHM = 13.0e6
DEFAULT_REFERENCE_INPUT_CAP_PF = 325.0
DEFAULT_REFERENCE_INPUT_RESISTANCE_OHM = 0.5e6


def impedance_complex_from_mag_phase(magnitude_ohm, phase_deg):
    magnitude = float(magnitude_ohm)
    phase_rad = math.radians(float(phase_deg))
    return complex(magnitude * math.cos(phase_rad), magnitude * math.sin(phase_rad))


def impedance_mag_phase_from_complex(z_value):
    return float(abs(z_value)), float(math.degrees(math.atan2(z_value.imag, z_value.real)))


def build_impedance_model_metadata(series_resistor_kohm, shunt_cap_pf, frequency_hz):
    return {
        "impedance_model_version": IMPEDANCE_MODEL_VERSION,
        "impedance_model_name": IMPEDANCE_MODEL_NAME,
        "impedance_model_params": {
            "board_series_resistor_kohm": float(series_resistor_kohm),
            "board_shunt_cap_pf": float(shunt_cap_pf),
            "frequency_hz": float(frequency_hz),
            "signal_input_cap_pf": float(DEFAULT_SIGNAL_INPUT_CAP_PF),
            "signal_input_resistance_ohm": float(DEFAULT_SIGNAL_INPUT_RESISTANCE_OHM),
            "reference_input_cap_pf": float(DEFAULT_REFERENCE_INPUT_CAP_PF),
            "reference_input_resistance_ohm": float(DEFAULT_REFERENCE_INPUT_RESISTANCE_OHM),
        },
        "impedance_target_quantity": "channel_to_ref_impedance_after_board_rc_removal",
    }


def _node_admittance(input_resistance_ohm, total_cap_pf, omega):
    admittance = complex(0.0, 0.0)
    if input_resistance_ohm > 0.0:
        admittance += complex(1.0 / float(input_resistance_ohm), 0.0)
    if total_cap_pf > 0.0 and omega > 0.0:
        admittance += complex(0.0, omega * total_cap_pf * 1.0e-12)
    return admittance


def restore_channel_to_ref_impedance(
    measured_magnitude_ohm,
    measured_phase_deg,
    series_resistor_kohm,
    shunt_cap_pf,
    frequency_hz,
):
    z_measured = impedance_complex_from_mag_phase(measured_magnitude_ohm, measured_phase_deg)
    if abs(z_measured) <= 0.0:
        return z_measured

    board_series_resistor_ohm = max(0.0, float(series_resistor_kohm) * 1000.0)
    board_shunt_cap_pf = max(0.0, float(shunt_cap_pf))
    omega = 2.0 * math.pi * max(0.0, float(frequency_hz))

    signal_total_cap_pf = board_shunt_cap_pf + DEFAULT_SIGNAL_INPUT_CAP_PF
    reference_total_cap_pf = board_shunt_cap_pf + DEFAULT_REFERENCE_INPUT_CAP_PF

    y_signal = _node_admittance(
        DEFAULT_SIGNAL_INPUT_RESISTANCE_OHM,
        signal_total_cap_pf,
        omega,
    )
    y_reference = _node_admittance(
        DEFAULT_REFERENCE_INPUT_RESISTANCE_OHM,
        reference_total_cap_pf,
        omega,
    )

    denominator = (y_reference / z_measured) - (y_signal * y_reference)
    if abs(denominator) <= 1.0e-18:
        return z_measured

    z_branch = (y_signal + y_reference) / denominator
    z_channel_to_ref = z_branch - complex(2.0 * board_series_resistor_ohm, 0.0)

    if z_channel_to_ref.real < 0.0 and abs(z_channel_to_ref) < max(1.0, 0.05 * 2.0 * board_series_resistor_ohm):
        z_channel_to_ref = complex(0.0, z_channel_to_ref.imag)

    if not (math.isfinite(z_channel_to_ref.real) and math.isfinite(z_channel_to_ref.imag)):
        return z_measured
    return z_channel_to_ref


def device_impedance_from_channel_to_ref_impedance(
    channel_to_ref_magnitude_ohm,
    channel_to_ref_phase_deg,
    series_resistor_kohm,
    shunt_cap_pf,
    frequency_hz,
):
    z_channel_to_ref = impedance_complex_from_mag_phase(
        channel_to_ref_magnitude_ohm,
        channel_to_ref_phase_deg,
    )
    board_series_resistor_ohm = max(0.0, float(series_resistor_kohm) * 1000.0)
    board_shunt_cap_pf = max(0.0, float(shunt_cap_pf))
    omega = 2.0 * math.pi * max(0.0, float(frequency_hz))

    signal_total_cap_pf = board_shunt_cap_pf + DEFAULT_SIGNAL_INPUT_CAP_PF
    reference_total_cap_pf = board_shunt_cap_pf + DEFAULT_REFERENCE_INPUT_CAP_PF
    y_signal = _node_admittance(
        DEFAULT_SIGNAL_INPUT_RESISTANCE_OHM,
        signal_total_cap_pf,
        omega,
    )
    y_reference = _node_admittance(
        DEFAULT_REFERENCE_INPUT_RESISTANCE_OHM,
        reference_total_cap_pf,
        omega,
    )

    z_branch = z_channel_to_ref + complex(2.0 * board_series_resistor_ohm, 0.0)
    denominator = (y_signal * y_reference * z_branch) + y_signal + y_reference
    if abs(denominator) <= 1.0e-18:
        return z_branch
    return (y_reference * z_branch) / denominator
