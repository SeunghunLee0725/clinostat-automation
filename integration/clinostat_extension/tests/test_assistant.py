"""음성 비서 백엔드(work/assistant.py) — 규칙 파서, 상태 요약, 온보드 LLM 클라이언트.

설계 원칙:
- 장치를 움직이는 명령은 **규칙 파서만** 만든다. LLM 은 상황 설명·질문 응답만 맡는다.
- LLM 은 Tailscale 안의 llama-server(dgx-spark 1순위, kims-pi-2 2순위)를 쓴다.
  하나가 죽어도 다음 호스트로 넘어가고, 전부 죽으면 규칙 요약으로 대신한다.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import httpx
import pytest

WORK = Path(__file__).resolve().parents[1] / "work"
sys.path.insert(0, str(WORK))

import assistant  # noqa: E402


# ─────────────────────────── 규칙 파서 ───────────────────────────

@pytest.mark.parametrize("text,pump_id,hz,sec", [
    ("펌프 1 300헤르츠 10초", 1, 300, 10),
    ("펌프 2를 600 Hz로 5초 돌려 줘", 2, 600, 5),
    ("펌프1 150hz", 1, 150, 10),                  # 시간 생략 → 기본 10초
    ("펌프 일번 삼백 헤르츠 이십 초", 1, 300, 20),  # 한글 숫자
    ("펌프 2 천 헤르츠 1초", 2, 1000, 1),
])
def test_pump_run_is_parsed_with_confirmation(text, pump_id, hz, sec):
    action = assistant.parse_command(text)
    assert action is not None
    assert action.kind == "pump_run"
    assert action.params == {"pump_id": pump_id, "hz": hz, "sec": sec}
    assert action.requires_confirm is True
    assert f"펌프 {pump_id}" in action.summary and f"{hz}" in action.summary


@pytest.mark.parametrize("text", ["펌프 1 1500헤르츠 3초", "펌프 3 300헤르츠", "펌프 1 0헤르츠 5초"])
def test_pump_run_out_of_range_is_rejected_not_executed(text):
    action = assistant.parse_command(text)
    assert action is not None and action.kind == "invalid"
    assert action.requires_confirm is False


@pytest.mark.parametrize("text,params", [
    ("펌프 1 정지", {"pump_id": 1}),
    ("펌프 2 멈춰", {"pump_id": 2}),
    ("펌프 정지", {}),                 # 번호 없음 → 전체 펌프
])
def test_pump_stop_needs_no_confirmation(text, params):
    action = assistant.parse_command(text)
    assert action.kind == "pump_stop" and action.params == params
    assert action.requires_confirm is False


@pytest.mark.parametrize("text,kind", [
    ("비상 정지", "estop"),
    ("비상정지!", "estop"),
    ("전부 정지", "stop_all"),
    ("모두 멈춰", "stop_all"),
    ("전체 정지해", "stop_all"),
    ("클리노스텟 정지", "clinostat_stop"),
    ("회전 멈춰", "clinostat_stop"),
    ("측정 정지", "sensor_stop"),
    ("센서 꺼", "sensor_stop"),
    ("지금 상태 알려 줘", "status"),
    ("현황 보고", "status"),
    ("상황 요약해 줘", "status"),
])
def test_stop_and_status_commands_never_wait_for_confirmation(text, kind):
    action = assistant.parse_command(text)
    assert action is not None and action.kind == kind
    assert action.requires_confirm is False


@pytest.mark.parametrize("text,kind", [
    ("측정 시작", "sensor_start"),
    ("센서 켜 줘", "sensor_start"),
    ("클리노스텟 시작", "clinostat_start"),
    ("클리노 가동", "clinostat_start"),
])
def test_start_commands_require_confirmation(text, kind):
    action = assistant.parse_command(text)
    assert action.kind == kind and action.requires_confirm is True


def test_emergency_stop_wins_over_everything_in_the_same_sentence():
    assert assistant.parse_command("펌프 1 300헤르츠 말고 비상 정지").kind == "estop"


def test_unknown_sentence_is_left_to_the_llm():
    assert assistant.parse_command("오늘 실험 결과 어떻게 생각해?") is None
    assert assistant.parse_command("") is None


def test_action_serialises_to_plain_dict():
    action = assistant.parse_command("펌프 1 300헤르츠 10초")
    payload = assistant.action_to_dict(action)
    assert payload["kind"] == "pump_run" and payload["params"]["hz"] == 300
    json.dumps(payload)                     # JSON 직렬화 가능


@pytest.mark.parametrize("text,expected", [
    ("삼백", 300), ("천", 1000), ("이십", 20), ("십오", 15), ("사백오십", 450), ("육", 6), ("천이백", 1200),
])
def test_korean_numerals(text, expected):
    assert assistant.korean_number(text) == expected


# ─────────────────────────── 상태 요약 ───────────────────────────

CONTROL = {
    "running": True, "elapsed": 4360.2, "strategy_name": "random_walk",
    "outer_rpm": 2.0, "inner_rpm": 3.0, "outer_actual": 2.1, "inner_actual": 3.4,
    "metrics_meas": {"tasmg_G": 0.0123},
}
SPACEBIO = {
    "gateway": {"state": "online"},
    "sensor": {"state": "idle", "mode": "BLE_LIVE", "sample": None, "rref_ohm": 81000},
    "pumps": [
        {"pump_id": 1, "state": "IDLE", "mode": "WIRELESS", "position_steps": 6000, "estop_latched": False},
        {"pump_id": 2, "state": "RUNNING", "mode": "WIRELESS", "position_steps": 7200, "estop_latched": False},
    ],
    "session": {"state": "idle", "session_id": None},
}


def test_build_context_flattens_both_status_sources():
    ctx = assistant.build_context(CONTROL, SPACEBIO)
    assert ctx["clinostat"]["strategy"] == "random_walk"
    assert ctx["clinostat"]["outer_rpm"] == 2.1       # 실측 우선
    assert ctx["clinostat"]["tasmg_G"] == 0.0123
    assert [p["pump_id"] for p in ctx["pumps"]] == [1, 2]
    assert ctx["sensor"]["state"] == "idle"
    assert ctx["estim"]["state"] == "not_connected"    # 전기신호 장치는 아직 자리만
    json.dumps(ctx)


def test_build_context_survives_missing_gateway():
    ctx = assistant.build_context(CONTROL, None)
    assert ctx["gateway"]["state"] == "unreachable"
    assert ctx["pumps"] == [] and ctx["sensor"]["state"] == "unknown"


def test_template_summary_mentions_every_subsystem_in_korean():
    text = assistant.template_summary(assistant.build_context(CONTROL, SPACEBIO))
    assert "random_walk" in text and "1시간 12분" in text
    assert "펌프 2" in text and "운전" in text
    assert "센서" in text and "전기신호" in text
    assert "0.012" in text


def test_template_summary_flags_latched_estop():
    latched = json.loads(json.dumps(SPACEBIO))
    latched["pumps"][0]["estop_latched"] = True
    text = assistant.template_summary(assistant.build_context(CONTROL, latched))
    assert "비상정지" in text


# ─────────────────────────── LLM 클라이언트 ───────────────────────────

def _transport(script):
    """호스트별 응답 스크립트 → MockTransport. script[host] = (status, body) | Exception."""
    def handler(request: httpx.Request) -> httpx.Response:
        outcome = script[request.url.host]
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


HOSTS = (
    {"name": "dgx-spark", "url": "http://10.0.0.1:8081", "model": "big", "timeout_s": 5},
    {"name": "kims-pi-2", "url": "http://10.0.0.2:8081", "model": "small", "timeout_s": 60},
)
CHAT_OK = {"choices": [{"message": {"content": "  모두 정상입니다.\n"}}]}


@pytest.mark.anyio
async def test_chat_uses_first_host_when_it_answers():
    client = assistant.LlmClient(HOSTS, transport=_transport({"10.0.0.1": (200, CHAT_OK)}))
    reply = await client.chat("sys", "지금 상태")
    assert reply.text == "모두 정상입니다."
    assert reply.host == "dgx-spark" and reply.model == "big"
    assert reply.latency_ms >= 0


@pytest.mark.anyio
async def test_chat_falls_back_to_second_host_on_error():
    client = assistant.LlmClient(HOSTS, transport=_transport({
        "10.0.0.1": httpx.ConnectError("down"),
        "10.0.0.2": (200, CHAT_OK),
    }))
    reply = await client.chat("sys", "지금 상태")
    assert reply.host == "kims-pi-2"


@pytest.mark.anyio
async def test_chat_raises_when_every_host_fails():
    client = assistant.LlmClient(HOSTS, transport=_transport({
        "10.0.0.1": httpx.ConnectError("down"),
        "10.0.0.2": (500, {"error": "boom"}),
    }))
    with pytest.raises(assistant.LlmUnavailable):
        await client.chat("sys", "지금 상태")


@pytest.mark.anyio
async def test_chat_sends_openai_chat_shape_with_system_and_user():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=CHAT_OK)

    client = assistant.LlmClient(HOSTS[:1], transport=httpx.MockTransport(handler))
    await client.chat("시스템", "사용자")
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"]["model"] == "big"
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]
    assert seen["body"]["max_tokens"] <= 400


@pytest.mark.anyio
async def test_probe_reports_each_host():
    client = assistant.LlmClient(HOSTS, transport=_transport({
        "10.0.0.1": (200, {"status": "ok"}),
        "10.0.0.2": httpx.ConnectError("down"),
    }))
    hosts = await client.probe()
    assert [(h["name"], h["reachable"]) for h in hosts] == [("dgx-spark", True), ("kims-pi-2", False)]


def test_default_hosts_are_tailscale_llama_servers():
    names = [h["name"] for h in assistant.DEFAULT_LLM_HOSTS]
    assert names == ["dgx-spark", "kims-pi-2"]
    for h in assistant.DEFAULT_LLM_HOSTS:
        assert h["url"].startswith("http://100.") and h["url"].endswith(":8081")


def test_system_prompt_forbids_device_control_and_invention():
    prompt = assistant.system_prompt({"pumps": []})
    assert "조작" in prompt and "지어내" in prompt
    assert '"pumps"' in prompt            # 상태 JSON 이 프롬프트에 들어간다


# ─────────────────────────── server.py 라우트 (정적 검증) ───────────────────────────

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


def test_server_exposes_console_page_and_assistant_routes():
    routes = _routes()
    assert ("get", "/console") in routes
    assert ("get", "/api/assistant/status") in routes
    assert ("post", "/api/assistant/command") in routes
    assert ("post", "/api/assistant/summary") in routes


def test_console_page_is_served_without_heuristic_cache():
    source = SERVER.read_text(encoding="utf-8")
    assert '"/console"' in source[source.index("PAGE_PATHS"):][:200]


def test_server_never_executes_llm_output_as_a_command():
    """LLM 응답은 글자일 뿐이다 — 서버가 그것을 장치 명령으로 바꾸는 코드가 있어선 안 된다."""
    source = SERVER.read_text(encoding="utf-8")
    block = source[source.index("/api/assistant/command"):]
    block = block[:block.index("\n@app.", 1)] if "\n@app." in block[1:] else block
    assert "parse_command" in block
    assert "_spacebio_relay" not in block and "control.start" not in block and "control.stop" not in block
