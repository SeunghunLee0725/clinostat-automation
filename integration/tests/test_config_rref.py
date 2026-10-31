"""저장소 config.yaml 의 Rref 가 큐브 보드 실물(100 kΩ, 2026-10-31 확인)과 같아야 한다."""

from pathlib import Path

import yaml


def test_config_rref_matches_the_board():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["ble"]["rref_ohm"] == 100000
