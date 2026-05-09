import math
import random


def swap_sort(a, b):
    if a > b:
        return b, a
    return a, b


def median_fast_16(values):
    arr = list(values)
    network_layers = [
        [(0, 13), (1, 12), (2, 15), (3, 14), (4, 8), (5, 6), (7, 11), (9, 10)],
        [(0, 5), (1, 7), (2, 9), (3, 4), (6, 13), (8, 14), (10, 15), (11, 12)],
        [(0, 1), (2, 3), (4, 5), (6, 8), (7, 9), (10, 11), (12, 13), (14, 15)],
        [(0, 2), (1, 3), (4, 10), (5, 11), (6, 7), (8, 9), (12, 14), (13, 15)],
        [(1, 2), (3, 12), (4, 6), (5, 7), (8, 10), (9, 11), (13, 14)],
        [(1, 4), (2, 6), (5, 8), (7, 10), (9, 13), (11, 14)],
        [(2, 4), (3, 6), (9, 12), (11, 13)],
        [(3, 5), (6, 8), (7, 9), (10, 12)],
        [(3, 4), (5, 6), (7, 8), (9, 10), (11, 12)],
        [(6, 7), (8, 9)],
    ]
    for layer in network_layers:
        for i, j in layer:
            arr[i], arr[j] = swap_sort(arr[i], arr[j])
    return 0.5 * (arr[7] + arr[8])


def median_safe_16(values):
    sorted_values = list(values)
    for i in range(1, 16):
        key = sorted_values[i]
        j = i - 1
        while j >= 0 and sorted_values[j] > key:
            sorted_values[j + 1] = sorted_values[j]
            j -= 1
        sorted_values[j + 1] = key
    return 0.5 * (sorted_values[7] + sorted_values[8])


def median_ref(values):
    s = sorted(values)
    return 0.5 * (s[7] + s[8])


def build_16_sequences():
    return [
        ("ascending_int", [float(i) for i in range(16)]),
        ("descending_int", [float(i) for i in range(15, -1, -1)]),
        ("all_same", [42.0] * 16),
        ("half_neg_half_pos", [-100.0] * 8 + [100.0] * 8),
        ("alternating_sign", [(-1.0) ** i * (i + 1) for i in range(16)]),
        ("with_duplicates", [1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0, 4.0, 5.0, 5.0, 6.0, 6.0, 7.0, 7.0, 8.0, 8.0]),
        ("near_zero_small", [1e-6, -1e-6, 2e-6, -2e-6, 3e-6, -3e-6, 4e-6, -4e-6, 5e-6, -5e-6, 6e-6, -6e-6, 7e-6, -7e-6, 8e-6, -8e-6]),
        ("large_dynamic_range", [1e9, -1e9, 1e8, -1e8, 1e7, -1e7, 1e6, -1e6, 1e5, -1e5, 1e4, -1e4, 1e3, -1e3, 10.0, -10.0]),
        ("median_between_duplicates", [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]),
        ("single_outlier_high", [0.0] * 15 + [1e6]),
        ("single_outlier_low", [-1e6] + [0.0] * 15),
        ("fractional_values", [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8]),
        ("already_center_equal", [5.0, 1.0, 9.0, 2.0, 8.0, 3.0, 7.0, 4.0, 6.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]),
        ("negative_heavy", [-50.0, -40.0, -30.0, -20.0, -10.0, -9.0, -8.0, -7.0, -6.0, -5.0, -4.0, -3.0, -2.0, -1.0, 0.0, 100.0]),
        ("edge_order_mix", [16.0, 1.0, 15.0, 2.0, 14.0, 3.0, 13.0, 4.0, 12.0, 5.0, 11.0, 6.0, 10.0, 7.0, 9.0, 8.0]),
        ("close_float_values", [1.000001, 1.000002, 1.000003, 1.000004, 1.000005, 1.000006, 1.000007, 1.000008, 1.000009, 1.000010, 1.000011, 1.000012, 1.000013, 1.000014, 1.000015, 1.000016]),
    ]


def compare_one_case(name, seq, eps=1e-7):
    f = median_fast_16(seq)
    s = median_safe_16(seq)
    r = median_ref(seq)
    ok_fast = math.isclose(f, r, rel_tol=0.0, abs_tol=eps)
    ok_safe = math.isclose(s, r, rel_tol=0.0, abs_tol=eps)
    return {
        "name": name,
        "fast": f,
        "safe": s,
        "ref": r,
        "ok_fast": ok_fast,
        "ok_safe": ok_safe,
    }


def run_random_fuzz(n=20000, seed=20260308, eps=1e-7):
    rng = random.Random(seed)
    bad_fast = 0
    bad_safe = 0
    first_bad_fast = None
    first_bad_safe = None
    for _ in range(n):
        seq = [rng.uniform(-1e4, 1e4) for _ in range(16)]
        res = compare_one_case("random", seq, eps=eps)
        if not res["ok_fast"]:
            bad_fast += 1
            if first_bad_fast is None:
                first_bad_fast = (seq, res)
        if not res["ok_safe"]:
            bad_safe += 1
            if first_bad_safe is None:
                first_bad_safe = (seq, res)
    return bad_fast, bad_safe, first_bad_fast, first_bad_safe


def main():
    print("=== Fixed 16-case test ===")
    results = [compare_one_case(name, seq) for name, seq in build_16_sequences()]
    for r in results:
        status = "OK" if (r["ok_fast"] and r["ok_safe"]) else "FAIL"
        print(f"{r['name']:<24} {status}  fast={r['fast']:.9f} safe={r['safe']:.9f} ref={r['ref']:.9f}")

    bad_fixed = [r for r in results if not (r["ok_fast"] and r["ok_safe"])]
    print(f"\nfixed_case_fail_count={len(bad_fixed)} / {len(results)}")

    print("\n=== Random fuzz test ===")
    bad_fast, bad_safe, first_bad_fast, first_bad_safe = run_random_fuzz()
    print(f"random_bad_fast={bad_fast}")
    print(f"random_bad_safe={bad_safe}")

    if first_bad_fast is not None:
        seq, r = first_bad_fast
        print("first_bad_fast_sequence=", seq)
        print("first_bad_fast_result=", r)

    if first_bad_safe is not None:
        seq, r = first_bad_safe
        print("first_bad_safe_sequence=", seq)
        print("first_bad_safe_result=", r)

    all_ok = (len(bad_fixed) == 0) and (bad_fast == 0) and (bad_safe == 0)
    print(f"\nALL_OK={all_ok}")


if __name__ == "__main__":
    main()
