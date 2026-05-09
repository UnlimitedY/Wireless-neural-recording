import json
import sys
import tempfile
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Common_Analysis.behavior_rt import compute_trial_rt_record, parse_rt_trials
from Common_Analysis.trial_parsing import TeventData, TrialData


def _trial_line(trial_num, epoch_s):
    base = [
        str(epoch_s),
        str(trial_num),
        "5",
        "1",
        "1",
        "100",
        "200",
        "0",
        "1",
        "3.5",
        "4.5",
        "0",
    ]
    extended = ["10", "90", "1", "0", "1", "2", "3", "0", "4", "1", "5"]
    states = ["123456", "3", "20", "1000", "6", "1300", "2", "2600"]
    return " ".join(base + extended + states)


def test_rt_uses_sample_start_to_first_choice_lick():
    trial = TrialData(_trial_line(1, 1700000000))
    tevent = TeventData("1 4 0 1300 8 1760 10 1810 1 2600")

    record, warnings = compute_trial_rt_record(trial, tevent, 1700000000000)

    assert warnings == []
    assert record["rt_ms"] == 460.0
    assert record["choice_event_id"] == 8
    assert record["sample_start_epoch_ms"] == 1700000000300.0
    assert record["choice_epoch_ms"] == 1700000000760.0


def test_parse_rt_trials_fits_log_rt_three_classes():
    with tempfile.TemporaryDirectory() as tmpdir:
        sd = Path(tmpdir) / "SD_card_files"
        session = sd / "day1"
        session.mkdir(parents=True)
        trial_lines = []
        tevent_lines = []
        rts = [180, 190, 210, 220, 230, 520, 540, 560, 580, 600, 1300, 1400, 1500, 1600, 1700]
        for idx, rt in enumerate(rts, start=1):
            trial_lines.append(_trial_line(idx, 1700000000 + idx))
            tevent_lines.append(f"{idx} 3 0 1300 8 {1300 + rt} 1 2600")
        (session / "Trial.txt").write_text("\n".join(trial_lines), encoding="utf-8")
        (session / "Tevent.txt").write_text("\n".join(tevent_lines), encoding="utf-8")

        records, fit, warnings = parse_rt_trials(sd)

        assert len(records) == len(rts)
        assert fit["n_valid_rt"] == len(rts)
        assert len(fit["thresholds_ms"]) == 2
        assert fit["thresholds_ms"][0] < fit["thresholds_ms"][1]
        classes = [record["rt_class"] for record in records]
        assert min(classes) == 0
        assert max(classes) == 2
        assert not [warning for warning in warnings if "Failed" in warning]


if __name__ == "__main__":
    test_rt_uses_sample_start_to_first_choice_lick()
    test_parse_rt_trials_fits_log_rt_three_classes()
    print("behavior RT feature tests passed")
