import json, math, os, time, threading
from typing import Optional

BASELINE_PATH = "data/baseline.json"
_baseline_lock = threading.Lock()

#--------------------------------#
# Change the alpha value in------#
# Future to be adaptive for------#
# each context-------------------#
#--------------------------------#

ALPHA = 0.05
MIN_SAMPLES = 30

TRACKED_METRICS =[
    "typing_speed_wpm",
    "avg_iki_ms",
    "avg_dwell_time_ms",
    "revision_ratio",
    "max_pause_duration_ms",
]


def _read_baseline_file() -> dict:
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH, 'r') as f:
            return json.load(f)
    return {}

def load_baseline() -> dict:
    with _baseline_lock:
        return _read_baseline_file()

def save_baseline(baseline: dict):
    """Must be called while holding _baseline_lock."""
    os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
    with open(BASELINE_PATH, "w") as f:
        json.dump(baseline, f, indent=2)

def update_baseline(metrics : dict, context_key : str):
    with _baseline_lock:
        baseline = _read_baseline_file()

        if context_key not in baseline:
            metric_baselines = {}
            for m in TRACKED_METRICS:
                if m in metrics:
                    metric_baselines[m] = {"mean": metrics[m], "variance": 0.0}
            baseline[context_key] = {
                "sample_count": 0,
                "alpha": ALPHA,
                "metrics": metric_baselines,
                "last_update": time.time()
            }
        else:
            context_data = baseline[context_key]
            alpha = context_data["alpha"]

            for m in TRACKED_METRICS:
                if m not in metrics or m not in context_data["metrics"]:
                    continue

                old_mean = context_data["metrics"][m]["mean"]
                old_var  = context_data["metrics"][m]["variance"]

                current_value = metrics[m]

                new_mean = (alpha * current_value) + ((1 - alpha) * old_mean)
                new_var  = (1 - alpha) * (old_var + alpha * (current_value - old_mean) ** 2)

                context_data["metrics"][m]["mean"] = new_mean
                context_data["metrics"][m]["variance"] = new_var

            context_data["sample_count"] += 1
            context_data["last_update"] = time.time()

        save_baseline(baseline)

def compute_deviation(metrics: dict, context_key:str) -> Optional[dict]:
    baseline = load_baseline()
    if context_key not in baseline:
        return None

    context_data = baseline[context_key]

    if context_data["sample_count"] < MIN_SAMPLES:
        return None
    
    z_scores = {} # deviation scores for each metric

    for m in TRACKED_METRICS:
        if m not in metrics or m not in context_data["metrics"]:
            continue
        
        mean = context_data["metrics"][m]["mean"]
        variance = context_data["metrics"][m]["variance"]
        if variance > 1e-6:
             z_scores[m] = round((metrics[m] - mean) / math.sqrt(variance), 2)
        
    if not z_scores:
        return None
    
    overall_deviation =  round(math.sqrt(sum(z**2 for z in z_scores.values()) / len(z_scores)), 2)

    return {
        "context_key": context_key,
        "overall_deviation": overall_deviation,
        "anomaly": overall_deviation > 2.0,
        "z_scores": z_scores
    }
    

