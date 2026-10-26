"""Hour-by-hour battery and solar simulation that follows the controller's signals.

The controller decides *when* to run on storage; this answers whether the
battery can actually carry it, and what it saves in cost and CO2 against
running on the main grid all day. Results are advisory only.
"""
from dataclasses import asdict, dataclass

from .controller import MODE_RENEWABLE


@dataclass
class BatteryConfig:
    capacity_kwh: float = 2500.0
    initial_soc_pct: float = 80.0
    min_soc_pct: float = 10.0
    max_power_kw: float = 300.0
    round_trip_efficiency: float = 0.90
    pv_capacity_kwp: float = 500.0          # solar array size
    pv_performance_ratio: float = 0.80
    grid_charge_hours: tuple = (0, 5)       # cheap overnight top-up window, inclusive
    grid_charge_target_pct: float = 90.0
    tariff_offpeak: float = 6.0             # INR per kWh, 23:00-05:59
    tariff_normal: float = 8.0
    tariff_peak: float = 12.0               # 18:00-22:59
    grid_co2_kg_per_kwh: float = 0.71       # India grid average (CEA)

    @classmethod
    def from_dict(cls, data):
        fields = cls.__dataclass_fields__
        kwargs = {}
        for k, v in (data or {}).items():
            if k in fields and k != "grid_charge_hours" and v is not None and v != "":
                kwargs[k] = float(v)
        cfg = cls(**kwargs)
        cfg.initial_soc_pct = min(100.0, max(0.0, cfg.initial_soc_pct))
        cfg.min_soc_pct = min(cfg.initial_soc_pct, max(0.0, cfg.min_soc_pct))
        return cfg


def tariff(cfg, hour):
    if 18 <= hour <= 22:
        return cfg.tariff_peak
    if hour >= 23 or hour <= 5:
        return cfg.tariff_offpeak
    return cfg.tariff_normal


def simulate(hours, cfg):
    """hours: list of dicts with timestamp, hour, demand_kw, solar_irradiance_wm2, grid_mode."""
    eff = cfg.round_trip_efficiency ** 0.5  # split losses between charge and discharge
    cap = cfg.capacity_kwh
    soc = cap * cfg.initial_soc_pct / 100
    floor = cap * cfg.min_soc_pct / 100

    steps = []
    totals = {"grid_kwh": 0.0, "battery_kwh": 0.0, "solar_kwh": 0.0, "cost": 0.0, "baseline_cost": 0.0, "co2_kg": 0.0, "baseline_co2_kg": 0.0}
    shortfall_hours = []
    for h in hours:
        demand = max(0.0, h["demand_kw"])
        pv = cfg.pv_capacity_kwp * cfg.pv_performance_ratio * h["solar_irradiance_wm2"] / 1000
        solar_used = min(pv, demand)
        net = demand - solar_used
        surplus = pv - solar_used
        battery_out = grid = charge = 0.0

        if h["grid_mode"] == MODE_RENEWABLE:
            available = max(0.0, (soc - floor) * eff)
            battery_out = min(net, available, cfg.max_power_kw)
            soc -= battery_out / eff
            grid = net - battery_out  # the battery could not carry the full load
            if grid > 0.5:
                shortfall_hours.append(h["timestamp"])
        else:
            grid = net
            hour = h["hour"]
            if cfg.grid_charge_hours[0] <= hour <= cfg.grid_charge_hours[1]:
                target = cap * cfg.grid_charge_target_pct / 100
                charge = max(0.0, min(cfg.max_power_kw - surplus, (target - soc) / eff))
                grid += charge

        # Solar surplus always tops up the battery.
        solar_charge = min(surplus, cfg.max_power_kw, max(0.0, (cap - soc) / eff))
        soc += (solar_charge + charge) * eff
        soc = min(cap, soc)

        price = tariff(cfg, h["hour"])
        totals["grid_kwh"] += grid
        totals["battery_kwh"] += battery_out
        totals["solar_kwh"] += solar_used + solar_charge
        totals["cost"] += grid * price
        totals["baseline_cost"] += demand * price
        totals["co2_kg"] += grid * cfg.grid_co2_kg_per_kwh
        totals["baseline_co2_kg"] += demand * cfg.grid_co2_kg_per_kwh
        steps.append({
            "timestamp": h["timestamp"],
            "soc_pct": round(100 * soc / cap, 2),
            "solar_kw": round(pv, 2),
            "battery_kw": round(battery_out, 2),
            "battery_charge_kw": round(solar_charge + charge, 2),
            "grid_kw": round(grid, 2),
            "battery_shortfall": h["grid_mode"] == MODE_RENEWABLE and grid > 0.5,
        })

    summary = {k: round(v, 2) for k, v in totals.items()}
    summary["cost_saving"] = round(totals["baseline_cost"] - totals["cost"], 2)
    summary["co2_saving_kg"] = round(totals["baseline_co2_kg"] - totals["co2_kg"], 2)
    summary["final_soc_pct"] = steps[-1]["soc_pct"] if steps else cfg.initial_soc_pct
    summary["min_soc_pct"] = min((s["soc_pct"] for s in steps), default=cfg.initial_soc_pct)
    summary["shortfall_hours"] = shortfall_hours
    cfg_out = asdict(cfg)
    cfg_out["grid_charge_hours"] = list(cfg.grid_charge_hours)
    return {"steps": steps, "summary": summary, "config": cfg_out}


def required_capacity(hours, cfg, ceiling_kwh=50000.0):
    """Smallest battery (kWh) that covers every storage-mode hour, other settings fixed.

    Returns None if even the ceiling fails (the power limit is the bottleneck,
    not energy) or 0 if no storage-mode hours need the battery.
    """
    def ok(capacity):
        trial = BatteryConfig(**{**asdict(cfg), "capacity_kwh": capacity})
        return not simulate(hours, trial)["summary"]["shortfall_hours"]

    if ok(1.0):
        return 0.0
    if not ok(ceiling_kwh):
        return None
    lo, hi = 1.0, ceiling_kwh
    while hi - lo > 10:
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    return round(hi, -1)
