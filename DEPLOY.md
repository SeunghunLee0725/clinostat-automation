# 새 기계에 설치 — 순서

기계 종류에 따라 필요한 부분이 다르다.

| 기계 | 필요한 것 |
|---|---|
| **라즈베리파이** (실기 관제) | 1 → 2 → 3 → 5 |
| **개발 노트북** (코드·테스트) | 1 → 2 → 5 |
| **펌웨어 flash 용** (arduino-cli 있는 기계) | 1 → 4 |

---

## 1. 클론과 자격증명

```bash
git clone https://github.com/SeunghunLee0725/clinostat-automation.git
cd clinostat-automation
```

**이 저장소에는 자격증명이 없다.** 쓰기 전에 채운다.

```bash
cd firmware/clinostat_cube_esp32c6
cp pump_secrets.h.example pump_secrets.h
$EDITOR pump_secrets.h              # WiFi SSID·암호, MQTT 계정·암호
```

`.gitignore` 가 `pump_secrets.h` 를 막으므로 실수로 커밋될 일은 없다.
게이트웨이 쪽 MQTT 자격증명은 3번의 `config.yaml` 에서 채운다.

## 2. 게이트웨이 의존성

```bash
cd integration
bash deploy/setup.sh                # venv + 잠긴 의존성 (런타임만)
```

플랫폼별 잠금 파일이 있어야 한다. 검증된 조합은 셋뿐이다.

| 플랫폼 | Python | 잠금 파일 |
|---|---|---|
| macOS arm64 (개발) | 3.14 | `constraints-macos-arm64-py314-dev.txt` |
| Raspberry Pi OS aarch64 | 3.13 | `constraints-linux-aarch64-py313.txt` |
| Raspberry Pi OS aarch64 | 3.11 | `constraints-linux-aarch64-py311.txt` |

다른 조합이면 `setup.sh` 가 **거부한다.** 기계의 기본 `python3` 가 표와 다르면
인터프리터를 지정한다.

```bash
PYTHON=python3.14 bash deploy/setup.sh
```

테스트를 돌리려면 dev 의존성을 더한다.

```bash
.venv/bin/pip install -r requirements-dev.txt -c <위 표의 잠금 파일>
.venv/bin/python -m pytest -q       # 537개가 통과해야 한다
```

## 3. 파이 런타임 배치

디렉터리 배치, `config.yaml` 의 기계별 항목, systemd 유닛, BLE 주의사항,
방화벽(`nftables`) 은 **[`integration/DEPLOY.md`](integration/DEPLOY.md)** 에 있다.
여기서 반복하지 않는다 — 그쪽이 단일 진실 소스다.

요점만:
- 게이트웨이는 **절대 외부에 바인딩하지 않는다** (`0.0.0.0` 이면 `load_config` 가 거부)
- `deploy_to_pi.sh` 는 **`config.yaml` 을 덮어쓰지 않는다** — 설정 키가 늘면 손으로 더한다
- `clinostat.service` 를 게이트웨이에 의존시키지 않는다 — 게이트웨이가 죽어도 모터 제어는 돌아야 한다

## 4. 펌웨어 빌드·업로드

```bash
cd firmware/clinostat_cube_esp32c6
arduino-cli lib install PubSubClient
arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C6 --output-dir ./build_out .
arduino-cli upload -p <PORT> --fqbn esp32:esp32:XIAO_ESP32C6 .
```

⚠ **포트는 `/dev/serial/by-id` 의 `Espressif` 링크로 찾을 것.** ACM 번호는 매번
바뀌고 클리노스텟 장비(Oriental Motor BLVD-KRD 등)가 같은 대역을 쓴다 —
**잘못 열면 안 된다.**

배선을 모를 때의 진단 순서(`sweep` → `pin:step:` → `hz:`)와 육안 확인 주의사항은
[`firmware/clinostat_cube_esp32c6/README.md`](firmware/clinostat_cube_esp32c6/README.md).

## 5. 확인

```bash
cd integration
.venv/bin/python -m uvicorn gateway.app:app --host 127.0.0.1 --port 8010
curl -s localhost:8010/health
```

파이라면 브라우저로:

```
http://<파이>:8000/
```

- 우측 **SpaceBio Integration** 패널 → 실험명이 `YYYYMMDD-` 로 채워져 있다
- **기록 관리** 카드에 세션 목록·용량이 뜬다
- 측정 시작 → 저항 추이 그래프가 흐른다

---

## 여러 기계에 깔 때 주의

- **BLE 주변장치는 중앙장치를 하나만 받는다.** 노트북 모니터 앱이 센서에 붙어
  있으면 파이가 못 붙는다. 두 기계에서 동시에 측정할 수 없다.
- `config.yaml` 은 **기계 고유 설정**이다. 저장소 버전은 모의 백엔드 기준이므로
  실기에서는 반드시 고친다. 배포 스크립트가 덮어쓰지 않으니 새 설정 키가 생기면
  `integration/DEPLOY.md` 를 보고 손으로 더해라.
- 무선 펌프 보드가 다른 IP 대역으로 옮겨가면 **MQTT 가 조용히 막힌다** —
  방화벽 허용목록에 보드 대역을 더해야 한다.
- 브로커 주소는 재flash 없이 바꿀 수 있다 — 시리얼이나 MQTT 로
  `broker:<ip>` / `brokerhost:<name>`, NVS 에 남는다.
