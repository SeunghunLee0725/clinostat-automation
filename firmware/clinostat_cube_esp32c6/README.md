# clinostat_cube_esp32c6 — 무선 펌프 컨트롤러

지난해 데모 컨트롤러의 재작성판. 원본 펌웨어 소스가 없어(디스크·GitHub 모두 부재)
`S25007_Manual_RevB.pdf` 의 명령/상태 프로토콜을 재현했다.
**프로토콜은 매뉴얼과 동일** → 기존 Flutter 앱과 `spacebio-gateway` 가 수정 없이 붙는다.

## 구동부 재작성 (2026-07-29)

원본 매뉴얼은 **사용자 매뉴얼이라 배선/핀아웃이 없다.** 그래서 STEP/DIR/EN 핀은 추측이었고,
"WiFi·MQTT 는 되는데 모터가 안 도는" 상태가 오래 이어졌다. 이번 재작성의 목표는 기능 추가가
아니라 **모르는 배선을 재flash 없이 찾아낼 수 있게 만드는 것**이다.

| 이전 | 지금 |
|---|---|
| 핀이 컴파일 타임 상수 → 배선 바꿀 때마다 재flash | **런타임 변경 + NVS 저장** (`pin:`, `pinD:`) |
| USB 는 로그 전용 (`Serial.read` 없음) → 네트워크 끊기면 조작 불가 | **시리얼로 전 명령 사용** — 브로커 없어도 구동·진단 가능 |
| `ensureMqtt()` 가 접속될 때까지 블로킹 → 방화벽에 막히자 보드가 통째로 멈춤 | **비블로킹 상태기계** — 오프라인이어도 loop 가 계속 돈다 |
| 진단(`spin`/`probe`/`sweep`)이 핀을 직접 토글 → `pos` 에 안 잡혀 상태가 어긋남 | **모든 펄스가 단일 엔진** → `pos` 가 항상 실제와 일치 |
| 진단이 최대 30초 블로킹 → 그동안 status 끊김 | 진단도 비블로킹 (스윕 중에도 status 응답) |
| SPM 상한 2400(=40펄스/초)뿐 → 마이크로스텝 때문에 육안 확인 불가 | `hz:` 로 펄스율 직접 지정 (같은 엔진, `pos` 정상) |
| DIR 을 명시적으로 구동하지 않음 | 항상 정해진 값 유지, `dir:` 로 변경 |

AccelStepper 의존성을 제거했다(자체 엔진으로 대체). 필요한 라이브러리는 **PubSubClient** 뿐.

## 펌프 2대 (2026-10-15)

보드 하나가 **DRV8825 두 개**를 몬다. 엔진(`PumpDrive`) 인스턴스가 `PUMP_COUNT`(=2)개 있고
각자 독립된 STEP/DIR/EN 핀·spm·`pos` 를 가진다. 두 모터는 완전히 개별 운전된다.

| | 펌프 1 | 펌프 2 |
|---|---|---|
| 기본 핀 | STEP=D9(GPIO20) DIR=D10(GPIO18) — 2026-07-29 실기 확인 | STEP=D7(GPIO17) DIR=D8(GPIO19) — **미검증 기본값** |
| NVS 키 | 기존 그대로 (`stepGpio`, `spm` …) | 접미사 `2` (`stepGpio2`, `spm2` …) |
| MQTT 명령 | `s25007/board1/cmd` | `s25007/board1/p2/cmd` |
| MQTT 상태 | `s25007/board1/status` | `s25007/board1/p2/status` |
| 시리얼 접두어 | 없음 또는 `p1:` | `p2:` |

- 상태 줄 맨 앞에 `pump=N` 이 붙는다. 나머지 형식은 매뉴얼 RevB §5 + `pos` 그대로.
- 응답 토픽 `s25007/board1/reply` 는 공유하며 펌프 2 응답은 `p2 ` 접두어가 붙는다.
- `stopall` 은 두 펌프를 함께 멈춘다. 보드 공통 명령(`led` `pincheck` `broker` `brokerhost`
  `enpol` `pulse` `help` `status` `wiring`)은 접두어를 무시한다.
- `pin:` 으로 다른 펌프가 이미 쓰는 GPIO 를 지정하면 거부한다(두 모터가 한 선을 공유하면 개별 운전이 깨진다).
- `sweep` 은 다른 펌프의 STEP/DIR/EN 핀을 건너뛴다 — 그 모터가 같이 돌면 결과를 읽을 수 없다.

**펌프 2 처음 붙일 때:** 시리얼에서 `p2:sweep` → 모터 2 가 반응한 gpio 를 `p2:pin:step:<gpio>` 로
고정 → `p2:hz:2000:10` 으로 회전 확인. 펌프 1 과 같은 순서다.

전원: 12 V → 두 드라이버 VMOT 병렬. MP1584 5 V → XIAO 5 V 핀 + 두 드라이버의 SLP/RST.
DRV8825 는 로직 전원 핀이 없으므로(VMOT 로부터 내부 3.3 V 생성) 5 V 는 SLP/RST 를 HIGH 로
잡는 신호일 뿐이다. 각 VMOT 에 100 µF 이상 전해 콘덴서, 공통 GND, Vref 전류 제한 설정.

### 파일 구성
| 파일 | 역할 |
|---|---|
| `clinostat_cube_esp32c6.ino` | setup/loop, 상태 문자열 생성 |
| `pump_config.h` | NVS 영속 설정 (핀·극성·펄스폭·브로커·구동값), XIAO D핀↔GPIO 매핑 |
| `pump_drive.h` | 비블로킹 스텝 엔진, 램프업, 스윕, pincheck |
| `pump_net.h` | 비블로킹 WiFi/MQTT, mDNS+고정IP 브로커 탐색 |
| `pump_commands.h` | 명령 파서 (MQTT·시리얼 공용) |

## 설정

- **WiFi·MQTT 자격증명**: `pump_secrets.h` — **저장소에 없다.** 새 기계에서는
  `cp pump_secrets.h.example pump_secrets.h` 후 값을 채운다. 없으면 컴파일이 멈춘다.
- **MQTT**: 1883 평문 + 인증. 브로커는 익명 접속을 금지해야 한다 (아래 브로커 절 참고)
- **토픽**: `s25007/board1/cmd` (구독) · `s25007/board1/status` (2초 주기)
  · `s25007/board1/reply` (명령 응답 — status 파서와 섞이지 않도록 분리)
  · 펌프 2 는 `s25007/board1/p2/cmd` · `s25007/board1/p2/status` (위 "펌프 2대" 절)
- **브로커 주소**: mDNS 호스트명 우선, 실패 시 고정 IP. **둘 다 런타임 변경 가능** →
  브로커가 옮겨가도 재flash 불필요.

## 명령어

매뉴얼 RevB §4 명령은 의미까지 그대로 유지한다:

```
[p1:|p2:]start · stop · once · manualstep:N · spm:N · spinsteps:N · spinspm:N · led:on|off · stopall
```
접두어가 없으면 펌프 1 이다. MQTT 는 토픽이 대상을 정하므로 접두어가 필요 없다.

재작성하며 추가한 진단·배선 명령:

| 명령 | 설명 |
|---|---|
| `hz:<hz>[:<sec>]` | 펄스율 직접 지정. **육안 확인은 이걸로 한다** (아래 주의 참고) |
| `sweep[:<hz>[:<ms>]]` | 헤더 핀을 하나씩 펄스해 실제 STEP 배선을 찾는다. EN 핀은 건너뛴다 |
| `pin:step\|dir\|en:<gpio>` | 배선 변경 (-1 = 미연결). 즉시 적용 + NVS 저장 |
| `pinD:step\|dir\|en:<D번호>` | 같은 것을 D핀 번호로 (D9 → GPIO20) |
| `enpol:low\|high` | ENABLE 극성 |
| `pulse:<us>` | STEP HIGH 폭. 드라이버가 펄스를 놓치는 의심이 들면 올린다 |
| `dir:fwd\|rev` | 회전 방향 |
| `hold:on\|off` | 코일 여자만 켜고 끄기 (EN 배선된 경우) |
| `pincheck:<gpio>` | 패드 무결성 (MCU 쪽만 — 배선 단선은 못 잡는다) |
| `broker:<ip>` / `brokerhost:<name>` | 브로커 주소 |
| `status` · `wiring` · `resetpos` · `help` | 조회·초기화 |

**같은 명령을 MQTT 와 USB 시리얼 양쪽에서 쓴다.** 시리얼은 115200, 한 줄에 하나.
모터 명령(`hz` `sweep` `pin` `pinD` `dir` `hold` `resetpos` 등)도 `p2:` 접두어로 펌프 2 를 고른다.

## ⚠ 육안으로 회전을 확인할 때

(2026-10-15 이후 SPM_MAX 는 60000=1000Hz 이고 `manualstep` 도 설정된 spm 으로 돈다. 아래는
그 전 상한 2400 시절의 기록이다.) 매뉴얼의 SPM 상한 2400 은 **분당** 스텝수라 40펄스/초에 불과하다. 1/16 마이크로스텝이면
**1회전에 3200펄스 = 80초**여서 아무리 봐도 도는 게 안 보인다. 실제로 이 때문에
"안 돈다"고 잘못 판정한 적이 있다(2026-07-25, `spm:2400` 10초 = 0.16회전).

**반드시 `hz:` 를 쓸 것.** 예: `hz:4000:15` → 1/16 마이크로스텝 기준 초당 약 1.25회전.
1초간 100Hz→목표로 램프업하므로 급출발 탈조는 없다.

## 배선을 모를 때 순서

1. 시리얼 115200 접속 → `help`
2. `sweep` — 각 헤더 핀을 순서대로 펄스. 모터가 반응한 구간의 gpio 를 기록
3. `pin:step:<그 gpio>` 로 고정 (NVS 에 남아 재부팅해도 유지)
4. `hz:2000:10` 으로 회전 확인

`pos` 가 증가해도 **모터가 돌았다는 뜻이 아니다** — 엔코더가 없어 회전은 원격 확인이
불가능하다. 반드시 눈이나 손으로 확인할 것.

## 빌드 · 업로드

```bash
arduino-cli lib install PubSubClient
arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C6 --output-dir ./build_out .

# 맥에서 직접
arduino-cli upload -p <PORT> --fqbn esp32:esp32:XIAO_ESP32C6 .

# 파이를 거쳐 (맥에만 arduino-cli 가 있고 파이엔 esptool 만 있는 경우)
scp build_out/clinostat_cube_esp32c6.ino.merged.bin <pi>:~/pump.bin
ssh <pi> '~/.esptool-venv/bin/esptool --chip esp32c6 -p /dev/ttyACMx write-flash 0x0 ~/pump.bin'
```

⚠ **포트는 `/dev/serial/by-id` 의 `Espressif` 링크로 찾을 것.** ACM 번호는 매번 바뀌고,
클리노스텟 장비(Oriental Motor BLVD-KRD 등)가 같은 ACM 대역을 쓴다 — **잘못 열면 안 된다.**

## 백업 / 되돌리기

⚠ **플래시 백업 `.bin` 은 이 저장소에 없다.** 4 MB 전체 덤프라 NVS 에 WiFi·MQTT
자격증명이 그대로 박혀 있어서 공개 저장소에 둘 수 없다. 되돌려야 하면 작업 기계의
로컬 사본이 필요하다 (`.gitignore` 가 `*.bin`, `*.app` 을 막는다).

| 파일 (로컬 전용) | 내용 |
|---|---|
| `clinostat_backup_20260722.bin` | 지난해 원본 펌웨어 (클라우드 브로커 + TLS) |
| `clinostat_qsolutions_backup_20260729.bin` | 파이 배포판. **이 판의 소스는 남아 있지 않다** — 지우지 마라 |

```bash
esptool --chip esp32c6 -p <PORT> write-flash 0x0 <백업파일>
```

현재 소스로 되돌리는 건 저장소만으로 가능하다 — 위 "빌드 · 업로드" 절을 쓴다.

## 현장 네트워크 (2026-10-15, 실기)

- **보드를 파이 USB 에 꽂지 마라.** 브레드보드에서 MP1584 5V 가 XIAO 5V 핀(=USB VBUS)에
  묶여 있어 Pi 4 가 저전압(`get_throttled`=0x50000)으로 재부팅을 반복했다. 플래시·시리얼
  진단은 노트북 USB 에서, 무선 운전은 12V→MP1584 단독 급전으로 한다.
- 같은 SSID(`qsolutions6402`)인데 보드와 파이가 **다른 /24** 를 받는다 → mDNS 실패.
  상위 방화벽이 **1883 과 8000 을 drop** 하고 22·1884 등은 통과시킨다. 그래서 파이에
  `1884→127.0.0.1:1883` TCP 중계(`mqtt_relay_1884.py`, 홈 디렉터리)를 두고 보드는
  `broker:<파이 IP>:1884` 로 붙는다. 포트는 NVS 에 저장된다.
- `write-flash 0x0 *.merged.bin` 은 NVS 까지 지운다. 전체 플래시 뒤에는 `broker:…` 와
  바꾼 핀 설정을 다시 넣어야 한다.
- 브링업 스크립트는 USB 없이도 돈다: `bringup_2pump.py --mqtt` (파이에서, 브로커
  계정은 게이트웨이 `config.yaml` 의 `mqtt:` 절에서 읽는다).

## 로컬 브로커 (선택)

노트북에서 Mosquitto 를 직접 띄울 때. `mosquitto_local.conf.example` 를 복사해
`password_file` 경로를 이 기계 것으로 고친다.

```bash
cp mosquitto_local.conf.example mosquitto_local.conf
mosquitto_passwd -c ./mosquitto_passwd <계정>     # 암호 파일 생성
mosquitto -c mosquitto_local.conf -d
```

`mosquitto_passwd` 도 `.gitignore` 에 있다 — 계정 해시라 커밋하지 않는다.
**브로커가 공인 IP 를 받는 망이면 `allow_anonymous false` 를 반드시 유지할 것.**

## 검증 (2026-07-29, 실기)

- 부팅 → `pump_secrets.h` 의 WiFi 접속 (IP 192.168.0.4, rssi −50)
- 브로커 없을 때도 **보드가 멈추지 않음**, 시리얼로 전 명령 동작
- `broker:192.168.0.3` 시리얼 한 줄로 지정 → 즉시 MQTT 접속 성공 (재flash 없음)
- `manualstep:400` → 정확히 400스텝, 실측 **40.0 스텝/초** (= SPM_MAX 2400)
- `hz:800:8` → **6,050 펄스** (이론 450 램프 + 5,600 = 6,050)
- `hz:4000:8` → **30,052 펄스** (이론 30,050)
- `sweep` 진행 중에도 status 응답 — 비블로킹 확인
- `pincheck:20`(STEP) · `pincheck:18`(DIR) 모두 정상
