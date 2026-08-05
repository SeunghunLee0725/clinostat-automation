# clinostat-automation

클리노스텟 + LOC(펌프·저항센싱) + 근세포 수축력을 **하나의 화면에서 관제**하는 스택.
엣지 게이트웨이(파이)와 무선 펌프 컨트롤러(ESP32-C6) 펌웨어를 한 저장소에 둔다.

```
브라우저 ──▶ clinostat (:8000, 공개)  ──▶ SpaceBio Gateway (127.0.0.1:8010, loopback 전용)
             server.py                    gateway/app.py
             spacebio_proxy.py            ├─ BLE 저항센서 (실측 36 Hz)
             static/index.html            ├─ 무선 펌프 ──MQTT──▶ ESP32-C6 보드
                                          └─ 세션 기록 (CSV·manifest·펌프 이벤트)
```

## 구성

| 디렉터리 | 내용 |
|---|---|
| [`integration/`](integration/) | 게이트웨이(FastAPI) · 대시보드 확장 · 배포 스크립트 · 테스트 555개 |
| [`firmware/clinostat_cube_esp32c6/`](firmware/clinostat_cube_esp32c6/) | 무선 펌프 컨트롤러 펌웨어 (Seeed XIAO ESP32-C6) |

## 새 기계에 설치

**[DEPLOY.md](DEPLOY.md)** 에 순서가 있다. 게이트웨이 상세(파이 디렉터리 배치,
systemd, BLE·방화벽 주의, 기록 관리 API, 저장 용량)는
[`integration/DEPLOY.md`](integration/DEPLOY.md), 펌웨어 빌드·배선 진단은
[`firmware/clinostat_cube_esp32c6/README.md`](firmware/clinostat_cube_esp32c6/README.md).

## ⚠ 이 저장소는 공개다

자격증명은 **하나도 들어 있지 않다.** 새 기계에서 직접 채운다.

| 파일 | 만드는 법 |
|---|---|
| `firmware/**/pump_secrets.h` | `pump_secrets.h.example` 복사 후 WiFi·MQTT 값 입력 |
| `firmware/**/mosquitto_local.conf` | `mosquitto_local.conf.example` 복사 (로컬 브로커를 쓸 때만) |
| `integration/config.yaml` 의 `mqtt.username`/`password` | 파이 로컬에만 입력 — 커밋 금지 |

플래시 백업 `.bin` 도 없다 — 4 MB 전체 덤프에 NVS 자격증명이 박혀 있어
`.gitignore` 가 막는다. 되돌리려면 작업 기계의 로컬 사본이 필요하다.

## 현재 상태 (2026-08-05)

도는 것:
- **BLE 저항센서** — 실측 36 Hz, Rref·시간평균 계수를 화면에서 장치에 써 넣는다
- **통합 세션 기록** — 측정 시작/정지가 세션을 자동으로 열고 닫는다
- **기록 관리** — 세션 목록 · `.tar.gz` 다운로드 · 삭제
- **한 화면 배치** — 2560×1440 기준 3열 재배치
- **펌웨어** — WiFi·MQTT·STEP 펄스 정상 (`hz:4000:8` → 30,052 펄스, 이론 30,050)
- **무선 펌프 모터** — 스텝 구동·부피 주입 모두 실기 확인 (2026-08-04). 부피는
  `ul_per_step` 보정으로 스텝을 환산한다. 대시보드부터 모터까지 종단 검증됨

아직 아닌 것:
- **보드가 꺼져 있어도 명령이 200으로 성공한다** — 브로커가 publish를 받아 주기
  때문이다. 보드 status 최종 수신 시각을 추적해 offline을 표시해야 한다
- 유로 방식(밸브/볼루스/수동) 미정 → `pump_actuator` 가 소스-불문 API로 흡수

## 테스트

```bash
cd integration && .venv/bin/python -m pytest -q      # 555개
```

`tests/` 는 게이트웨이, `clinostat_extension/tests/` 는 프록시와 화면 계약을 본다.
후자에는 **append-only 가드**가 있다 — 캡처된 베이스라인 HTML 의 줄을 고치거나
지우면 `test_work_copies_are_a_superset_of_the_captured_baseline` 가 깨진다.
