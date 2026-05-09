import re


def extract_number(token, as_int=False):
    match = re.search(r"[-+]?\d+(?:\.\d+)?", token)
    if not match:
        raise ValueError(f"Cannot parse numeric value from token: {token}")
    value = float(match.group(0))
    return int(value) if as_int else value


class TrialData:
    def __init__(self, line):
        parts = line.strip().split()
        if len(parts) < 12:
            raise ValueError("Trial line has insufficient columns")
        self.raw_line = line.strip()
        self.raw_parts = parts
        self.time = parts[0]
        self.trial_num = extract_number(parts[1], as_int=True)
        self.protocol_index = extract_number(parts[2], as_int=True)
        self.trial_type = extract_number(parts[3], as_int=True)
        self.trial_outcome = extract_number(parts[4], as_int=True)
        self.sample_period = extract_number(parts[5], as_int=True)
        self.delay_period = extract_number(parts[6], as_int=True)
        self.is_earlylick = extract_number(parts[7], as_int=True)
        self.rule = extract_number(parts[8], as_int=True)
        self.curr_stimu = [extract_number(parts[9]), extract_number(parts[10])]
        self.baseline_flag = extract_number(parts[11], as_int=True)
        self.extra_metadata = {}
        self.trial_start_timestamp_ms = None
        self.n_visited = None
        self.states_history = []

        self._parse_optional_metadata_and_states(parts)

        self.start_ts = 0
        self.end_ts = 0
        for state, timestamp in self.states_history:
            if state == 20 and self.start_ts == 0:
                self.start_ts = timestamp
            if state == 2:
                self.end_ts = max(self.end_ts, timestamp)

        if self.end_ts == 0 and len(self.states_history) > 0:
            self.end_ts = self.states_history[-1][1]

    def _parse_optional_metadata_and_states(self, parts):
        # Current DBRuleSwitch format:
        #   0..22 fixed metadata, 23 TrialStartTimestampMs, 24 nVisited,
        #   then nVisited pairs of state/time.
        # Older extended files omit TrialStartTimestampMs and put nVisited at 23.
        # Very old files only have the first 12 metadata columns and state pairs at the tail.
        parsers = (
            self._parse_new_extended_format,
            self._parse_old_extended_format,
            self._parse_tail_state_pairs,
        )
        for parser in parsers:
            if parser(parts):
                return

    def _parse_state_pairs_from(self, parts, start_idx, n_visited, require_exact=False):
        if n_visited is None:
            return None
        end_idx = start_idx + (2 * n_visited)
        if n_visited < 0 or end_idx > len(parts):
            return None
        if require_exact and end_idx != len(parts):
            return None
        states = []
        for idx in range(start_idx, end_idx, 2):
            try:
                states.append((int(parts[idx]), int(parts[idx + 1])))
            except Exception:
                return None
        return states

    def _parse_new_extended_format(self, parts):
        if len(parts) < 25:
            return False
        try:
            trial_start_timestamp_ms = int(float(parts[23]))
            n_visited = int(parts[24])
        except Exception:
            return False
        states = self._parse_state_pairs_from(parts, 25, n_visited, require_exact=True)
        if states is None:
            return False
        self._set_extended_metadata(parts, has_trial_start_timestamp=True)
        self.trial_start_timestamp_ms = (
            trial_start_timestamp_ms if trial_start_timestamp_ms >= 0 else None
        )
        self.n_visited = n_visited
        self.states_history = states
        return True

    def _parse_old_extended_format(self, parts):
        if len(parts) < 24:
            return False
        try:
            n_visited = int(parts[23])
        except Exception:
            return False
        states = self._parse_state_pairs_from(parts, 24, n_visited, require_exact=True)
        if states is None:
            return False
        self._set_extended_metadata(parts, has_trial_start_timestamp=False)
        self.n_visited = n_visited
        self.states_history = states
        return True

    def _parse_tail_state_pairs(self, parts):
        states = []
        for idx in range(len(parts) - 2, 0, -2):
            try:
                state = int(parts[idx])
                timestamp = int(parts[idx + 1])
            except Exception:
                break
            states.append((state, timestamp))
        states.reverse()
        self.states_history = states
        self.n_visited = len(states) if states else None
        return True

    def _set_extended_metadata(self, parts, has_trial_start_timestamp):
        names = [
            "curr_protocol_trials",
            "curr_protocol_perf_percent",
            "sample_type",
            "el_favor",
            "current_el_punish_enabled",
            "block_switch_mode",
            "block_switch_step",
            "rule_switch_hint_active",
            "rule_switch_correct_count",
            "trial_block_onset",
            "pending_protocol_index",
        ]
        for offset, name in enumerate(names, start=12):
            if offset < len(parts):
                self.extra_metadata[name] = _coerce_numeric(parts[offset])
        if has_trial_start_timestamp and len(parts) > 23:
            self.extra_metadata["trial_start_timestamp_ms"] = _coerce_numeric(parts[23])


class TeventData:
    def __init__(self, line):
        parts = line.strip().split()
        self.trial_num = int(parts[0])
        self.n_event = int(parts[1])
        self.events = []
        idx = 2
        for _ in range(self.n_event):
            if idx + 1 >= len(parts):
                break
            self.events.append((int(parts[idx]), int(parts[idx + 1])))
            idx += 2


def parse_trial_file(filepath):
    trials = {}
    with open(filepath, "r") as handle:
        for line in handle:
            if "Trial:" in line or "Tevent:" in line or not line.strip():
                continue
            parts = line.strip().split()
            if len(parts) < 12:
                continue
            try:
                trial = TrialData(line)
            except Exception:
                continue
            trials[trial.trial_num] = trial
    return trials


def parse_tevent_file(filepath):
    tevents = {}
    with open(filepath, "r") as handle:
        for line in handle:
            if "Tevent" in line:
                line = line.replace("Tevent:", "")
            if not line.strip():
                continue
            try:
                tevent = TeventData(line)
            except Exception:
                continue
            tevents[tevent.trial_num] = tevent
    return tevents


def _coerce_numeric(value):
    try:
        numeric = float(value)
    except Exception:
        return value
    if numeric.is_integer():
        return int(numeric)
    return numeric
