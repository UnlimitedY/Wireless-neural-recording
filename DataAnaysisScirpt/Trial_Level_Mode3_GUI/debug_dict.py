import pyedflib
import numpy as np
from data_processing import parse_trial_file, parse_tevent_file

trials = parse_trial_file('ExampleData/BehaviorFile/Trial.txt')
tevents = parse_tevent_file('ExampleData/BehaviorFile/Tevent.txt')
print(f"Total Trials Parsed: {len(trials)}")
print(f"Total tevnts Parsed: {len(tevents)}")

f = pyedflib.EdfReader('ExampleData/NeuralFile/2026-04-03-04-46-19mode3_raw.edf')
raw_align = f.readSignal(2)
f.close()

align_binary = (raw_align > 0.5).astype(int)
edges = np.where(np.diff(align_binary, prepend=0) == 1)[0]
print(f"\nFound {len(edges)} alignment edges.")

for ev_idx in edges:
    trial_id_pulse = np.round(raw_align[min(ev_idx + 10, len(raw_align)-1)])
    trial_id = int(trial_id_pulse)
    
    status = []
    if trial_id not in trials:
        status.append("NOT IN trials")
    if trial_id not in tevents:
        status.append("NOT IN tevents")
    if trial_id in trials and trials[trial_id].start_ts == 0:
        status.append("start_ts == 0")
        
    print(f"Edge index: {ev_idx}, Trial ID: {trial_id}, Status: {' | '.join(status) if status else 'OK'}")
