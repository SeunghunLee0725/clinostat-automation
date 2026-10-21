# 작업 로그 — 우주바이오 펌프·센서 통합

다음 세션이 바로 이어서 일할 수 있도록 **현재 상태**와 **다음 할 일**을 적는다.
가장 최근 항목이 위다. 상세 근거는 각 커밋과 `firmware/clinostat_cube_esp32c6/README.md`,
`DEPLOY.md` 에 있다.

---

## 2026-10-21 — 저항센서 새 보드 연결 성공

| 항목 | 상태 |
|---|---|
| 보드 | Nano 33 BLE(시리얼 711432C98D98544F, 주소 32:C9:8D:98:54:4F) + 새 센서 보드. ADS1115 0x48 인식, **TMP102 없음**(25 °C 고정, 비치명), VREF 1.63 V·Vcc 3.26 V |
| 원인 | 보드에 BLE 없는 시리얼 점검 스케치가 올라가 있어 광고가 안 보였음(부팅 로그에 `[BLE]` 줄 없음). 소스 없음, 부트로더가 읽기 미지원이라 백업 못 함 |
| 조치 | 파이에서 `~/sensor_ble_firmware_20261020.bin` bossac 업로드 → 광고 `ResistanceSensor` RSSI −39 |
| 연결 | `POST /api/sensor/start` → 1차 실패, 2차 성공(기존과 같음). `:8000/api/spacebio/sensor/status` 로 30 s 연속 수신, raw_adc ≈ 25,085, R ≈ 1.36 kΩ(Rref 81 kΩ), ΔR/R0 ±1 % |
| 주의 | 1200 bps 리셋을 짧게 반복하면 부트로더(PID 005a)에 걸려 원격 복구 불가 → USB 재연결 |

### 다음 할 일
- 시료 R ≈ 1.4 kΩ 은 Rref 81 kΩ 대비 작아 Vmid 가 Vexc 에 붙음(분해능 낮음). 실제 시료 범위에 맞춰 Rref 재검토
- 10-16 절의 미커밋 `ble_sensor.py`·`config.yaml` 커밋과 `test_ble_sensor.py` 2건 수정은 아직 남음
- 펌프와 동시 운전 시 통합 세션 기록 확인

---

## 2026-10-17 — 통합 관제 콘솔(/console) + 음성 비서 배포

### 지금 상태 (이 줄만 읽어도 됨)

| 항목 | 상태 |
|---|---|
| 새 페이지 | `http://100.77.1.42:8000/console` (`work/static/console.html`, 커밋 703cd93). 기존 `/` 화면과 별개 파일이라 baseline 계약 무관 |
| 카드 | 클리노스텟(/ws), 저항센서(/ws/spacebio, SVG 스파크라인), 펌프 1·2(주파수 입력형), **전기신호 제어(빈 카드, 전부 disabled)**, 음성 비서, 이벤트 로그·비상정지 |
| 음성 비서 서버 | `work/assistant.py` + `/api/assistant/{status,command,summary}`. 규칙 파서가 명령을 만들고, 장치를 움직이는 명령은 브라우저가 [실행] 확인 뒤 기존 `/api/*` 로 보냄. LLM 글은 절대 실행되지 않음 |
| 온보드 LLM | Tailscale llama-server 2대, 순서대로 시도: **dgx-spark** `100.90.190.82:8081` gemma-4-12b-it(GPU, 파이에서 1~3 s) → **kims-pi-2** `100.85.45.76:8081` gemma-4-E2B-it(CPU, ~10 s). 둘 다 죽으면 규칙 요약(`template_summary`) |
| LLM 기동 방법 | 두 호스트 `shlee@` 에서 `nohup setsid ~/llm_onboard/llama.cpp/build/bin/llama-server -m ~/llm_onboard/models/<gguf> --host 0.0.0.0 --port 8081 --jinja …`(Spark `-ngl 99 -t 8 -c 8192`, pi5 `-t 4 -c 4096`). 로그 `~/llm_onboard/logs/assistant_server.log`. **재부팅하면 사라짐**(systemd 미등록, pi5 linger 꺼짐) — 콘솔 LLM 칩이 "LLM 없음"이면 다시 띄울 것 |
| gemma-4 주의 | 기본값은 thinking 을 `reasoning_content` 에 쓰고 본문이 비어 8 s 걸린다. 클라이언트가 `chat_template_kwargs.enable_thinking=false` 를 보내 1 s 대 |
| STT/TTS | 브라우저 Web Speech API(ko-KR)·speechSynthesis. HTTP 주소라 Chrome 이 마이크 권한을 매번 물을 수 있음. 글 입력창으로도 같은 경로 |
| 실측 | 파이에서 "지금 상태 알려 줘" → Spark 3.2 s 한국어 요약. 콘솔에서 "펌프 1 100헤르츠 2초" → 확인 → 실행 → 펌프 1 위치 6000→6200 |
| 테스트 | 771 passed(`--deselect tests/test_ble_sensor.py`). 새 테스트 `test_assistant.py`, `test_console_dom.py` |
| 파이 배포 | server.py·assistant.py·console.html(백업 `~/spacebio-backups/20261017_001258`). `deploy_to_pi.sh` 에 두 파일 추가됨 |

### 음성 명령 규칙 (서버 `assistant.parse_command`)
- `펌프 N X헤르츠 [Y초]` → 확인 후 스텝 명령(steps=X·Y, spm=X·60). 한글 숫자(삼백, 이십) 됨. 1~1000 Hz, 펌프 1·2
- `펌프 [N] 정지`, `클리노스텟 정지`, `전부/모두 정지`, `비상 정지`, `측정 정지` → 확인 없이 즉시
- `측정 시작`, `클리노스텟 시작` → 확인 필요(클리노스텟은 안전 확인 체크박스도 필요)
- `상태/현황/요약/보고` → LLM 요약(없으면 규칙 요약). 그 외 문장 → LLM 대화(실행 없음)

### 다음 할 일
- 전기신호 장치가 정해지면 `csEstimCard` 필드(채널·파형·진폭·주파수)와 `/api/spacebio/estim/*` 를 채운다
- llama-server 를 systemd(user) 로 영구화(pi5 는 `loginctl enable-linger` 에 sudo 필요)
- 음성 호출어("스페이스바이오") 상시 듣기는 보류 — 마이크 버튼 방식 유지
- 저항센서 연결은 아래 2026-10-16 "다음 세션" 절 그대로

---

## 2026-10-16 — 펌프 2대 무선 운전 완성, 다음은 전기신호 센서 연결

### 지금 상태 (이 줄만 읽어도 됨)

| 층 | 상태 |
|---|---|
| ESP32-C6 보드 | 펌웨어 **1a02cdc**(앱 영역만 플래시, NVS 유지). NVS: broker `61.76.30.23:1884`, pulse 30 µs, 펌프 1 EN=D1, 펌프 2 EN=D2, ramp 100 Hz/1000 ms(기본) |
| 드라이버 DRV8825 ×2 | 감지저항 R100. **Vref 0.63 V**(전류 한계 1.26 A) 양쪽 동일. 100~600 Hz 전 구간 회전, 10분 열 시험 발열 거의 없음 |
| 배선 | 펌프 1 STEP=D9 DIR=D10 EN=D1 · 펌프 2 STEP=D7 DIR=D8 EN=D2. 12 V→MP1584→XIAO 5 V. **보드를 파이 USB 에 꽂지 말 것**(Pi 4 브라운아웃) |
| 파이 게이트웨이 :8010 | `pump.count: 2`, PUMP_SPM_MAX 60000. 1884→1883 중계는 사용자 systemd `mqtt-relay-1884.service`(linger) |
| 파이 대시보드 :8000 | server.py **176cf5e**(페이지 no-cache 미들웨어), index.html **8608e93**(펌프 카드 "주파수로 구동" 1~1000 Hz × 초). 백업 `~/spacebio-backups/20261016_154116`(index.html), `20261016_154421`(server.py) |
| 저장소 | origin/main = 176cf5e. 미커밋: `integration/config.yaml`(rref 10000→81000), `integration/gateway/ble_sensor.py`(raw_adc 로 저항 재계산·재연결 백오프). **둘 다 파이에 이미 배포된 것과 동일** — 커밋만 안 됨 |
| 테스트 | `cd integration && .venv/bin/python -m pytest -q -p no:cacheprovider --deselect tests/test_ble_sensor.py` → 601 passed. `tests/test_ble_sensor.py` 2건 실패(위 미커밋 변경에 맞춰 테스트 갱신 필요) |

### 오늘 한 일 (시간순)

1. 보드 플래시: 맥에서는 **USB 허브 경유로만** 인식됨(`/dev/cu.usbmodem31101`). b45e24a → 1a02cdc.
2. "600 Hz 10초가 돌다 안 돌다" 원인 규명: 명령 경로는 3/3 정상(펄스 전부 출력) → 탈조. 펌프 2 Vref 1.7 V(=3.4 A, 칩 한계 초과)에 펌프 1 을 맞춘 것이 원인. **0.63 V 로 내리자 해결.** 어제 "600 Hz 이상만 돈다"던 저속 불량도 같은 원인.
3. 주파수 스윕(펌프 1·2, 100~600 Hz, `hz:<hz>:<sec>` MQTT) 전 구간 회전. 펌프 2 핀 D7/D8 검증 완료.
4. EN 배선(D1/D2) 후 `pinD:en:1`·`pinD:en:2` 로 NVS 등록, `hold:on/off` 확인. 정지 중 축이 자유롭게 돎. 튜브 역류 없음.
5. 열 시험 30회(두 펌프 600 Hz 10 s on/10 s off) → 발열 거의 없음.
6. 대시보드 프리셋을 주파수 입력형으로 교체(8608e93), 배포. 배포 뒤 옛 페이지가 보이던 원인은 Cache-Control 부재 → 미들웨어로 no-cache(176cf5e), 배포·재시작.
7. 펌웨어 `ramp:<hz>[,<ms>]` 설정 + 운전 중 재명령 시 현재 속도 유지(1a02cdc) 플래시.

### 손에 익혀야 할 절차

- **플래시(앱만, NVS 유지)**: 12 V 끄기 → 보드를 **허브 경유** 맥 USB → `arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C6 --output-dir <dir> .` → `esptool --chip esp32c6 -p /dev/cu.usbmodem* write-flash 0x10000 <dir>/clinostat_cube_esp32c6.ino.bin` → USB 뽑고 12 V. 전체 `merged.bin` 은 NVS 를 지우니 쓰지 말 것.
- **전원 재투입 후** WiFi·1884 재접속까지 **1~2분**. 그 전엔 MQTT 무응답이 정상. 확인: 파이에서 `ss -tn | grep :1884`.
- **보드 명령(파이에서)**: `cd ~/pump_fw_20261015_2pump && ~/spacebio-gateway/.venv/bin/python bringup_2pump.py --mqtt --cmd "<명령>" --listen 2`. MQTT 는 토픽이 대상이므로 `p1:` 접두어 없이. 펌프 2 는 `s25007/board1/p2/cmd`.
- **대시보드 끝단 시험**: `POST http://127.0.0.1:8000/api/spacebio/pump/step {"pump_id":1,"steps":3000,"spm":36000}`.
- **게이트웨이 재시작**(sudo 없음): `kill -TERM $(systemctl show -p MainPID --value spacebio-gateway)`; 대시보드는 `clinostat` 서비스로 같은 방법(기동 ~5 s).
- 비밀(WiFi/MQTT 비밀번호)은 `pump_secrets.h`(gitignore)와 파이 홈 스크립트에만. 푸시 전 `git diff origin/main..HEAD | grep -E '61\.76\.|211\.197\.|WIFI_PASS = "|MQTT_PASS = "'` 가 0 이어야 한다.

### 다음 세션: 전기신호(저항) 센서 연결

센서 = Arduino Nano 33 BLE + ADS1115, BLE 이름 `ResistanceSensor`, 서비스 UUID
`12345678-1234-1234-1234-1234567890AB`. 게이트웨이 `gateway/ble_sensor.py` 가 UUID 로 찾아 붙고
대시보드 "저항센서" 카드(`#spacebioPanel` 왼쪽)에 뜬다. 회로(2026-09-04 확정): Vexc 3.2 V — 시료 —
AIN0 — **Rref 81 kΩ** — GND, AIN1 = 1.6 V 중점, PGA 2(±2.048 V). 펌웨어 소스는
`1_research_results/Muscle_LOC_integraion/LOC_circuit_design/firmware/sensor_ble_firmware/`
(맥 arduino-cli, `arduino:mbed_nano:nano33ble`, 파이에서는 bossac).

시작 순서:
1. **미커밋 정리부터**: `integration/gateway/ble_sensor.py` + `config.yaml` 을 커밋한다(파이와 동일본).
   그 전에 `tests/test_ble_sensor.py` 실패 2건(`test_gives_up_after_max_reconnects_and_reports_error`,
   `test_rref_is_pushed_to_the_device_on_connect`)을 새 동작(재연결 백오프, Rref 로 재계산)에 맞춰 고친다.
2. 센서 전원(외부 5 V 는 능력 좋은 것으로) → 파이에서 `bleak` 20 s 스캔으로 UUID 광고 확인.
   광고가 없으면 펌웨어 미탑재(공장 Blink 도 LED 1 s 점멸이라 헷갈림).
3. 대시보드 저항센서 카드: Rref 81000 확인 → "측정 시작". 1차 연결은 거의 항상 실패하고 2차에 붙는다.
4. raw_adc 판독: 32767 고정 = 입력 개방(전극 미연결/건조), 0 + R=Rref = 시료가 Rref 와 같음(정상),
   I²C 실패도 0 이므로 온도·배터리와 함께 본다. battery_pct 0 은 VIN 급전 시 정상.
5. 펌프와 동시 운전: 세션 기록(통합 세션)에 저항 추이와 펌프 µL 누적이 함께 남는지 확인.
   펌프 µL 보정(스텝당 토출량)은 아직 안 넣었다 — 부피 주입을 쓰려면 카드 "① µL 보정" 먼저.

### 열린 결정

- 운전 주파수: 저속도 도니 600 Hz 고정 아님. 실험 유량에서 역산해 정한다(카드 기본값 600 Hz 는 임시).
- `ramp:` 기본(100 Hz/1 s)은 Vref 정상화 뒤 문제 없음. 탈조가 다시 보이면 `ramp:600` 시험.
- Vref 를 더 올릴 일이 있으면 정격 이내로, 방열판 없이 1.5 A(0.75 V) 를 넘기지 말 것.
