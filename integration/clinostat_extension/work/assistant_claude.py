"""구독제 Claude 대화 비서 — Claude Code CLI(`claude -p`) 를 서브프로세스로 불러 묻는다.

왜 CLI 인가: API 키(종량제) 대신 운영자의 Claude 구독 로그인을 그대로 쓴다. 파이에서
`claude setup-token` 으로 받은 장기 토큰을 `.env` 의 CLAUDE_CODE_OAUTH_TOKEN 에 두면 된다.

역할 분담(안전, assistant.py 와 같다):
- 서버는 상태 JSON·전략 목록·실험 상태·최근 기록을 **시스템 프롬프트에 넣어** 한 번 묻는다.
  Claude 쪽 도구는 전부 끈다(`--tools ""`). 파일·셸에 손댈 길이 없다.
- 답은 구조화 출력 `{reply, action}` 만 믿는다. reply 는 말풍선·TTS 로만 가고,
  action 은 `build_action` 검증을 거쳐 Action 으로 브라우저에 **돌려줄 뿐** 서버가 실행하지 않는다.
  시작류(실험·회전·펌프·측정)는 운영자 확인, 정지류는 즉시.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

from assistant import PUMP_HZ_MAX, PUMP_HZ_MIN, PUMP_IDS, Action

DEFAULT_BIN = "claude"
DEFAULT_MODEL = "sonnet"
ENV_BIN = "CLINOSTAT_CLAUDE_BIN"
ENV_MODEL = "CLINOSTAT_CLAUDE_MODEL"
ENV_SSH = "CLINOSTAT_CLAUDE_SSH"      # 예: shlee@100.85.45.76 — 다른 기계(Pi 5)의 claude 를 ssh 로 부른다
SSH_OPTS = ("-oBatchMode=yes", "-oConnectTimeout=10", "-oServerAliveInterval=15")
CALL_TIMEOUT_S = 120.0       # 파이는 프로세스 기동만 수 초, 모델 응답까지 10~30 s
PROBE_TIMEOUT_S = 30.0
HISTORY_LIMIT = 20           # 세션당 기억하는 발화 수(운영자+비서)
EXPERIMENT_MAX_S = 7200.0
PUMP_DEFAULT_SEC = 10
_STRATEGY_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "운영자에게 읽어 줄 한국어 1~3문장"},
        "action": {
            "type": ["object", "null"],
            "properties": {"tool": {"type": "string"}, "params": {"type": "object"}},
            "required": ["tool", "params"],
        },
    },
    "required": ["reply", "action"],
}

Runner = Callable[[list[str], float], Awaitable[tuple[int, bytes, bytes]]]


# ─────────────────────────── 환경 ───────────────────────────

def load_env_file(path: Path) -> tuple[str, ...]:
    """KEY=VALUE 줄을 읽어 **아직 없는** 환경변수만 채운다. 값은 로그에 남기지 않는다."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    loaded = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")
            loaded.append(key)
    return tuple(loaded)


# ─────────────────────────── 조작 도구 ───────────────────────────

@dataclass(frozen=True)
class ActSpec:
    kind: str
    requires_confirm: bool
    doc: str
    build: Callable[[dict[str, Any]], Action]


def _invalid(reason: str) -> Action:
    return Action("invalid", {}, False, reason)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _numeric_params(params: Any) -> Optional[dict[str, float]]:
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        return None
    out = {}
    for key, value in params.items():
        number = _num(value)
        if not isinstance(key, str) or number is None:
            return None
        out[key] = number
    return out


def _start_experiment(p: dict[str, Any]) -> Action:
    strategy = p.get("strategy")
    if not isinstance(strategy, str) or not _STRATEGY_RE.match(strategy):
        return _invalid("전략 이름이 올바르지 않습니다 (영문·숫자·밑줄)")
    duration = _num(p.get("duration_s"))
    if duration is None or not (1.0 <= duration <= EXPERIMENT_MAX_S):
        return _invalid(f"실험 시간은 1~{int(EXPERIMENT_MAX_S)} 초 사이여야 합니다")
    params = _numeric_params(p.get("params"))
    if params is None:
        return _invalid("전략 파라미터는 숫자만 가능합니다")
    minutes = duration / 60
    return Action("experiment_run", {"strategy": strategy, "params": params, "duration_s": duration}, True,
                  f"{strategy} 조건으로 {duration:g} s({minutes:.1f}분) 실험 시작" + (f", 파라미터 {params}" if params else ""))


def _set_strategy(p: dict[str, Any]) -> Action:
    name = p.get("name")
    if not isinstance(name, str) or not _STRATEGY_RE.match(name):
        return _invalid("전략 이름이 올바르지 않습니다")
    params = _numeric_params(p.get("params"))
    if params is None:
        return _invalid("전략 파라미터는 숫자만 가능합니다")
    return Action("strategy_set", {"name": name, "params": params}, True, f"운전 조건을 {name} 으로 설정")


def _run_pump(p: dict[str, Any]) -> Action:
    pump = _num(p.get("pump_id"))
    hz = _num(p.get("hz"))
    sec = _num(p.get("sec", PUMP_DEFAULT_SEC))
    if pump is None or int(pump) not in PUMP_IDS:
        return _invalid(f"펌프 번호는 {PUMP_IDS[0]}~{PUMP_IDS[-1]} 사이여야 합니다")
    if hz is None or not (PUMP_HZ_MIN <= hz <= PUMP_HZ_MAX):
        return _invalid(f"주파수는 {PUMP_HZ_MIN}~{PUMP_HZ_MAX} Hz 사이여야 합니다")
    if sec is None or sec < 1:
        return _invalid("구동 시간은 1초 이상이어야 합니다")
    hz_out = int(hz) if hz.is_integer() else hz
    return Action("pump_run", {"pump_id": int(pump), "hz": hz_out, "sec": int(sec)}, True,
                  f"펌프 {int(pump)}을(를) {hz_out} Hz로 {int(sec)}초 구동")


def _stop_pump(p: dict[str, Any]) -> Action:
    pump = p.get("pump_id")
    if pump is None:
        return Action("pump_stop", {}, False, "모든 펌프 정지")
    number = _num(pump)
    if number is None or int(number) not in PUMP_IDS:
        return _invalid(f"펌프 번호는 {PUMP_IDS[0]}~{PUMP_IDS[-1]} 사이여야 합니다")
    return Action("pump_stop", {"pump_id": int(number)}, False, f"펌프 {int(number)} 정지")


def _fixed(kind: str, confirm: bool, summary: str) -> Callable[[dict[str, Any]], Action]:
    return lambda _p: Action(kind, {}, confirm, summary)


ACT_TOOLS: dict[str, ActSpec] = {
    "start_experiment": ActSpec("experiment_run", True,
                                "{strategy: 전략 이름, duration_s: 초(1~7200), params?: {숫자 파라미터}} — 기록되는 실험 실행",
                                _start_experiment),
    "set_strategy": ActSpec("strategy_set", True, "{name: 전략 이름, params?: {숫자 파라미터}} — 실행 없이 운전 조건만 설정",
                            _set_strategy),
    "start_clinostat": ActSpec("clinostat_start", True, "{} — 현재 설정된 조건으로 회전 시작(기록 없음)",
                               _fixed("clinostat_start", True, "클리노스텟 운전 시작 (안전 확인 필요)")),
    "run_pump": ActSpec("pump_run", True, "{pump_id: 1|2, hz: 1~1000, sec?: 초(기본 10)} — 펌프 구동", _run_pump),
    "start_sensor": ActSpec("sensor_start", True, "{} — 저항센서 측정 시작(새 세션)",
                            _fixed("sensor_start", True, "저항센서 측정 시작 (새 세션 열림)")),
    "stop_experiment": ActSpec("experiment_stop", False, "{} — 실험 중단(요약 저장)",
                               _fixed("experiment_stop", False, "실험 중단")),
    "stop_clinostat": ActSpec("clinostat_stop", False, "{} — 회전 정지", _fixed("clinostat_stop", False, "클리노스텟 정지")),
    "stop_pump": ActSpec("pump_stop", False, "{pump_id?: 1|2} — 펌프 정지(번호 없으면 전부)", _stop_pump),
    "stop_all": ActSpec("stop_all", False, "{} — 클리노스텟과 모든 펌프 정지", _fixed("stop_all", False, "클리노스텟과 모든 펌프 정지")),
    "emergency_stop": ActSpec("estop", False, "{} — 펌프 비상 정지(해제 전까지 잠김)",
                              _fixed("estop", False, "모든 펌프 비상 정지 (해제 전까지 잠김)")),
    "stop_sensor": ActSpec("sensor_stop", False, "{} — 측정 정지, 세션 저장",
                           _fixed("sensor_stop", False, "저항센서 측정 정지, 세션 저장")),
}


def build_action(name: Any, params: Any) -> Action:
    spec = ACT_TOOLS.get(name) if isinstance(name, str) else None
    if spec is None:
        return _invalid(f"알 수 없는 조작: {name}")
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        return _invalid("조작 파라미터 형식이 올바르지 않습니다")
    return spec.build(dict(params))


def tools_doc() -> str:
    lines = []
    for name, spec in ACT_TOOLS.items():
        gate = "운영자 확인 후 실행" if spec.requires_confirm else "즉시 실행"
        lines.append(f"- {name} {spec.doc} [{gate}]")
    return "\n".join(lines)


# ─────────────────────────── 프롬프트 ───────────────────────────

def system_prompt(ctx: Mapping[str, Any], *, now: str | None = None, strategies: Mapping[str, Any] | None = None,
                  experiment: Mapping[str, Any] | None = None, recent_runs: list | None = None) -> str:
    now_text = now or time.strftime("%Y-%m-%d %H:%M")
    parts = [
        "당신은 우주바이오 미세중력 실험(2축 클리노스텟 + 펌프 + 저항센서) 관제 콘솔의 음성 비서입니다. "
        "운영자가 말로 묻고 지시하면 한국어로 1~3문장, 숫자는 단위와 함께 간결히 답합니다. 답은 음성으로 읽힙니다. "
        "그러니 읽기 쉬운 구어체로 쓰고, 영문 상태어는 한국어로 풀어 말하세요(degraded→저하, fault→고장, unreachable→연결 안 됨, rpm→알피엠). 괄호·기호·목록·마크다운은 쓰지 마세요.",
        "규칙:\n"
        "1. 장치를 움직이는 요청이면 reply 에 무엇을 할지 말하고 action 에 아래 조작 하나를 넣습니다. 한 번에 하나만.\n"
        "2. 당신은 직접 조작하지 못합니다. '확인 후 실행' 조작은 운영자가 화면의 [실행]을 눌러야 보내지므로 "
        "실행됐다고 단정하지 말고 눌러 달라고 하세요. 즉시 실행 조작은 화면이 바로 보냅니다.\n"
        "3. 실행 결과는 '[실행 결과] …' 메시지로 돌아옵니다. 그러면 한 문장으로 확인만 하세요.\n"
        "4. 아래 상태에 없는 사실은 지어내지 말고 모르면 모른다고 하세요. 상태가 unreachable/degraded 면 그 점을 먼저 말하세요.\n"
        "5. 실험 시간을 말하지 않으면 전략 설명의 권장 시간을 쓰고, 없으면 600초를 제안하되 action 에 넣기 전에 되묻지 말고 reply 에 밝히세요.\n"
        "6. 조작 요청이 아니면 action 은 null.",
        "사용 가능한 조작(action.tool / params):\n" + tools_doc(),
        f"현재 시각: {now_text}",
        "현재 장치 상태(JSON):\n" + json.dumps(ctx, ensure_ascii=False),
    ]
    if strategies is not None:
        parts.append("등록된 운전 전략(JSON):\n" + json.dumps(strategies, ensure_ascii=False))
    if experiment is not None:
        parts.append("실험 러너 상태(JSON):\n" + json.dumps(experiment, ensure_ascii=False))
    if recent_runs is not None:
        parts.append("최근 실험 기록(최신순, JSON):\n" + json.dumps(recent_runs, ensure_ascii=False))
    return "\n\n".join(parts)


def render_prompt(history: tuple[dict[str, Any], ...], text: str) -> str:
    if not history:
        return f"운영자: {text}"
    lines = ["[지금까지의 대화]"]
    for turn in history:
        who = "운영자" if turn["role"] == "user" else "비서"
        lines.append(f"{who}: {turn['text']}")
    lines.append("")
    lines.append(f"[새 메시지]\n운영자: {text}")
    return "\n".join(lines)


# ─────────────────────────── CLI 클라이언트 ───────────────────────────

class ClaudeUnavailable(RuntimeError):
    """claude CLI 가 없거나, 로그인이 안 됐거나, 응답을 못 만들었다."""


@dataclass(frozen=True)
class AskResult:
    structured: dict[str, Any]
    latency_ms: int
    cost_usd: float | None


async def _subprocess_runner(argv: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
        cwd=_work_dir(), env=os.environ.copy())
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"claude timeout after {timeout_s:.0f}s") from exc
    return proc.returncode or 0, stdout, stderr


def _work_dir() -> str:
    """CLI 작업 디렉터리 — 프로젝트 메모리·설정이 없는 빈 폴더."""
    path = Path(tempfile.gettempdir()) / "clinostat-assistant"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


class ClaudeCliClient:
    def __init__(self, bin_path: str, model: str = DEFAULT_MODEL, runner: Runner | None = None,
                 timeout_s: float = CALL_TIMEOUT_S, ssh_target: str | None = None):
        self.bin_path = bin_path
        self.model = model
        self.ssh_target = ssh_target
        self._runner = runner or _subprocess_runner
        self._timeout_s = timeout_s

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ,
                 which: Callable[[str], Optional[str]] = shutil.which, runner: Runner | None = None) -> Optional["ClaudeCliClient"]:
        model = environ.get(ENV_MODEL) or DEFAULT_MODEL
        ssh_target = (environ.get(ENV_SSH) or "").strip()
        if ssh_target:
            # 원격 기계의 claude — 로그인 쉘이 아니라 PATH 가 짧으므로 절대경로를 기본으로 둔다
            return cls(environ.get(ENV_BIN) or "~/.local/bin/claude", model, runner=runner, ssh_target=ssh_target)
        candidates = [environ.get(ENV_BIN) or DEFAULT_BIN, str(Path.home() / ".local" / "bin" / "claude")]
        for candidate in candidates:
            found = which(candidate) or (candidate if candidate.startswith("/") and Path(candidate).is_file() else None)
            if found:
                return cls(found, model, runner=runner)
        return None

    def _wrap(self, argv: list[str]) -> list[str]:
        """ssh 대상이 있으면 원격 셸이 그대로 받도록 한 문자열로 인용한다. 첫 토큰(경로)의 ~ 만 셸이 펼친다."""
        if not self.ssh_target:
            return argv
        remote = argv[0] + " " + " ".join(shlex.quote(a) for a in argv[1:])
        return ["ssh", *SSH_OPTS, self.ssh_target, remote]

    def argv(self, system: str, prompt: str) -> list[str]:
        return self._wrap(self._local_argv(system, prompt))

    def _local_argv(self, system: str, prompt: str) -> list[str]:
        return [
            self.bin_path, "-p", prompt,
            "--output-format", "json",
            "--json-schema", json.dumps(OUTPUT_SCHEMA, ensure_ascii=False),
            "--system-prompt", system,
            "--model", self.model,
            "--tools", "",
            "--max-turns", "1",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--exclude-dynamic-system-prompt-sections",
        ]

    async def ask(self, system: str, prompt: str) -> AskResult:
        started = time.monotonic()
        try:
            rc, stdout, stderr = await self._runner(self.argv(system, prompt), self._timeout_s)
        except (OSError, TimeoutError) as exc:
            raise ClaudeUnavailable(f"claude 실행 실패: {exc}") from exc
        latency = int((time.monotonic() - started) * 1000)
        if rc != 0:
            raise ClaudeUnavailable(f"claude 종료 코드 {rc}: {_tail(stderr) or _tail(stdout)}")
        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
        except ValueError as exc:
            raise ClaudeUnavailable(f"claude 출력이 JSON 이 아님: {_tail(stdout)}") from exc
        if not isinstance(data, dict) or data.get("is_error") or data.get("subtype") not in (None, "success"):
            raise ClaudeUnavailable(f"claude 오류({data.get('subtype') if isinstance(data, dict) else '?'}): "
                                    f"{str(data.get('result') if isinstance(data, dict) else data)[:300]}")
        structured = _structured(data)
        if structured is None:
            raise ClaudeUnavailable("claude 가 structured output 을 돌려주지 않음")
        return AskResult(structured, latency, data.get("total_cost_usd"))

    async def probe(self) -> dict[str, Any]:
        started = time.monotonic()
        base = {"configured": True, "bin": self.bin_path, "model": self.model, "ssh": self.ssh_target,
                "logged_in": False, "auth_method": None, "error": None}
        try:
            rc, stdout, stderr = await self._runner(self._wrap([self.bin_path, "auth", "status"]), PROBE_TIMEOUT_S)
        except (OSError, TimeoutError) as exc:
            return {**base, "error": f"claude 실행 실패: {exc}", "latency_ms": _ms(started)}
        if rc != 0:
            return {**base, "error": _tail(stderr) or _tail(stdout) or f"exit {rc}", "latency_ms": _ms(started)}
        try:
            status = json.loads(stdout.decode("utf-8", "replace"))
        except ValueError:
            return {**base, "error": "auth status 출력이 JSON 이 아님", "latency_ms": _ms(started)}
        logged_in = bool(status.get("loggedIn"))
        return {**base, "logged_in": logged_in, "auth_method": status.get("authMethod"),
                "error": None if logged_in else "로그인 안 됨 — claude 가 있는 기계에서 `claude auth login` 실행",
                "latency_ms": _ms(started)}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _tail(raw: bytes, limit: int = 300) -> str:
    return raw.decode("utf-8", "replace").strip()[-limit:]


def _structured(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    out = data.get("structured_output")
    if isinstance(out, dict) and "reply" in out:
        return out
    result = data.get("result")
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            return None
        if isinstance(parsed, dict) and "reply" in parsed:
            return parsed
    return None


# ─────────────────────────── 세션 · 대화 ───────────────────────────

def trim_history(history: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    if len(history) <= HISTORY_LIMIT:
        return tuple(history)
    kept = tuple(history[-HISTORY_LIMIT:])
    while kept and kept[0]["role"] != "user":
        kept = kept[1:]
    return kept


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, tuple[dict[str, Any], ...]] = {}

    def get(self, session_id: str) -> tuple[dict[str, Any], ...]:
        return self._sessions.get(session_id, ())

    def put(self, session_id: str, history: tuple[dict[str, Any], ...]) -> None:
        self._sessions = {**self._sessions, session_id: trim_history(history)}


@dataclass(frozen=True)
class ChatResult:
    reply: str
    action: Optional[Action]
    model: str
    latency_ms: int
    cost_usd: float | None


class ClaudeAssistant:
    def __init__(self, client: ClaudeCliClient, store: SessionStore | None = None):
        self.client = client
        self.store = store or SessionStore()

    @classmethod
    def from_env(cls) -> Optional["ClaudeAssistant"]:
        client = ClaudeCliClient.from_env()
        return cls(client) if client else None

    async def chat(self, session_id: str, text: str, system: str) -> ChatResult:
        history = self.store.get(session_id)
        asked = await self.client.ask(system, render_prompt(history, text))
        reply, action, raw = _interpret(asked.structured)
        note = f" [조작 요청: {raw['tool']} {json.dumps(raw['params'], ensure_ascii=False)}]" if raw else ""
        self.store.put(session_id, history + ({"role": "user", "text": text},
                                              {"role": "assistant", "text": reply + note}))
        return ChatResult(reply, action, self.client.model, asked.latency_ms, asked.cost_usd)


def _interpret(structured: Mapping[str, Any]) -> tuple[str, Optional[Action], Optional[dict[str, Any]]]:
    reply = str(structured.get("reply") or "").strip() or "(응답 없음)"
    raw = structured.get("action")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("tool"), str):
        return reply, None, None
    action = build_action(raw["tool"], raw.get("params"))
    raw_out = {"tool": raw["tool"], "params": raw.get("params") or {}}
    if action.kind == "invalid":
        return f"{reply} (요청을 보내지 않았습니다: {action.summary})", None, raw_out
    return reply, action, raw_out
