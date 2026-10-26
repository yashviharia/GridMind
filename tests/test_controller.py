"""Spec conformance for the transition controller (problem statement, section 6)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "appsail"))
from gridcore import controller  # noqa: E402

R, G = controller.MODE_RENEWABLE, controller.MODE_GRID


@pytest.mark.parametrize(
    "hour, demand, mode, overload",
    [
        # Demand threshold is strictly greater than 160.
        (3, 160.0, G, False),
        (3, 160.01, R, False),
        (12, 159.99, G, False),
        # Peak window 18:00-22:00 is inclusive at both ends.
        (17, 100.0, G, False),
        (18, 100.0, R, False),
        (22, 100.0, R, False),
        (23, 100.0, G, False),
        # Overload is >= 200, independent of grid mode.
        (12, 199.99, R, False),
        (12, 200.0, R, True),
        (20, 250.0, R, True),
        (0, 0.0, G, False),
    ],
)
def test_rules(hour, demand, mode, overload):
    s = controller.evaluate(hour, demand)
    assert s["grid_mode"] == mode
    assert s["overload_alert"] is overload


def test_all_hours_low_demand():
    modes = [controller.evaluate(h, 50.0)["grid_mode"] for h in range(24)]
    assert [h for h, m in enumerate(modes) if m == R] == [18, 19, 20, 21, 22]


def test_transitions_only_on_change():
    hours = [{"timestamp": f"t{h}", **controller.evaluate(h, 50.0)} for h in range(24)]
    t = controller.transitions(hours)
    assert [(x["timestamp"], x["to"]) for x in t] == [("t0", G), ("t18", R), ("t23", G)]
