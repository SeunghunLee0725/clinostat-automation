"""구독제 Claude 대화 비서(work/assistant_claude.py) — `claude -p` 서브프로세스 호출, 확인 정책, 세션.

설계 원칙(기존 assistant.py 와 같다):
- Claude 는 API 키가 아니라 **Claude Code CLI(구독 로그인)** 로 부른다. 서버는 argv 를 만들고
  structured_output(JSON) 만 믿는다. 글(reply)은 말풍선·TTS 로만 간다.
- 장치를 움직이는 조작은 Action 객체로 브라우저에 돌려줄 뿐, 서버가 실행하지 않는다.
  시작류는 운영자 확인(requires_confirm), 정지류는 즉시.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

WORK = Path(__file__).resolve().parents[1] / "work"
sys.path.insert(0, str(WORK))

import assistant_claude as ac  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ─────────────────────────── 가짜 CLI ───────────────────────────

def _cli_json(structured, *, subtype="success", is_error=False, result=None, cost=0.01, api_ms=1500):
    body = {"type": "result", "subtype": subtype, "is_error": is_error, "session_id": "sess",
            "result": result if result is not None else json.dumps(structured, ensure_ascii=False),
            "total_cost_usd": cost, "duration_ms": api_ms + 1000, "duration_api_ms": api_ms,
            "usage": {"input_tokens": 3, "output_tokens": 40}}
    if structured is not None:
        body["structured_output"] = structured
    return json.dumps(body, ensure_ascii=False).encode()


def _runner(outcomes: list, seen: list | None = None):
    """호출마다 outcomes 를 순서대로 — (rc, stdout, stderr) 튜플 또는 예외."""
    queue = list(outcomes)

    async def run(argv, timeout_s):
        if seen is not None:
            seen.append(list(argv))
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return run


def _client(outcomes, seen=None, model="sonnet"):
    return ac.ClaudeCliClient("/usr/bin/claude", model=model, runner=_runner(outcomes, seen))


def _assistant(outcomes, seen=None, store=None):
    return ac.ClaudeAssistant(_client(outcomes, seen), store=store or ac.SessionStore())


CTX = {"clinostat": {"running": False, "strategy": "", "elapsed_s": 0, "outer_rpm": 0, "inner_rpm": 0,
                     "tasmg_G": None},
       "gateway": {"state": "ok"}, "sensor": {"state": "idle"}, "pumps": [], "session": {"state": "idle"},
       "estim": {"state": "not_connected"}}
SYSTEM = ac.system_prompt(CTX, now="2026-11-03 11:00")


# ─────────────────────────── 환경 파일 · 생성 ───────────────────────────

def test_load_env_file_sets_missing_keys_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# 주석\nCLAUDE_CODE_OAUTH_TOKEN=tok-file\nCLINOSTAT_CLAUDE_MODEL=opus\n\nBROKEN LINE\n",
                   encoding="utf-8")
    monkeypatch.setenv("CLINOSTAT_CLAUDE_MODEL", "sonnet-from-shell")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert ac.load_env_file(env) == ("CLAUDE_CODE_OAUTH_TOKEN",)
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-file"
    assert os.environ["CLINOSTAT_CLAUDE_MODEL"] == "sonnet-from-shell"   # 셸 값이 이긴다


def test_load_env_file_is_silent_when_missing(tmp_path):
    assert ac.load_env_file(tmp_path / "nope.env") == ()


def test_from_env_needs_a_cli_binary():
    assert ac.ClaudeCliClient.from_env(environ={}, which=lambda name: None) is None
    client = ac.ClaudeCliClient.from_env(environ={"CLINOSTAT_CLAUDE_MODEL": "opus"},
                                         which=lambda name: "/opt/bin/claude")
    assert client is not None and client.model == "opus" and client.bin_path == "/opt/bin/claude"
    client = ac.ClaudeCliClient.from_env(environ={"CLINOSTAT_CLAUDE_BIN": "/home/pi/.local/bin/claude"},
                                         which=lambda name: name if name.startswith("/") else None)
    assert client.bin_path == "/home/pi/.local/bin/claude" and client.model == ac.DEFAULT_MODEL


# ─────────────────────────── 조작 도구 정의 ───────────────────────────

def test_start_tools_need_confirmation_and_stop_tools_do_not():
    assert ac.ACT_TOOLS
    for name, spec in ac.ACT_TOOLS.items():
        if name.startswith("stop") or name == "emergency_stop":
            assert spec.requires_confirm is False, name
        else:
            assert spec.requires_confirm is True, name


def test_tools_doc_lists_every_tool_for_the_prompt():
    doc = ac.tools_doc()
    for name in ac.ACT_TOOLS:
        assert name in doc
    assert "pump_id" in doc and "duration_s" in doc


@pytest.mark.parametrize("name,params,kind,confirm", [
    ("start_experiment", {"strategy": "fibonacci_lattice_dwell", "duration_s": 600}, "experiment_run", True),
    ("set_strategy", {"name": "adaptive_blend", "params": {"alpha": 0.7}}, "strategy_set", True),
    ("start_clinostat", {}, "clinostat_start", True),
    ("run_pump", {"pump_id": 1, "hz": 300, "sec": 10}, "pump_run", True),
    ("run_pump", {"pump_id": 2, "hz": 100}, "pump_run", True),
    ("start_sensor", {}, "sensor_start", True),
    ("stop_experiment", {}, "experiment_stop", False),
    ("stop_clinostat", {}, "clinostat_stop", False),
    ("stop_pump", {"pump_id": 2}, "pump_stop", False),
    ("stop_pump", {}, "pump_stop", False),
    ("stop_all", {}, "stop_all", False),
    ("emergency_stop", {}, "estop", False),
    ("stop_sensor", {}, "sensor_stop", False),
])
def test_build_action_maps_tool_to_console_action(name, params, kind, confirm):
    action = ac.build_action(name, params)
    assert action.kind == kind and action.requires_confirm is confirm
    assert action.summary


def test_start_experiment_action_carries_strategy_params_and_duration():
    action = ac.build_action("start_experiment", {"strategy": "constant", "params": {"inner_rad_s": 0.3},
                                                  "duration_s": 120})
    assert action.params == {"strategy": "constant", "params": {"inner_rad_s": 0.3}, "duration_s": 120.0}
    assert "constant" in action.summary and "120" in action.summary


@pytest.mark.parametrize("name,params", [
    ("start_experiment", {"strategy": "", "duration_s": 60}),
    ("start_experiment", {"strategy": "../x", "duration_s": 60}),
    ("start_experiment", {"strategy": "constant", "duration_s": 0}),
    ("start_experiment", {"strategy": "constant", "duration_s": 99999}),
    ("start_experiment", {"strategy": "constant", "duration_s": 60, "params": {"w": "abc"}}),
    ("run_pump", {"pump_id": 3, "hz": 100, "sec": 5}),
    ("run_pump", {"pump_id": 1, "hz": 5000, "sec": 5}),
    ("stop_pump", {"pump_id": 9}),
    ("no_such_tool", {}),
    ("run_pump", "not a dict"),
])
def test_build_action_rejects_bad_input(name, params):
    action = ac.build_action(name, params)
    assert action.kind == "invalid" and action.requires_confirm is False


# ─────────────────────────── CLI 호출 ───────────────────────────

def test_argv_is_headless_toolless_and_structured():
    argv = _client([]).argv("SYS", "PROMPT")
    assert argv[0] == "/usr/bin/claude"
    assert "-p" in argv and argv[argv.index("-p") + 1] == "PROMPT"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--max-turns") + 1] == "1"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert set(schema["required"]) == {"reply", "action"}


def test_ssh_target_wraps_the_whole_command_for_the_remote_shell():
    client = ac.ClaudeCliClient("~/.local/bin/claude", model="sonnet", runner=_runner([]), ssh_target="shlee@100.85.45.76")
    argv = client.argv("SYS 'quoted'", "펌프 1 \"300\" 헤르츠")
    assert argv[0] == "ssh" and "shlee@100.85.45.76" in argv and "-oBatchMode=yes" in argv
    remote = argv[-1]
    assert remote.startswith("~/.local/bin/claude -p ")
    assert "'펌프 1 \"300\" 헤르츠'" in remote and "'SYS '\"'\"'quoted'\"'\"''" in remote
    assert "--tools ''" in remote


def test_from_env_prefers_ssh_target_over_a_local_binary():
    client = ac.ClaudeCliClient.from_env(environ={"CLINOSTAT_CLAUDE_SSH": "shlee@pi5"}, which=lambda name: None)
    assert client is not None and client.ssh_target == "shlee@pi5" and client.bin_path == "~/.local/bin/claude"
    client = ac.ClaudeCliClient.from_env(environ={"CLINOSTAT_CLAUDE_SSH": "shlee@pi5", "CLINOSTAT_CLAUDE_BIN": "/usr/bin/claude"},
                                         which=lambda name: None)
    assert client.bin_path == "/usr/bin/claude"


async def test_probe_goes_through_ssh_too():
    seen = []
    client = ac.ClaudeCliClient("~/.local/bin/claude", runner=_runner([(0, b'{"loggedIn": true}', b"")], seen), ssh_target="shlee@pi5")
    probe = await client.probe()
    assert seen[0][0] == "ssh" and seen[0][-1] == "~/.local/bin/claude auth status"
    assert probe["logged_in"] is True and probe["ssh"] == "shlee@pi5"


async def test_ask_returns_structured_output_and_timing():
    seen = []
    client = _client([(0, _cli_json({"reply": "정지 상태입니다.", "action": None}, cost=0.02, api_ms=1800), b"")], seen)
    out = await client.ask("SYS", "지금 어때")
    assert out.structured == {"reply": "정지 상태입니다.", "action": None}
    assert out.cost_usd == 0.02 and out.latency_ms >= 0
    assert seen[0][argv_index(seen[0], "-p") + 1] == "지금 어때"


def argv_index(argv, flag):
    return argv.index(flag)


async def test_ask_falls_back_to_parsing_result_text():
    client = _client([(0, _cli_json(None, result='{"reply":"네","action":null}'), b"")])
    out = await client.ask("SYS", "x")
    assert out.structured == {"reply": "네", "action": None}


@pytest.mark.parametrize("outcome,needle", [
    ((1, b"", b"Not logged in. Please run /login"), "Not logged in"),
    ((0, _cli_json(None, subtype="error_during_execution", is_error=True, result="rate limit"), b""), "rate limit"),
    ((0, b"this is not json", b""), "JSON"),
    ((0, _cli_json(None, result="그냥 글"), b""), "structured"),
    (TimeoutError("timeout"), "timeout"),
    (FileNotFoundError("claude"), "claude"),
])
async def test_ask_raises_claude_unavailable_with_a_reason(outcome, needle):
    with pytest.raises(ac.ClaudeUnavailable) as info:
        await _client([outcome]).ask("SYS", "x")
    assert needle.lower() in str(info.value).lower()


async def test_probe_uses_auth_status_not_a_model_call():
    seen = []
    status = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "x@y"}).encode()
    probe = await _client([(0, status, b"")], seen).probe()
    assert seen[0][1:3] == ["auth", "status"]
    assert probe == {**probe, "configured": True, "logged_in": True, "auth_method": "claude.ai", "model": "sonnet"}
    assert probe["error"] is None

    probe = await _client([(1, b"", b"not logged in")]).probe()
    assert probe["logged_in"] is False and probe["error"]
    probe = await _client([FileNotFoundError("claude")]).probe()
    assert probe["configured"] is True and probe["logged_in"] is False and "claude" in probe["error"]


# ─────────────────────────── 대화 루프 ───────────────────────────

async def test_plain_reply_has_no_action():
    result = await _assistant([(0, _cli_json({"reply": "정지 상태입니다.", "action": None}), b"")]).chat("s1", "어때", SYSTEM)
    assert result.reply == "정지 상태입니다." and result.action is None
    assert result.model == "sonnet" and result.latency_ms >= 0


async def test_prompt_carries_history_and_new_message():
    seen = []
    store = ac.SessionStore()
    assistant = _assistant([(0, _cli_json({"reply": "하나", "action": None}), b""),
                            (0, _cli_json({"reply": "둘", "action": None}), b"")], seen, store)
    await assistant.chat("s1", "첫째", SYSTEM)
    await assistant.chat("s1", "둘째", SYSTEM)
    prompt = seen[1][seen[1].index("-p") + 1]
    assert prompt.index("첫째") < prompt.index("하나") < prompt.index("둘째")
    assert "둘" not in prompt.split("둘째")[-1]        # 새 답은 아직 프롬프트에 없다
    assert [m["role"] for m in store.get("s1")] == ["user", "assistant", "user", "assistant"]
    assert store.get("other") == ()


async def test_act_action_is_returned_with_confirmation_and_remembered():
    seen = []
    store = ac.SessionStore()
    assistant = _assistant([(0, _cli_json({"reply": "5분 실험을 준비했습니다. 실행을 눌러 주세요.",
                                           "action": {"tool": "start_experiment",
                                                      "params": {"strategy": "constant", "duration_s": 300}}}), b""),
                            (0, _cli_json({"reply": "시작됐습니다.", "action": None}), b"")], seen, store)
    result = await assistant.chat("s1", "constant 로 5분 돌려", SYSTEM)
    assert result.action.kind == "experiment_run" and result.action.requires_confirm is True
    assert result.action.params["duration_s"] == 300.0
    assert "실행을 눌러" in result.reply
    await assistant.chat("s1", "[실행 결과] constant 실험 시작 → 성공", SYSTEM)
    prompt = seen[1][seen[1].index("-p") + 1]
    assert "start_experiment" in prompt and "[실행 결과]" in prompt


async def test_stop_action_is_immediate():
    result = await _assistant([(0, _cli_json({"reply": "전부 정지합니다.", "action": {"tool": "stop_all", "params": {}}}), b"")]).chat("s1", "다 멈춰", SYSTEM)
    assert result.action.kind == "stop_all" and result.action.requires_confirm is False


async def test_invalid_action_is_dropped_and_reply_says_why():
    result = await _assistant([(0, _cli_json({"reply": "돌리겠습니다.", "action": {"tool": "run_pump", "params": {"pump_id": 7, "hz": 100}}}), b"")]).chat("s1", "펌프 7", SYSTEM)
    assert result.action is None
    assert "펌프 번호" in result.reply or "1~2" in result.reply


async def test_malformed_action_object_is_ignored():
    result = await _assistant([(0, _cli_json({"reply": "네", "action": {"tool": 5}}), b"")]).chat("s1", "x", SYSTEM)
    assert result.action is None and result.reply == "네"


async def test_cli_failure_raises_and_keeps_history_clean():
    store = ac.SessionStore()
    with pytest.raises(ac.ClaudeUnavailable):
        await _assistant([(1, b"", b"Not logged in")], store=store).chat("s1", "안녕", SYSTEM)
    assert store.get("s1") == ()


def test_history_is_trimmed_to_the_limit_from_the_front():
    history = tuple({"role": "user" if i % 2 == 0 else "assistant", "text": f"m{i}"} for i in range(ac.HISTORY_LIMIT + 8))
    trimmed = ac.trim_history(history)
    assert len(trimmed) == ac.HISTORY_LIMIT and trimmed[0]["role"] == "user"
    assert trimmed[-1] == history[-1]


def test_system_prompt_states_safety_rules_state_and_tools():
    prompt = ac.system_prompt(CTX, now="2026-11-03 11:00", strategies={"current": "", "strategies": [{"name": "constant", "description": "상수", "params": {}}]},
                              experiment={"running": False}, recent_runs=[{"run_id": "experiment_20260907_125605_833427", "verdict": "pass"}])
    for word in ("조작", "확인", "지어내", "[실행 결과]", "2026-11-03", "start_experiment", "constant",
                 "experiment_20260907_125605_833427"):
        assert word in prompt
    assert '"pumps"' in prompt


# ─────────────────────────── server.py 배선 (정적 검증) ───────────────────────────

SERVER = WORK / "server.py"


def _routes() -> set[tuple[str, str]]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                    and isinstance(deco.func.value, ast.Name) and deco.func.value.id == "app"
                    and deco.args and isinstance(deco.args[0], ast.Constant)):
                found.add((deco.func.attr, deco.args[0].value))
    return found


def test_server_exposes_chat_route_and_loads_env_file():
    assert ("post", "/api/assistant/chat") in _routes()
    source = SERVER.read_text(encoding="utf-8")
    assert "assistant_claude.load_env_file(" in source
    assert "ClaudeAssistant.from_env(" in source
    assert "sk-ant-" not in source and "ANTHROPIC_API_KEY" not in source


def test_server_chat_route_never_executes_the_action():
    """/api/assistant/chat 은 Action 을 돌려줄 뿐 experiment_runner·control·펌프를 건드리지 않는다."""
    source = SERVER.read_text(encoding="utf-8")
    block = source[source.index('"/api/assistant/chat"'):]
    block = block[:block.index("\n@app.", 1)] if "\n@app." in block[1:] else block
    for forbidden in ("experiment_runner.start", "control.start", "/api/spacebio/pump/step",
                      "pump/dispense", "control.apply_speed_config"):
        assert forbidden not in block


def test_system_prompt_asks_for_spoken_korean_without_english_status_words():
    """답은 TTS 로 읽히므로 degraded/fault/rpm 같은 영문 상태어를 한국어로 풀어 말하게 한다."""
    assert "저하" in SYSTEM and "고장" in SYSTEM and "구어체" in SYSTEM


def test_server_status_route_reports_the_https_url():
    source = SERVER.read_text(encoding="utf-8")
    block = source[source.index('"/api/assistant/status"'):]
    block = block[:block.index("\n@app.", 1)]
    assert '"https_url"' in block
    assert "assistant_tools.tailscale_https_url(" in source
