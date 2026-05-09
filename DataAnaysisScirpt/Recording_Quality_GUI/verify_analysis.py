import sys
import os

print("Starting verification script...")

try:
    # Add current dir to path to import analysis
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import analysis
    print("Imported analysis module.")
except ImportError as e:
    print(f"Error importing analysis: {e}")
    sys.exit(1)

def test_analysis():
    print("Inside test_analysis...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Correct paths based on previous LS
    # Note: Use forward slashes or raw strings to avoid escaping issues
    files_to_check = [
        os.path.join(base_dir, "Mode3NeuralDataDemo", "2026-02-21-00-19-06mode3_raw.edf"),
        os.path.join(base_dir, "Mode3NeuralDataDemo", "2026-02-21-00-19-06LFP&ESA.edf"),
        os.path.join(base_dir, "BehaviorDataDemo", "2026-02-19-00-16-35sensor.edf")
    ]
    
    print("--- Testing Data Loss Calculation ---")
    threshold_map = {} # Empty map, uses default -1000
    
    for fpath in files_to_check:
        print(f"Checking file: {fpath}")
        if os.path.exists(fpath):
            print(f"Processing {os.path.basename(fpath)}...")
            try:
                results = analysis.calculate_data_loss(fpath, threshold_map, default_threshold=-1000)
                if not results:
                    print("  No results returned (empty dict).")
                for ch, stats in results.items():
                    print(f"  Channel: {ch}, Loss Rate: {stats['loss_rate']:.4f} ({stats['loss_count']}/{stats['total_samples']})")
            except Exception as e:
                print(f"  Error calculating loss: {e}")
        else:
            print(f"File not found: {fpath}")

    print("\n--- Testing Sync Pulse Detection (Raw File) ---")
    raw_file = files_to_check[0]
    if os.path.exists(raw_file):
        try:
            success_rate, n_matched, n_total, avg_delay_ms, msg = analysis.detect_sync_success_rate(raw_file, method='band')
            success_rate_e, n_matched_e, n_total_e, avg_delay_ms_e, msg_e = analysis.detect_sync_success_rate(raw_file, method='energy')
            print(f"File: {os.path.basename(raw_file)}")
            print(f"[Band] Success Rate: {success_rate:.4f}")
            print(f"[Band] Matched: {n_matched}/{n_total}")
            print(f"[Band] Avg Delay (ms): {avg_delay_ms}")
            print(f"[Band] Message: {msg}")
            print(f"[Energy] Success Rate: {success_rate_e:.4f}")
            print(f"[Energy] Matched: {n_matched_e}/{n_total_e}")
            print(f"[Energy] Avg Delay (ms): {avg_delay_ms_e}")
            print(f"[Energy] Message: {msg_e}")
        except Exception as e:
            print(f"Error detecting sync: {e}")
    else:
        print(f"Raw file not found: {raw_file}")

if __name__ == "__main__":
    test_analysis()
