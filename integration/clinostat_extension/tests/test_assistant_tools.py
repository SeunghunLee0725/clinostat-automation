"""Claude 읽기 도구용 요약(work/assistant_tools.py) — 실험 상태·기록 디렉터리를 작은 JSON 으로 줄인다."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

WORK = Path(__file__).resolve().parents[1] / "work"
sys.path.insert(0, str(WORK))

import assistant_tools as at  # noqa: E402


def _write_run(runs: Path, run_id: str, *, strategy="constant", verdict="pass", meas_i5=1.2, error=None) -> Path:
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)
    summary = {
        "run_dir": str(run_dir), "strategy": strategy, "params": {"inner_rad_s": 0.5}, "duration_s": 600.0,
        "stopped_by_user": False, "error": error, "finished_at": "2026-09-07T12:56:05",
        "summary": {
            "cmd": {"n": 10596, "I5": 14.6, "D_std": 2.03, "H_norm": 0.9, "chi2": 3.1, "tasmg_G": 0.012,
                    "taspg_error_G": None, "elapsed": 600.0},
            "meas": {"n": 10596, "I5": meas_i5, "D_std": 0.4, "H_norm": 0.95, "chi2": 1.1, "tasmg_G": 0.021,
                     "taspg_error_G": None, "elapsed": 600.0},
            "strategy": strategy, "target_gravity_G": None,
        },
        "verification": {"verdict": verdict, "issues": [] if verdict == "pass" else ["sensor_coverage"],
                         "warnings": [], "stats": {"n": 10596, "duration_s": 600.0, "sensor_coverage": 1.0,
                                                   "big": list(range(500))}},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "ticks.csv").write_text("t\n", encoding="utf-8")
    return run_dir


def test_run_id_pattern_only_accepts_experiment_dirs():
    assert at.RUN_ID_RE.match("experiment_20260907_125605_833427")
    assert not at.RUN_ID_RE.match("../etc/passwd")
    assert not at.RUN_ID_RE.match("experiment_2026")
    assert not at.RUN_ID_RE.match("campaign_20260907.json")


def test_run_brief_keeps_key_metrics_and_drops_bulk(tmp_path):
    run_dir = _write_run(tmp_path, "experiment_20260907_125605_833427", verdict="fail")
    brief = at.run_brief(run_dir)
    assert brief["run_id"] == "experiment_20260907_125605_833427"
    assert brief["strategy"] == "constant" and brief["duration_s"] == 600.0
    assert brief["verdict"] == "fail" and brief["issues"] == ["sensor_coverage"]
    assert brief["meas"]["I5"] == 1.2 and brief["cmd"]["tasmg_G"] == 0.012
    assert "D" not in brief["meas"]
    assert "stats" not in brief and "big" not in json.dumps(brief)
    assert len(json.dumps(brief)) < 1500


def test_run_brief_reports_missing_or_broken_summary(tmp_path):
    empty = tmp_path / "experiment_20260101_000000_000000"
    empty.mkdir()
    brief = at.run_brief(empty)
    assert brief["run_id"] == empty.name and brief["error"]
    (empty / "summary.json").write_text("{not json", encoding="utf-8")
    assert at.run_brief(empty)["error"]


def test_list_recent_runs_is_newest_first_and_limited(tmp_path):
    for i, rid in enumerate(["experiment_20260901_100000_000001", "experiment_20260907_125605_833427",
                             "experiment_20260903_090000_000002"]):
        _write_run(tmp_path, rid, meas_i5=float(i))
    (tmp_path / "campaign_20260907.json").write_text("{}", encoding="utf-8")
    (tmp_path / "run_20260907.json").write_text("{}", encoding="utf-8")
    runs = at.list_recent_runs(tmp_path, limit=2)
    assert [r["run_id"] for r in runs] == ["experiment_20260907_125605_833427", "experiment_20260903_090000_000002"]
    assert at.list_recent_runs(tmp_path, limit=0) == []
    assert len(at.list_recent_runs(tmp_path, limit=50)) == 3
    assert at.list_recent_runs(tmp_path / "missing", limit=5) == []


def test_get_run_summary_refuses_paths_outside_runs(tmp_path):
    _write_run(tmp_path, "experiment_20260907_125605_833427")
    assert at.get_run_summary(tmp_path, "experiment_20260907_125605_833427")["verdict"] == "pass"
    assert at.get_run_summary(tmp_path, "../experiment_x")["error"]
    assert at.get_run_summary(tmp_path, "experiment_20260101_000000_000000")["error"]


def test_experiment_brief_compacts_runner_status():
    status = {"running": True, "run_id": "experiment_20261103_110000_000001", "run_dir": "/x",
              "started_at": "2026-11-03T11:00:00", "finished_at": None, "duration_s": 1200.0,
              "strategy": "fibonacci_lattice_dwell", "params": {"N_points": 500}, "error": None,
              "verification": None, "cultivation_log_ref": {}, "independent_imu_evidence_ref": {}}
    brief = at.experiment_brief(status, now_iso="2026-11-03T11:05:30")
    assert brief["running"] is True and brief["strategy"] == "fibonacci_lattice_dwell"
    assert brief["elapsed_s"] == 330 and brief["remaining_s"] == 870
    assert "run_dir" not in brief and "cultivation_log_ref" not in brief

    idle = at.experiment_brief({"running": False, "run_id": None, "verification": None})
    assert idle["running"] is False and idle["elapsed_s"] is None


def test_strategies_brief_drops_formula_and_keeps_params():
    registry = {"constant": object(), "halton": object()}
    presets = {"constant": {"description": "상수", "params": {"inner_rad_s": 0.5}, "formula": ["x"]}}
    brief = at.strategies_brief(registry, presets, current="halton")
    assert brief["current"] == "halton"
    assert brief["strategies"] == [{"name": "constant", "description": "상수", "params": {"inner_rad_s": 0.5}},
                                   {"name": "halton", "description": "", "params": {}}]
