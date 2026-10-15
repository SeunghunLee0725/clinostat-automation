# 새 기계에 설치 — 순서

## 저장소에 **없는** 것 (기계마다 직접 채운다)

이 저장소는 **공개**다. 자격증명과 기계 고유값은 들어 있지 않다. 클론만으로는
소프트웨어가 다 서지만(`setup.sh` → 테스트 555개 → 게이트웨이 기동까지 확인됨),
실기를 붙이려면 아래를 만들어야 한다.

| 없는 것 | 왜 | 어떻게 |
|---|---|---|
| `firmware/**/pump_secrets.h` | WiFi·MQTT 자격증명 | `pump_secrets.h.example` 복사 후 채움 (1번) |
| MQTT 브로커 + 계정 | 무선 펌프의 명령 경로 | `integration/DEPLOY.md` 의 브로커 절 |
| `integration/config.yaml` 실기값 | 기계마다 다름 | 저장소본은 **모의 백엔드** 기본값 (3번) |
| `integration/.venv/` | 플랫폼 의존 | `bash deploy/setup.sh` 가 만든다 (2번) |
| `*.bin` 플래시 백업 | NVS에 자격증명이 박혀 있어 커밋 금지 | 소스에서 다시 빌드 (4번) |

⚠ 예외 하나 — `firmware/.../clinostat_qsolutions_backup_20260729.bin` 은 **소스가
남아 있지 않은 판**이다. 그 기계에만 있으니 지우지 마라. 현재 소스로 빌드한 펌웨어와
기능은 같고, 다른 점은 컴파일된 WiFi/브로커 기본값뿐이다(런타임에 바꿀 수 있다).


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
.venv/bin/python -m pytest -q       # 555개가 통과해야 한다
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

## 6. 펌프 — 부피로 주입하려면 µL 보정이 먼저다

펌웨어는 **스텝으로만** 돈다. 연동식 펌프는 튜브 규격마다 스텝당 토출량이 달라
펌웨어에 상수로 박을 수 없다. 그래서 **실측해서 화면에 넣는다.**

1. 튜브를 끼운 상태로 정해진 스텝 수를 돌린다 — 패널 **③ 스텝 구동**에서
   예: 10,000 스텝. (육안·계량이 목적이므로 충분히 크게)
2. 토출된 양을 계량한다(저울 또는 눈금 실린더).
3. `µL ÷ 스텝` 을 패널 **① µL 보정**의 *스텝당 토출량*에 넣고 **보정 적용**.
4. 그때부터 **② 부피 주입**이 열린다. 게이트웨이가 환산해서 보낸다.

```
steps = 목표량µL ÷ ul_per_step
spm   = (유량µL/s ÷ ul_per_step) × 60     # 1~2400 으로 자름
```

API 로도 같다 — `POST /api/spacebio/pump/calibration {"ul_per_step": 0.5}`.
적용값은 `pump.ul_per_step` 으로 상태에 실려 나오고, 없으면 화면이 부피 컨트롤을
잠근다. **튜브를 바꾸면 다시 재야 한다.**

화면에서 넣은 값은 **게이트웨이 메모리에만 있다** — 재시작하면 지워진다. 튜브가
고정된 설치라면 `config.yaml` 에 적어 둔다. 그러면 기동할 때부터 적용된다.

```yaml
pump:
  backend: wireless
  ul_per_step: 0.5        # 실측값
```

## 7. 운전 중 알아둘 것

- **비상정지 해제에는 확인 문구가 필요하다.** 패널에 그대로 적혀 있으니 보고 입력한다
  (`RESET_SIMULATED_PUMP_ESTOP`). 오타 방지용이지 암호가 아니다.
- **`stop` 으로는 코일 전류가 끊기지 않는다**(EN 미배선). 드라이버 발열을 멈추려면
  모터 전원(VMOT)을 물리적으로 내려야 한다.
- **스테퍼 드라이버 전류 제한(Vref)을 먼저 맞춰라.** 전압을 올릴수록 전류가 따라
  올라가면 정전류 제어가 안 걸린 것이다 — 과열로 열 셧다운이 나서 축이 잠긴 채
  회전만 안 한다. DRV8825 는 `Vref = I × 5 × Rsense`(표준 Rsense 0.1Ω → `I × 0.5`).
- **보드는 파이 USB 에 꽂지 않는다.** 12V→MP1584 가 XIAO 5V 핀에 물린 채 파이 USB 까지
  꽂으면 Pi 4 가 저전압으로 재부팅을 반복한다(2026-10-15). 플래시는 노트북에서.
- **현장 방화벽이 1883 을 drop 한다.** 파이에서 1884→1883 중계를 띄우고 보드에
  `broker:<파이 IP>:1884` 를 넣는다. 자세한 건 펌웨어 README "현장 네트워크".
- **육안으로 회전을 확인할 때는 `hz:<hz>:<sec>`** 를 쓴다. 매뉴얼 SPM 상한(2400)은
  1/16 마이크로스텝에서 1회전에 80초라 눈으로 볼 수 없다. `hz:800:10` ≈ 2.4회전.

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
