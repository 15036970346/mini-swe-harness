from compare_history import paired, totals
from harness_support import environment_config


def test_partial_results_keep_fixed_denominator_and_unknowns():
    rows = [
        {
            "instance_id": "a",
            "historical": {"resolved": False},
            "B3": {"resolved": True, "calls": 4, "seconds": 3, "cny_peak": 0.2, "usage_complete": True},
        },
        {
            "instance_id": "b",
            "historical": {"resolved": True},
            "B3": {"resolved": False, "calls": 6, "seconds": 5, "cny_peak": 0.3, "usage_complete": True},
        },
        {
            "instance_id": "c",
            "historical": {"resolved": True},
            "B3": {"resolved": None, "calls": 2, "usage_complete": False},
        },
        {"instance_id": "d", "historical": {"resolved": None}, "B3": {"resolved": None}},
    ]
    summary = totals(rows, "B3")
    assert summary["planned"] == 4 and summary["assessed"] == 2 and summary["resolved"] == 1
    assert summary["cny_peak_recorded"] == 0.5 and summary["incomplete_paid_usage"] == 1
    assert paired(rows, "historical", "B3") == {
        "paired_tasks": 2,
        "both_resolved": 0,
        "gained": ["a"],
        "lost": ["b"],
        "both_failed": 0,
    }


def test_container_resources_match_historical_pilot():
    import yaml
    from harness_support import ROOT

    old = yaml.safe_load((ROOT / "pilot.yaml").read_text())["environment"]
    new = environment_config({"image_name": "pinned-test-image"})
    assert new["run_args"] == old["run_args"]
    assert new["timeout"] == old["timeout"]
    assert new["container_timeout"] == old["container_timeout"]
