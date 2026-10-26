"""Transition controller: the exact rules from the problem statement (section 6).

Kept deliberately tiny and free of any other logic so it can be audited against
the spec line by line. Battery feasibility is advisory and lives in battery.py;
it never changes these signals.
"""
RENEWABLE_DEMAND_KW = 160.0  # strictly greater than switches to storage
OVERLOAD_DEMAND_KW = 200.0   # greater than or equal raises the alert
PEAK_WINDOW = (18, 22)       # inclusive hours

MODE_RENEWABLE = "RENEWABLE_STORAGE"
MODE_GRID = "MAIN_GRID"


def evaluate(hour, demand_kw):
    """Return the dispatch signal for one forecast hour."""
    in_peak = PEAK_WINDOW[0] <= hour <= PEAK_WINDOW[1]
    high_demand = demand_kw > RENEWABLE_DEMAND_KW
    reasons = []
    if high_demand:
        reasons.append(f"demand {demand_kw:.1f} kW > {RENEWABLE_DEMAND_KW:.0f} kW")
    if in_peak:
        reasons.append(f"hour {hour:02d}:00 in peak window {PEAK_WINDOW[0]}:00-{PEAK_WINDOW[1]}:00")
    mode = MODE_RENEWABLE if (high_demand or in_peak) else MODE_GRID
    overload = demand_kw >= OVERLOAD_DEMAND_KW
    return {
        "grid_mode": mode,
        "overload_alert": overload,
        "reason": "; ".join(reasons) if reasons else f"demand {demand_kw:.1f} kW <= {RENEWABLE_DEMAND_KW:.0f} kW, off-peak",
    }


def transitions(signals):
    """List the hours where the grid mode changes (the actual switch commands)."""
    out = []
    prev = None
    for s in signals:
        if s["grid_mode"] != prev:
            out.append({"timestamp": s["timestamp"], "from": prev, "to": s["grid_mode"]})
            prev = s["grid_mode"]
    return out
