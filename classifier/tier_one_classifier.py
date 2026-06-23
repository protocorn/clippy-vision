import sqlite3, json, time, math

from core.baseline import load_baseline

INTERESTING_THRESHOLD     = 7
NOT_INTERESTING_THRESHOLD = 4   # narrower ambiguous band → fewer Tier-2 calls

def tier1_score(event: dict, conn: sqlite3.Connection) -> dict | None:
    score = 5   # neutral start
    notes = []
    window_context = event["window_context"]
    process = window_context["process_name"] or ""
    payload = json.loads(event["payload"]) if event["payload"] else {}

    # --- Feature 1: Deviation magnitude ---
    # Already computed by baseline.py, free to reuse
    if event["event_type"] == "deviation":
        sigma = payload.get("overall_deviation", 0)
        if sigma > 1.5:
            score += 2
            notes.append(f"deviation {sigma}σ")
        elif sigma < 1.0:
            score -= 3   # sub-1σ is normal variation, not worth LLM time
            notes.append(f"low deviation {sigma:.2f}σ")

    # --- Feature 2: Context novelty ---
    # How many times has this process appeared in the last 7 days?
    # Rare process = high novelty = more interesting
    row = conn.execute(
        """SELECT COUNT(*) FROM events
           WHERE process_name = ?
           AND timestamp > ?
           AND event_id != ?""",
        (process, time.time() - 7 * 86400, event["event_id"])
    ).fetchone()
    prior_count = row[0] if row else 0

    if prior_count == 0:
        score += 2.5   # never seen this process before
        notes.append("new process")
    elif prior_count < 5:
        score += 1.5
        notes.append("rare process")
    elif prior_count < 50 and prior_count >= 5:
        score +=1     # very common process — routine
        notes.append("very common process")
    else:
        score += 0.5
        notes.append("common process")

    # --- Feature 3: Typing intensity vs personal baseline ---
    if event["event_type"] == "typing_burst":
        baseline     = load_baseline()
        context_data = baseline.get(process, {}).get("metrics", {})

        wpm      = payload.get("typing_speed_wpm", 0)
        wpm_stats = context_data.get("typing_speed_wpm")
        if wpm_stats and wpm_stats["variance"] > 1e-6:
            wpm_z = (wpm - wpm_stats["mean"]) / math.sqrt(wpm_stats["variance"])
            if wpm_z > 1.5:
                score += 2
                notes.append(f"unusually fast for you ({wpm:.0f} wpm, +{wpm_z:.1f}σ)")
            elif wpm_z < -1.5:
                score += 1.5   # unusually slow is also interesting — struggling?
                notes.append(f"unusually slow for you ({wpm:.0f} wpm, {wpm_z:.1f}σ)")
        else:
            # No personal baseline yet — small neutral bump to avoid discarding
            if wpm > 0:
                score += 0.5

        rev       = payload.get("revision_ratio", 0)
        rev_stats = context_data.get("revision_ratio")
        if rev_stats and rev_stats["variance"] > 1e-6:
            rev_z = (rev - rev_stats["mean"]) / math.sqrt(rev_stats["variance"])
            if rev_z > 1.5:
                score += 1.5
                notes.append(f"unusually high revision for you ({rev_z:.1f}σ)")
        else:
            if rev > 0.3:   # flat fallback before baseline builds up
                score += 0.5

    # --- Feature 4: Paste events — neutral by default, let WPM/context drive the score ---
    # Paste is routine in coding; only interesting if paired with other signals

    # --- Feature 5: Clipboard / paste content ---
    if event["event_type"] in ("clipboard_change", "paste"):
        content = payload.get("content") or payload.get("pasted_content") or ""
        word_count = len(content.split())
        
        # Long content = likely meaningful (copying code, error messages, URLs)
        if word_count > 50:
            score += 2
            notes.append(f"substantial clipboard content ({word_count} words)")
        elif word_count > 15:
            score += 1
            notes.append(f"moderate clipboard content ({word_count} words)")

    # Clamp to [0, 10]
    score = max(0, math.floor(min(10, score)))

    if score >= INTERESTING_THRESHOLD:
        return {"verdict": "interesting", "score": score,
                "reason": ", ".join(notes) or "feature score high"}

    if score <= NOT_INTERESTING_THRESHOLD:
        return {"verdict": "not_interesting", "score": score,
                "reason": ", ".join(notes) or "feature score low"}

    return None   # ambiguous → Tier 2