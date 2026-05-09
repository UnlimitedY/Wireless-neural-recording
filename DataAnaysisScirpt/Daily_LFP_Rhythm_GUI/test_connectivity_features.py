import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from continuous_lfp_rhythm.connectivity_features import compute_pac_matrix, compute_plv_matrix


def test_plv_matrix_detects_phase_locking():
    rng = np.random.default_rng(3)
    t = np.linspace(0.0, 20.0, 5000, endpoint=False)
    base = 2.0 * np.pi * 8.0 * t
    phase = np.vstack([
        base,
        base + 0.15,
        rng.uniform(-np.pi, np.pi, size=t.size),
    ])

    plv = compute_plv_matrix(phase)

    assert plv.shape == (3, 3)
    assert plv[0, 1] > 0.98
    assert plv[0, 2] < 0.08
    assert np.allclose(np.diag(plv), 1.0)


def test_pac_matrix_detects_phase_modulated_amplitude():
    t = np.linspace(0.0, 30.0, 6000, endpoint=False)
    phase = np.vstack([2.0 * np.pi * 4.0 * t])
    amplitude_modulated = 1.0 + 0.8 * np.cos(phase[0])
    amplitude_flat = np.ones_like(amplitude_modulated)
    amplitude = np.vstack([amplitude_modulated, amplitude_flat])

    pac = compute_pac_matrix(((phase + np.pi) % (2.0 * np.pi)) - np.pi, amplitude, n_bins=18)

    assert pac.shape == (1, 2)
    assert pac[0, 0] > 0.01
    assert pac[0, 1] < pac[0, 0] * 0.2


if __name__ == "__main__":
    test_plv_matrix_detects_phase_locking()
    test_pac_matrix_detects_phase_modulated_amplitude()
    print("connectivity feature tests passed")
