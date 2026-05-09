import numpy as np
import scipy.signal as signal
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.svm import SVC
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def _fill_nan_for_analysis(values):
    arr = np.asarray(values, dtype=float).copy()
    finite = np.isfinite(arr)
    if np.all(finite):
        return arr
    if np.any(finite):
        x = np.arange(arr.size)
        arr[~finite] = np.interp(x[~finite], x[finite], arr[finite])
    else:
        arr[:] = 0.0
    return arr


class VerificationAnalyzer:
    def __init__(self, dp_instance):
        self.dp = dp_instance # DataProcessor instance with trial_dict loaded
        
    def calculate_snr(self, trial_id, signal_band=(15, 30), baseline_band=(1, 100), pre_ms=1000, post_ms=4000):
        # Data Quality Analysis: SNR 
        # Calculate SNR by comparing Power in specific band vs broad band during action
        data = self.dp.get_trial_data(trial_id, pre_ms=pre_ms, post_ms=post_ms)
        if not data: return None
        lfp_t, lfp_v = data['lfp']
        fs = self.dp.trial_dict[trial_id]['fs_lfp']
        
        snr_results = []
        for ch in range(lfp_v.shape[0]):
            f, pxx = signal.welch(_fill_nan_for_analysis(lfp_v[ch, :]), fs, nperseg=int(fs*0.5))
            
            sig_mask = (f >= signal_band[0]) & (f <= signal_band[1])
            base_mask = (f >= baseline_band[0]) & (f <= baseline_band[1])
            
            sig_power = np.sum(pxx[sig_mask])
            base_power = np.sum(pxx[base_mask])
            
            snr = 10 * np.log10(sig_power / (base_power + 1e-10))
            snr_results.append(snr)
            
        return snr_results

    def calculate_spectra(self, trial_id, pre_ms=1000, post_ms=4000):
        # Signal Spectrum Analysis
        data = self.dp.get_trial_data(trial_id, pre_ms=pre_ms, post_ms=post_ms)
        if not data: return None
        lfp_t, lfp_v = data['lfp']
        fs = self.dp.trial_dict[trial_id]['fs_lfp']
        
        spectra = []
        for ch in range(lfp_v.shape[0]):
            f, pxx = signal.welch(_fill_nan_for_analysis(lfp_v[ch, :]), fs, nperseg=int(fs))
            # Limit to 0-150Hz
            mask = f <= 150
            spectra.append((f[mask], pxx[mask]))
            
        return spectra

    def run_decoding_analysis(self):
        # Based on Decoding Data Quality Check
        # Uses multiple trials to run a train/test pipeline.
        X = []
        y = []
        
        for k, td in self.dp.trial_dict.items():
            trial_obj = td['trial_obj']
            # Target is TrialType (1 = Left, 2 = Right)
            if trial_obj.trial_type not in [1, 2]: continue
            
            # Feature extraction (e.g., LFP Beta Band power around Cue)
            tdata = self.dp.get_trial_data(k, pre_ms=0, post_ms=1000)
            if not tdata: continue
            
            lfp_v = tdata['lfp'][1]
            fs = td['fs_lfp']
            
            # Simple feature: Average power in 15-30Hz across all 16 channels
            features = []
            for ch in range(min(16, lfp_v.shape[0])): # Only use first 16 channels assuming 16-31 is ESA
                f, pxx = signal.welch(_fill_nan_for_analysis(lfp_v[ch, :]), fs, nperseg=int(fs*0.5))
                mask = (f >= 15) & (f <= 30)
                features.append(np.sum(pxx[mask]))
                
            X.append(features)
            y.append(trial_obj.trial_type)
            
        X = np.array(X)
        y = np.array(y)
        
        if len(X) < 10:
            return {"error": "Not enough valid trials for decoding."}
            
        # Classifier Setup
        clf = make_pipeline(StandardScaler(), SVC(kernel='linear', C=1))
        
        # 5-fold cross validation
        scores = cross_val_score(clf, X, y, cv=5)
        
        # Train test split for basic reporting
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        clf.fit(X_train, y_train)
        acc = clf.score(X_test, y_test)
        
        return {
            "cv_scores": scores.tolist(),
            "cv_mean": np.mean(scores),
            "test_accuracy": acc,
            "dataset_size": len(X)
        }
