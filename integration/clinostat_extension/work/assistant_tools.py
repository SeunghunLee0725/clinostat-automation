"""Claude 비서가 읽을 상태 요약 — 실험 러너 상태와 runs/ 기록을 작은 JSON 으로 줄인다.

하드웨어를 건드리지 않는 순수 함수만 있다. server.py 가 시스템 프롬프트를 만들 때 쓴다.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

RUN_ID_RE = re.compile(r"^experiment_\d{8}_\d{6}_\d{6}$")
METRIC_KEYS = ("n", "I5", "D_std", "H_norm", "chi2", "tasmg_G", "taspg_error_G", "elapsed")


def _metrics(block: Any) -> dict[str, Any]:
    if not isinstance(block, Mapping):
        return {}
    return {k: block.get(k) for k in METRIC_KEYS if k in block}


def run_brief(run_dir: Path) -> dict[str, Any]:
    """runs/<run_id>/summary.json → 핵심 지표만. 없거나 깨졌으면 error 로 알린다."""
    run_id = run_dir.name
    path = run_dir / "summary.json"
    if not path.is_file():
        return {"run_id": run_id, "error": "summary.json 없음 (실행 중이거나 비정상 종료)"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"run_id": run_id, "error": f"summary.json 읽기 실패: {exc.__class__.__name__}"}
    summary = data.get("summary") if isinstance(data.get("summary"), Mapping) else {}
    verification = data.get("verification") if isinstance(data.get("verification"), Mapping) else {}
    return {
        "run_id": run_id,
        "strategy": data.get("strategy"),
        "params": data.get("params") or {},
        "duration_s": data.get("duration_s"),
        "finished_at": data.get("finished_at"),
        "stopped_by_user": bool(data.get("stopped_by_user")),
        "error": data.get("error"),
        "verdict": verification.get("verdict"),
        "issues": list(verification.get("issues") or []),
        "warnings": list(verification.get("warnings") or []),
        "target_gravity_G": summary.get("target_gravity_G"),
        "cmd": _metrics(summary.get("cmd")),
        "meas": _metrics(summary.get("meas")),
    }


def list_recent_runs(runs_dir: Path, limit: int = 5) -> list[dict[str, Any]]:
    """최신순 실험 요약. 실험 디렉터리(experiment_*)만 본다."""
    if limit <= 0 or not runs_dir.is_dir():
        return []
    names = sorted((p.name for p in runs_dir.iterdir() if p.is_dir() and RUN_ID_RE.match(p.name)), reverse=True)
    return [run_brief(runs_dir / name) for name in names[:limit]]


def get_run_summary(runs_dir: Path, run_id: str) -> dict[str, Any]:
    """run_id 로 한 건. 디렉터리 이름 규칙에 안 맞으면 경로를 만들지도 않는다."""
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        return {"run_id": str(run_id), "error": "run_id 형식이 아님 (experiment_YYYYMMDD_HHMMSS_ffffff)"}
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        return {"run_id": run_id, "error": "그런 실험 기록이 없음"}
    return run_brief(run_dir)


def experiment_brief(status: Mapping[str, Any], now_iso: str | None = None) -> dict[str, Any]:
    """ExperimentRunner.status() → 경과·잔여 시간을 더한 작은 dict."""
    running = bool(status.get("running"))
    started = status.get("started_at")
    duration = status.get("duration_s")
    elapsed: int | None = None
    remaining: int | None = None
    if running and started:
        try:
            now = datetime.fromisoformat(now_iso) if now_iso else datetime.now()
            elapsed = int((now - datetime.fromisoformat(started)).total_seconds())
            if isinstance(duration, (int, float)):
                remaining = max(0, int(duration) - elapsed)
        except ValueError:
            elapsed = None
    verification = status.get("verification") if isinstance(status.get("verification"), Mapping) else None
    return {
        "running": running,
        "run_id": status.get("run_id"),
        "strategy": status.get("strategy"),
        "params": status.get("params") or {},
        "duration_s": duration,
        "started_at": started,
        "finished_at": status.get("finished_at"),
        "elapsed_s": elapsed,
        "remaining_s": remaining,
        "error": status.get("error"),
        "verdict": verification.get("verdict") if verification else None,
        "issues": list(verification.get("issues") or []) if verification else [],
    }


def strategies_brief(registry: Mapping[str, Any], presets: Mapping[str, Any], current: str | None) -> dict[str, Any]:
    """/api/strategies 와 같은 목록에서 수식은 빼고 이름·설명·기본 파라미터만."""
    items = []
    for name in registry.keys():
        preset = presets.get(name, {}) if isinstance(presets.get(name), Mapping) else {}
        items.append({"name": name, "description": preset.get("description", ""),
                      "params": dict(preset.get("params", {}) or {})})
    return {"current": current or "", "strategies": items}


def tailscale_https_url() -> str | None:
    """`tailscale status --json` 의 CertDomains[0] → https 주소. Tailscale 이 없거나 HTTPS 미설정이면 None.

    Chrome 은 HTTP(비보안 출처)에서 마이크를 막으므로 음성 비서 위젯이 이 주소를 안내한다.
    """
    try:
        done = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        domains = json.loads(done.stdout).get("CertDomains")
    except (ValueError, AttributeError):
        return None
    if not isinstance(domains, list) or not domains or not isinstance(domains[0], str):
        return None
    return "https://" + domains[0]
