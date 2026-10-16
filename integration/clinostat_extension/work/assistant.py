"""음성 비서 백엔드 — 규칙 파서 + 상태 요약 + 온보드 LLM(llama-server) 클라이언트.

역할 분담(안전):
- 장치를 움직이는 명령(펌프 구동·정지, 비상정지, 측정 시작/정지 …)은 **규칙 파서**만 만든다.
  브라우저가 사용자 확인을 받은 뒤 기존 /api/* 를 호출하므로 서버에 새 실행 경로는 없다.
- LLM 은 상황 설명과 자유 질문 응답만 맡는다. 응답은 글자일 뿐 명령으로 해석되지 않는다.
- LLM 은 Tailscale 안의 llama-server(OpenAI 호환 /v1/chat/completions)를 순서대로 시도한다.
  전부 실패하면 `template_summary` 가 같은 상태를 문장으로 만들어 대신한다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

#: 온보드 LLM 후보. 둘 다 `~/llm_onboard/llama.cpp/build/bin/llama-server … --port 8081 --jinja`.
#: dgx-spark 는 GPU 라 1~2 s, kims-pi-2 는 CPU 라 E2B 로도 10 s 안팎이다.
DEFAULT_LLM_HOSTS: tuple[dict[str, Any], ...] = (
    {"name": "dgx-spark", "url": "http://100.90.190.82:8081", "model": "gemma-4-12b-it", "timeout_s": 20.0},
    {"name": "kims-pi-2", "url": "http://100.85.45.76:8081", "model": "gemma-4-E2B-it", "timeout_s": 90.0},
)
PROBE_TIMEOUT_S = 3.0
LLM_MAX_TOKENS = 240
LLM_TEMPERATURE = 0.2

PUMP_IDS = (1, 2)
PUMP_HZ_MIN, PUMP_HZ_MAX = 1, 1000          # 게이트웨이 PUMP_SPM_MAX 60000 과 같은 상한
PUMP_DEFAULT_SEC = 10


# ─────────────────────────── 명령 ───────────────────────────

@dataclass(frozen=True)
class Action:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    requires_confirm: bool = False
    summary: str = ""


def action_to_dict(action: Action) -> dict[str, Any]:
    return asdict(action)


_KO_DIGITS = {"영": 0, "공": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
_KO_UNITS = {"십": 10, "백": 100, "천": 1000}
_KO_NUMERAL_RE = re.compile(r"[영공일이삼사오육칠팔구십백천]+")


def korean_number(text: str) -> int:
    """'삼백오십' → 350, '천' → 1000. 단위 앞에 숫자가 없으면 1 로 본다."""
    total, current = 0, 0
    for ch in text:
        if ch in _KO_DIGITS:
            current = _KO_DIGITS[ch]
        elif ch in _KO_UNITS:
            total += (current or 1) * _KO_UNITS[ch]
            current = 0
    return total + current


def _digitise(text: str) -> str:
    """단위어(헤르츠·초·번) 앞의 한글 숫자를 아라비아 숫자로 바꾼다."""
    def swap(match: re.Match) -> str:
        return str(korean_number(match.group(0)))
    return _KO_NUMERAL_RE.sub(lambda m: swap(m) if _followed_by_unit(text, m) else m.group(0), text)


def _followed_by_unit(text: str, match: re.Match) -> bool:
    tail = text[match.end():match.end() + 4].lstrip()
    return bool(re.match(r"(헤르츠|헤르쯔|hz|㎐|초|번)", tail, re.IGNORECASE))


_HZ = r"(?:헤르츠|헤르쯔|hz|㎐)"
_STOP = r"(?:정지|멈춰|멈춤|스톱|중지|꺼|끄|종료)"
_START = r"(?:시작|개시|켜|가동|돌려|운전|기동)"

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("estop", re.compile(r"비상\s*정지")),
    ("stop_all", re.compile(r"(?:전체|전부|모두|다)\s*(?:장치\s*)?" + _STOP)),
    ("pump_run", re.compile(
        r"펌프\s*(?P<pump>\d)\s*번?\s*(?:을|를|은|는)?\s*(?P<hz>\d+(?:\.\d+)?)\s*" + _HZ
        + r"\s*(?:로|으로)?\s*(?:(?P<sec>\d+)\s*초)?", re.IGNORECASE)),
    ("pump_stop", re.compile(r"펌프\s*(?P<pump>\d)?\s*번?\s*(?:을|를|은|는)?\s*\S*?\s*" + _STOP)),
    ("clinostat_stop", re.compile(r"(?:클리노\S*|회전|모터)\s*(?:을|를)?\s*\S*?\s*" + _STOP)),
    ("clinostat_start", re.compile(r"(?:클리노\S*|회전)\s*(?:을|를)?\s*\S*?\s*" + _START)),
    ("sensor_stop", re.compile(r"(?:센서|측정)\S*\s*\S*?\s*" + _STOP)),
    ("sensor_start", re.compile(r"(?:센서|측정)\S*\s*\S*?\s*" + _START)),
    ("status", re.compile(r"상태|현황|상황|요약|보고|어때")),
)


def parse_command(text: str) -> Optional[Action]:
    """문장 → Action. 규칙에 안 걸리면 None(LLM 이 대화로 받는다)."""
    cleaned = _digitise((text or "").strip())
    if not cleaned:
        return None
    for kind, pattern in _RULES:
        match = pattern.search(cleaned)
        if match:
            return _build(kind, match)
    return None


def _build(kind: str, match: re.Match) -> Action:
    if kind == "pump_run":
        return _pump_run(match)
    if kind == "pump_stop":
        pump = match.group("pump")
        params = {"pump_id": int(pump)} if pump else {}
        target = f"펌프 {pump}" if pump else "모든 펌프"
        return Action("pump_stop", params, False, f"{target} 정지")
    fixed = {
        "estop": ("모든 펌프 비상 정지 (해제 전까지 잠김)", False),
        "stop_all": ("클리노스텟과 모든 펌프 정지", False),
        "clinostat_stop": ("클리노스텟 정지", False),
        "clinostat_start": ("클리노스텟 운전 시작 (안전 확인 필요)", True),
        "sensor_stop": ("저항센서 측정 정지, 세션 저장", False),
        "sensor_start": ("저항센서 측정 시작 (새 세션 열림)", True),
        "status": ("현재 상황 보고", False),
    }
    summary, confirm = fixed[kind]
    return Action(kind, {}, confirm, summary)


def _pump_run(match: re.Match) -> Action:
    pump_id = int(match.group("pump"))
    hz = float(match.group("hz"))
    sec = int(match.group("sec") or PUMP_DEFAULT_SEC)
    if pump_id not in PUMP_IDS:
        return Action("invalid", {}, False, f"펌프 번호는 {PUMP_IDS[0]}~{PUMP_IDS[-1]} 사이여야 합니다")
    if not (PUMP_HZ_MIN <= hz <= PUMP_HZ_MAX):
        return Action("invalid", {}, False, f"주파수는 {PUMP_HZ_MIN}~{PUMP_HZ_MAX} Hz 사이여야 합니다")
    if sec < 1:
        return Action("invalid", {}, False, "구동 시간은 1초 이상이어야 합니다")
    hz_out = int(hz) if hz.is_integer() else hz
    steps = round(hz * sec)
    return Action(
        "pump_run",
        {"pump_id": pump_id, "hz": hz_out, "sec": sec},
        True,
        f"펌프 {pump_id}을(를) {hz_out} Hz(spm {round(hz * 60)})로 {sec}초, {steps} 스텝 구동",
    )


# ─────────────────────────── 상태 → 문맥 ───────────────────────────

def _prefer_actual(actual: Any, commanded: Any) -> Any:
    return actual if actual not in (None, 0) else commanded


def build_context(control: Optional[dict], spacebio: Optional[dict]) -> dict[str, Any]:
    """클리노스텟 상태(/ws 와 같은 dict)와 게이트웨이 상태를 LLM 이 읽을 작은 JSON 으로 합친다."""
    control = control or {}
    metrics = control.get("metrics_meas") or {}
    clinostat = {
        "running": bool(control.get("running")),
        "strategy": control.get("strategy_name") or "",
        "elapsed_s": round(float(control.get("elapsed") or 0)),
        "outer_rpm": _prefer_actual(control.get("outer_actual"), control.get("outer_rpm")),
        "inner_rpm": _prefer_actual(control.get("inner_actual"), control.get("inner_rpm")),
        "tasmg_G": metrics.get("tasmg_G"),
    }
    if not spacebio:
        return {
            "clinostat": clinostat,
            "gateway": {"state": "unreachable"},
            "sensor": {"state": "unknown"},
            "pumps": [],
            "session": {"state": "unknown"},
            "estim": {"state": "not_connected"},
        }
    sensor = spacebio.get("sensor") or {}
    sample = sensor.get("sample") or None
    pumps = spacebio.get("pumps") or ([spacebio["pump"]] if spacebio.get("pump") else [])
    return {
        "clinostat": clinostat,
        "gateway": {"state": (spacebio.get("gateway") or {}).get("state", "unknown")},
        "sensor": {
            "state": sensor.get("state", "unknown"),
            "mode": sensor.get("mode"),
            "rref_ohm": sensor.get("rref_ohm"),
            "sample": sample and {
                "resistance_ohm": sample.get("resistance_ohm"),
                "delta_r_over_r0": sample.get("delta_r_over_r0"),
                "temperature_c": sample.get("temperature_c"),
                "battery_pct": sample.get("battery_pct"),
            },
        },
        "pumps": [
            {
                "pump_id": p.get("pump_id"),
                "state": p.get("state"),
                "mode": p.get("mode"),
                "position_steps": p.get("position_steps"),
                "estop_latched": bool(p.get("estop_latched")),
            }
            for p in pumps
        ],
        "session": {
            "state": (spacebio.get("session") or {}).get("state", "idle"),
            "session_id": (spacebio.get("session") or {}).get("session_id"),
        },
        "estim": {"state": "not_connected"},
    }


def _elapsed_ko(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}시간 {minutes}분"
    return f"{minutes}분" if minutes else f"{rest}초"


def template_summary(ctx: dict[str, Any]) -> str:
    """LLM 없이도 같은 내용을 말할 수 있는 규칙 요약(한국어)."""
    c = ctx["clinostat"]
    if c["running"]:
        name = c["strategy"] or "수동"
        g = c.get("tasmg_G")
        g_text = f", 시간평균 {g:.3f} g" if isinstance(g, (int, float)) else ""
        clin = f"클리노스텟은 {name} 조건으로 {_elapsed_ko(c['elapsed_s'])}째 운전 중입니다{g_text}."
    else:
        clin = "클리노스텟은 정지 상태입니다."

    s = ctx["sensor"]
    sample = s.get("sample")
    if s["state"] in ("running", "recording", "measuring") and sample:
        sens = (f"저항센서는 측정 중이고 ΔR/R₀ {sample['delta_r_over_r0']:.4f}, "
                f"온도 {sample['temperature_c']:.1f}도입니다.")
    elif s["state"] == "unknown":
        sens = "저항센서 상태는 게이트웨이가 응답하지 않아 알 수 없습니다."
    else:
        sens = f"저항센서는 {s['state']} 상태입니다."

    pumps = ctx["pumps"]
    if not pumps:
        pump_text = "펌프 상태는 알 수 없습니다."
    else:
        running = [str(p["pump_id"]) for p in pumps if str(p["state"]).upper() not in ("IDLE", "STOPPED", "OFFLINE")]
        latched = [str(p["pump_id"]) for p in pumps if p["estop_latched"]]
        if running:
            pump_text = f"펌프 {', '.join(running)}이(가) 운전 중이고 나머지는 정지입니다."
        else:
            pump_text = f"펌프 {len(pumps)}대는 모두 정지입니다."
        if latched:
            pump_text += f" 펌프 {', '.join(latched)}에 비상정지가 걸려 있어 해제가 필요합니다."

    estim = "전기신호 장치는 아직 연결 전입니다."
    return " ".join([clin, sens, pump_text, estim])


def system_prompt(ctx: dict[str, Any]) -> str:
    return (
        "당신은 우주바이오 미세중력 실험 관제 콘솔의 음성 비서입니다. "
        "아래 JSON 이 현재 장치 상태입니다. 운영자의 말에 한국어로 2~3문장, 숫자는 단위와 함께 간결히 답하세요. "
        "당신은 장치를 직접 조작할 수 없습니다. 조작 요청이면 화면의 버튼이나 '펌프 1 300헤르츠 10초' 같은 "
        "명령 문장을 쓰라고 안내하세요. 상태에 없는 사실은 지어내지 말고 모르면 모른다고 하세요.\n\n"
        "현재 상태:\n" + json.dumps(ctx, ensure_ascii=False)
    )


# ─────────────────────────── LLM 클라이언트 ───────────────────────────

class LlmUnavailable(RuntimeError):
    """모든 온보드 LLM 호스트가 응답하지 않는다."""


@dataclass(frozen=True)
class LlmReply:
    text: str
    host: str
    model: str
    latency_ms: int


class LlmClient:
    def __init__(self, hosts=DEFAULT_LLM_HOSTS, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._hosts = tuple(dict(h) for h in hosts)
        self._transport = transport

    @property
    def hosts(self) -> tuple[dict[str, Any], ...]:
        return self._hosts

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout_s, transport=self._transport)

    async def chat(self, system: str, user: str) -> LlmReply:
        failures = []
        for host in self._hosts:
            started = time.monotonic()
            try:
                text = await self._chat_once(host, system, user)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                failures.append(f"{host['name']}: {exc.__class__.__name__}")
                continue
            return LlmReply(text, host["name"], host["model"], int((time.monotonic() - started) * 1000))
        raise LlmUnavailable("; ".join(failures) or "no hosts configured")

    async def _chat_once(self, host: dict[str, Any], system: str, user: str) -> str:
        body = {
            "model": host["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": LLM_TEMPERATURE,
            # gemma-4 는 기본으로 '생각'을 reasoning_content 에 쓰고 본문을 비운다(8 s). 끄면 1 s 대.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with self._client(float(host.get("timeout_s", 20.0))) as client:
            response = await client.post(host["url"].rstrip("/") + "/v1/chat/completions", json=body)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"].get("content") or ""
        text = content.strip()
        if not text:
            raise ValueError("empty completion")
        return text

    async def probe(self) -> list[dict[str, Any]]:
        results = []
        for host in self._hosts:
            started = time.monotonic()
            reachable = False
            try:
                async with self._client(PROBE_TIMEOUT_S) as client:
                    response = await client.get(host["url"].rstrip("/") + "/health")
                reachable = response.status_code == 200
            except httpx.HTTPError:
                reachable = False
            results.append({
                "name": host["name"], "model": host["model"], "url": host["url"],
                "reachable": reachable, "latency_ms": int((time.monotonic() - started) * 1000),
            })
        return results
