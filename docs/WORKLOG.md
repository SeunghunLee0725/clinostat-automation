# 작업 로그 — 우주바이오 펌프·센서 통합

다음 세션이 바로 이어서 일할 수 있도록 **현재 상태**와 **다음 할 일**을 적는다.
가장 최근 항목이 위다. 상세 근거는 각 커밋과 `firmware/clinostat_cube_esp32c6/README.md`,
`DEPLOY.md` 에 있다.

---

## 2026-10-30 저녁 — 근육 전용 ADS1115 추가(0x49), 저항센서와 동시 측정 **완료**

(월요일로 잡았던 작업을 칩이 있어 당일 진행.)

| 항목 | 결과 |
|---|---|
| 배선 | 새 ADS1115: VDD 3V3, GND, SDA/SCL 을 기존 칩과 같은 D4/D5, ADDR→VDD(0x49). opamp 출력→새 칩 AIN0, Vref→AIN1. opamp 전원은 보드 3.3 V |
| 펌웨어 | `sensor_muscle.h` 주소 0x49·MUX AIN0−AIN1, 저항센서 suspend/resume·워밍업 폐기 제거, 부팅 시 `[EMG] ADS1115(0x49) 연결`. 칩 없으면 `err emg 칩(0x49) 없음`만, 저항·펌프 그대로. 앱 영역 플래시(해시 검증) |
| 게이트웨이·화면 | 409 `emg_sensor_busy`·`runtime.sensor_running` 제거, "저항 측정 시작 시 연속 측정 끄기" 훅 제거, 문구 AIN0−AIN1. 파이 배포(백업 `~/spacebio-backups/20261030_163641_emg_dual_ads`), 게이트웨이만 재시작(16:37) |
| 동시 측정 실측 | 연속 측정 끔: 저항 50/s, raw sd 6.7, 9.42~9.55 kΩ, 튐 0 · **켬: 저항 ≈50/s, raw sd 4.7, 9.47~9.54 kΩ, 튐 0 · 근육 541 S/s, 조각 84, 누락 0** |

아래는 당초 계획(참고용).

### (계획) 근육 전용 ADS1115 추가

**결정(2026-10-30)**: ADC 하나(MUX 공유)로는 저항센서와 근육 연속 측정을 동시에 못 한다. 칩 하나로
번갈아 읽는 안(①) 대신 **ADS1115 를 하나 더 다는 안(②)**으로 간다 — 근육 회로 문제가 저항센서로
번지는 것(10-30 오후 실측)을 구조적으로 줄이고, 검증된 저항센서 코드를 건드리지 않는다.

### 하드웨어 (사용자)
- 새 ADS1115 모듈: VDD → **3V3**(5 V 금지 — 풀업이 ESP32 핀을 5 V 로 당김), GND → 보드 GND,
  SDA → D4(GPIO22), SCL → D5(GPIO23) — 기존 칩과 같은 버스. **ADDR → VDD = 0x49**
- 근육 신호를 새 칩으로 옮긴다: opamp 출력 → 새 칩 **AIN0**, opamp Vref → 새 칩 **AIN1**(차동).
  기존 칩(0x48) AIN2·AIN3 는 비운다
- 모듈 풀업이 두 개 겹쳐도(각 10 kΩ 안팎) 400 kHz 에 문제없음 — 이상하면 한쪽 풀업 제거
- 같이 할 것: opamp 출력 클램프(직렬 4.7 kΩ + 쇼트키 2개, 3V3·GND), Vref ≈ 1.65 V 확인, 기준(세 번째) 전극 → Vref

### 펌웨어 (`sensor_muscle.h`)
1. 근육 칩 주소 상수 `EMG_ADS_ADDR = 0x49`, MUX AIN0−AIN1(`0x0000`) 로 `EMG_CFG_BASE` 변경 — 지금은 `SENSOR_ADS_ADDR`(0x48)·AIN2−AIN3(`0x3000`)
2. `start()`/`streamOn()` 의 `_sensor->suspend()`·`resume()` 제거, 워밍업 폐기 샘플도 불필요(다른 칩) — 단, 단발 첫 변환 확인은 유지
3. 부팅·명령 때 0x49 존재 확인(`Wire.beginTransmission(0x49)`) — 없으면 `err emg 칩(0x49) 없음`, 저항센서·펌프는 그대로
4. 같은 `Wire` 를 두 클래스가 loop 안에서 번갈아 쓰므로 동시 접근 문제는 없다. 근육이 단발 폴링(≈0.1 ms/회)을 자주 해 저항 50 Hz 읽기가 밀리지 않는지 실측
5. `statusLine`·help 문구의 `AIN2-AIN3` → `0x49 AIN0-AIN1`

### 게이트웨이·대시보드
- `emg_routes.py` 의 저항 측정 중 연속 측정 409(`emg_sensor_busy`) 제거 + 테스트 수정
- `index.html` 의 "저항 측정 시작 시 연속 측정 끄기" 훅 제거 + `test_stream_stops_before_resistance_measurement_starts` 정리
- 카드 문구 "(저항센서 일시정지)" 등 정리. 패킷 형식·토픽은 그대로

### 검증
- 저항 측정 + 근육 연속 측정 동시: 저항 50 Hz·sd(≈7 LSB) 유지, 근육 S/s·누락 0, 펌프 600 Hz 동시 구동 시 펄스 손실 없음
- 근육 입력을 일부러 포화시켜도 저항값이 흔들리지 않는지(칩 분리 효과)

### 함께 남은 일
- 근육 채널 60 Hz 험 pp≈430 mV, 바닥이 0 mV 에 붙음 → Vref·레일투레일 opamp·기준 전극 확인
- inner BLDC 알람 2Ah(엔코더 통신 이상) 재발 여부 — 재발하면 모터 케이블·USB 오류(-71) 시각 대조
- 첫 연속 측정 시험 때 보드 재부팅 1회(원인 미확정)

---

## 2026-10-30 오후 — 저항센서 튐·inner BLDC 정지 진단

| 항목 | 내용 |
|---|---|
| inner BLDC 안 돎 | 드라이버(BLVD-KRD) 현재 알람 **2Ah = 엔코더 통신 이상**(HP-5140-7E 알람표, ALM-RST 로 해제 불가·무여자). 출력 상태 ALM-A=1·READY=0. 드라이버 전원 재투입으로 해제(알람 0). 이력 2Ah, 84h(RS-485). **재투입 후 Modbus 포트 ttyACM2→ttyACM1** — 서버를 새 포트로 재연결함. 같은 날 USB 허브 1-1.3 이 `error -71` 폭주 뒤 통째로 끊김 2회(10:48, 15:46:39, 운전 직후). 파이 전원 이상 없음(throttled 0x0). 서버는 드라이버 알람을 화면에 안 보여줌 |
| 저항센서 튐 | 9.3 kΩ(정상) ↔ 48~65 kΩ 가 0.1 s 주기로 반복 = 60 Hz 를 50 Hz 로 읽은 앨리어싱. opamp 가 12 V 전원(긴 GND 경로)일 때 발생 → **opamp 를 보드 3.3 V 로 옮기자 해결**(9.29~9.43 kΩ, sd 6.7 LSB, 튐 0). 칩 고장 아님 |
| 근육 채널 | 3.3 V 전원 후 60 Hz 험 pp≈430 mV(평균 211 mV), 바닥 0 mV 클리핑 의심. 노치로 sd 162→27 mV |

## 2026-10-30 — 근육 응답 전압 캡처(ADS1115 AIN2−AIN3) — 플래시·GND 단락 검증 완료

| 항목 | 상태 |
|---|---|
| 결정 | 10-22 보류안 중 **ADS1115 경로** 채택. opamp 출력 → AIN2, opamp 기준(중점 또는 GND) → AIN3, 차동 |
| 브랜치 | `feat/muscle-emg-capture`(`feat/dashboard-redesign` 에서 분기), **미커밋** |
| 펌웨어 | `sensor_muscle.h` 추가. `emg:cap:<ms>`(10~2000) 동안 저항센서 일시정지(`suspend/resume`) → 단발 변환 연속(860 SPS 설정, OS 비트로 완료 확인 → 중복·누락 없음, 실효 ≈700 S/s) → 응답으로 min/max/pp mV·포화 수, `s25007/board1/emg` 로 64샘플 조각 5 ms 간격 발행. `emg:pga:<mV>`(4096/2048/1024/512/256, NVS), `emg` 상태. 빌드 OK(플래시 81 %, RAM 17 %) |
| 게이트웨이 | `gateway/emg_packet.py`(조각 파서·조립·요약), `gateway/emg_capture.py`(CLI: 명령 발행 → CSV 저장). 대시보드 연동 없음 |
| 테스트 | `tests/test_emg_packet.py` 10건. 전체 868 passed, 기존 `test_ble_sensor.py` 2건 실패는 그대로 |
| 실기 (16:xx) | 12 V 끔·맥 USB(허브, `/dev/cu.usbmodem313201`)로 앱 영역 플래시. **AIN2·AIN3 GND 단락 1 s: n=562, 562 S/s, raw 평균 0.25·sd 0.54 LSB(pp 0.19 mV), 포화 0** → 바닥 노이즈 ≈1 LSB(62.5 µV @ ±2.048 V). 첫 시도의 +14 mV 는 AIN2/3 오배선이었음 |
| 실기 수정 | ① 시계를 첫 변환에서 시작(USB 꽂혔는데 포트 안 열면 시작 응답 Serial 출력이 수백 ms 막혀 n=0) ② 연속→단발 전환 첫 결과 폐기(저항센서 이전 값 raw 21469, 7.5 ms) ③ n=0 이면 err. 실효 속도는 ≈560 S/s(860 SPS 설정, I²C 폴링 오버헤드) |
| 파이 도구 | `~/emg_tool/`(emg_packet·emg_capture 복사본 + `run.py` 가 게이트웨이 config 의 MQTT 계정을 읽어 실행). 결과 CSV 도 여기 |
| 대시보드 카드 (15:00) | 게이트웨이 `emg_monitor.py`·`emg_routes.py`(`/api/emg/latest`·`/capture`), :8000 프록시, 오른쪽 열 펌프 아래 "근육 응답 전압" 카드(캡처·pp·min/max·포화·파형). 사용자 재시작 후 캡처 동작 확인 |
| 연속 측정 (15:33 복사) | 펌웨어 `emg:stream:on|off`(저항센서 일시정지, 64샘플마다 `…/emg/stream`, keepalive 20 s 없으면 자동 종료) · 게이트웨이 `emg_stream.py`(최근 10 s, 화면 폴링 = keepalive, 15 s 안 물으면 버림, 저항 측정 중 켜기 409 `emg_sensor_busy`) · 카드 [연속 측정] 버튼, 최근 5 s 스크롤. 펌웨어 빌드 82 % |
| 60 Hz 노치 (15:40 복사) | `emg_filter.py` — 실제 샘플 시각에서 [1, sin/cos·τsin/τcos × 60·120·180 Hz] 최소제곱 후 사인 성분만 뺌(0.5 s 창). 균일 보간+biquad 는 지터(1.6~3.1 ms) 때문에 험이 광대역 잡음으로 번져 버림. 1 V 험 → 잔여 ≈7 mV(±0.1 Hz 변동까지). 파이에서 2 s 캡처 140 ms. **포화된 험(오후 실측)에는 오히려 나빠짐(pp 2664→3243 mV)** — 하드웨어(기준 전극·계측 증폭기·RC 필터) 먼저. 카드 설정 "60 Hz 노치"(기본 켬, 흰 선 = 노치, 회색 = 원신호) |
| 적용·확인 (15:41~) | 사용자 서비스 재시작(15:41). 연속 측정 펌웨어 앱 영역 플래시(`/dev/cu.usbmodem31101`, 해시 검증). MQTT 시험: 3 s 켬 → 조각 25·샘플 1600·543 S/s·누락 0, 켜는 동안 저항 묶음 0 → 끈 뒤 18(정상 복귀), 보드 재부팅 없음. 사용자 화면 사용 로그에서 조각 140·dropped 0. ⚠ 첫 시험 때 한 번 보드가 재부팅된 흔적(sent 카운터 리셋) — 이후 두 번 재현 안 됨, 원인 미확정 |

### 다음 할 일
- 실측: `python -m gateway.emg_capture --host <브로커> --ms 500` 로 pp·포화 확인 → PGA 결정
- 860 SPS 로 M파 모양이 부족하면 별도 고속 ADC 검토. 자극 시각 동기(트리거 입력 D3) 필요 여부 결정

## 2026-10-29~30 — 대시보드(:8000) 화면 개편 (파이 배포 완료)

| 항목 | 상태 |
|---|---|
| 브랜치 | `feat/dashboard-redesign`(`feat/esp32-sensor-merge` 에서 분기), 커밋 320aeeb·789aaaa·319d3c7·0982ca6 + 이 로그. **미푸시** |
| 테마 | 검은 배경·흰 글자. 주 버튼 흰 바탕/검은 글자, 색은 상태(정지 빨강·실측 초록·경고 호박)에만. 차트는 로드 뒤 `uiApplyChartTheme()` 가 다시 칠함(주 계열 흰색, 짝 계열 회색, 중력벡터는 회색→흰색 시간 단계) |
| 조작 | 펌프·저항센서 카드는 값과 실행 버튼만, 설정은 [설정] **클릭** 시 카드 안에서 펼침(`<details class="sb-drop">`). 접혀도 요약줄로 적용값 표시 |
| 배치 | ≥1600px: 세 열을 화면 높이에 고정(직사각형), 넘치면 카드·열 안에서 스크롤, STOP ALL 은 왼쪽 열 바닥 고정. 오른쪽 열 = 저항센서+저항추이 한 카드 / 펌프 1·2 한 줄 / 이벤트·기록 관리·기록. 패널 제목·부제 삭제 |
| 비상정지 | 대시보드의 비상정지 카드·버튼 제거(명령은 `/console`·음성 비서에 남음). 래치가 걸렸을 때만 펌프 아래에 해제 줄(`#pumpLatchBanner`)이 나타남 |
| 계약 | baseline 줄 보존 계약 때문에 기존 줄은 그대로 두고, 맨 뒤 `<style id="uiTheme">`·스크립트가 덮어쓴다 |
| 테스트 | `tests/test_dashboard_redesign.py` 추가, 확장 테스트 473 passed |
| 파이 | `~/clinostat/static/index.html` 교체(해시 e174162…), 재시작 불필요(no-cache). 백업 `~/spacebio-backups/20261030_001454_dashboard_redesign/index.html` |

### 다음 할 일
- 브랜치 푸시·main 병합 여부 결정
- Operation Condition 의 [Run] 버튼(다른 컴포넌트가 그림)은 아직 주황 테두리
- 모바일 페이지(`/static/mobile.html`)는 개편 대상 밖

## 2026-10-22 — 저항센서를 펌프 보드(ESP32-C6)로 통합 (실기 적용·종단 확인)

| 항목 | 상태 |
|---|---|
| 결정 | Nano 33 BLE 제거. ADS1115 를 XIAO D4(SDA)/D5(SCL) I²C 직결, MQTT `s25007/board1/sensor` 로 발행 |
| 펌웨어 | `sensor_ads1115.h` 추가(Nano 와 같은 0x04E3 설정, 50 Hz, BLE 와 같은 21 B 패킷 ×10 묶음). 센서가 없어도 펌프 정상, `sensor:on/off` NVS. 빌드 OK(플래시 81 %). 16:00 맥(USB 허브)에서 **앱 영역만** 플래시, NVS 유지 확인. 시리얼 `sensor` → `ads=ok raw=25216` |
| 게이트웨이 | `gateway/mqtt_sensor.py`(BleClient 프로토콜 구현 → BleSensorSource 로직 재사용), config `ble.transport: bleak|mqtt`(기본 bleak). 대시보드·API 변경 없음(모드는 계속 BLE_LIVE) |
| 테스트 | `tests/test_mqtt_sensor.py` 13건 추가. 전체 823 passed, 기존 `test_ble_sensor.py` 2건 실패는 그대로 |
| 복구 | git 태그 `pre-sensor-merge-20261022`, 브랜치 `feat/esp32-sensor-merge`, 바이너리·원본 파일과 절차 `../backups/20261022_pre_sensor_merge/RESTORE.md` |
| 파이 | mqtt_sensor.py·runtime.py·runtime_config.py 배포, config `ble.transport: mqtt`, 재시작(16:04). 백업 `~/spacebio-backups/20261022_160345_pre_sensor_merge` |
| 종단 | `/api/sensor/configure`(BLE_LIVE, Rref 81k) → start → 20 s 연속 수신, raw 25214~25216(±1 LSB, 펌프 정지 상태), R ≈ 1.156 kΩ |

| 구동 시험 | 12 V 급전(맥 USB 분리), 파이에서 MQTT 직접 수집(`noise_test.py`). 두 펌프 `hz:600:30` → pos 17,757 = 램프 포함 기대값(≈17,750)과 일치 → **센서 읽기로 인한 펄스 손실 없음**. raw sd: 정지 전 5.15 / 600 Hz 5.39 / 정지 후 5.67 LSB → **펌프 구동에 따른 노이즈 증가 없음**. 샘플 49.8~50.0 /s |
| 관찰 | 12 V 급전 시 sd ≈ 5 LSB(맥 USB 급전 때 ±1 LSB), 평균 raw 25,216→25,261 이동 → 노이즈·오프셋은 전원(MP1584→3V3 = Vexc) 쪽. 게이트웨이는 Vexc 3.2 V 고정 가정 |

### 다음 할 일
- 펌프 실제 회전은 육안 확인 필요(pos 는 펄스 수일 뿐)
- Vexc 전원 노이즈: Vexc 를 ADS1115 한 채널로 같이 재서 비율식으로 계산하거나 Vexc 전용 LDO/RC 필터
- Rref(81 kΩ)가 시료(≈1.1 kΩ)보다 70배 커서 Vmid 가 Vexc 에 붙어 있음 → 5 LSB 가 약 0.7 % 로 증폭. 시료 범위에 맞춰 Rref 를 낮출 것

### 카메라 색 수정 (2026-10-22 16:18)
- 증상: 대시보드 카메라가 파랗게 나옴(피부·골판지가 파랑). 원인: `work/camera.py` 가 picamera2 `"RGB888"`(실제 메모리 순서 B,G,R = OpenCV BGR)을 한 번 더 뒤집어 R/B 교환
- 수정: 뒤집기 제거 + 회귀 테스트 `test_picamera_rgb888_frame_is_passed_through_unflipped`(확장 테스트 439 passed). 파이 배포·`clinostat` 재시작, 스냅샷 정상색 확인. 백업 `~/spacebio-backups/20261022_161800_camera`
- 카메라는 imx708_noir(IR 필터 없음) — 조명에 따라 약간 분홍빛은 정상

### 논의 기록 (2026-10-22 오후) — 근육 응답 전압 측정 추가 계획
- **운용 전제**: 측정 중에도 12 V(MP1584) 급전을 그대로 쓴다. 펌프와 측정이 겹칠 일은 거의 없다(겹쳐도 시험상 간섭 없음). 남는 노이즈원은 12 V→3V3 = Vexc(sd ≈ 5 LSB) → Rref 하향, Vexc 를 ADS1115 AIN 으로 같이 재는 비율 보정이 후보
- **목적**: 근육에 전기자극을 줬을 때 나오는 응답 전압을 읽는다. **사용자가 신호를 실측해 범위를 알려주기로 함**(최대 전압, 음전압 여부, 파형 속도, GND 공유 여부)
- **남은 자원**: XIAO 빈 핀 D0(GPIO0, ADC 가능)·D3(GPIO21)·D6(GPIO16, 부팅 시 UART TX 펄스). ADS1115 AIN2·AIN3 비어 있음
- **ADS1115 제약**: VDD 는 3.3 V 유지(5 V 면 I²C 풀업이 ESP32 핀을 5 V 로 당김). 입력은 0~VDD, 음전압 불가 → 양방향 신호는 opamp 오프셋/레벨시프트 필요. ADC 하나를 MUX 로 공유 — 센서 연속변환 사이에 끼워 읽음(1회 2~3 ms). 최대 860 SPS 라 ms 단위 펄스·빠른 파형에는 부적합 → 그 경우 별도 ADC(빠른 외장 ADC 또는 ESP32 내장 ADC + opamp) 검토

---

## 2026-10-21 — 저항센서 새 보드 연결 성공

| 항목 | 상태 |
|---|---|
| 보드 | Nano 33 BLE(시리얼 711432C98D98544F, 주소 32:C9:8D:98:54:4F) + 새 센서 보드. ADS1115 0x48 인식, **TMP102 없음**(25 °C 고정, 비치명), VREF 1.63 V·Vcc 3.26 V |
| 원인 | 보드에 BLE 없는 시리얼 점검 스케치가 올라가 있어 광고가 안 보였음(부팅 로그에 `[BLE]` 줄 없음). 소스 없음, 부트로더가 읽기 미지원이라 백업 못 함 |
| 조치 | 파이에서 `~/sensor_ble_firmware_20261020.bin` bossac 업로드 → 광고 `ResistanceSensor` RSSI −39 |
| 연결 | `POST /api/sensor/start` → 1차 실패, 2차 성공(기존과 같음). `:8000/api/spacebio/sensor/status` 로 30 s 연속 수신, raw_adc ≈ 25,085, R ≈ 1.36 kΩ(Rref 81 kΩ), ΔR/R0 ±1 % |
| 전원 | **VIN 5 V 외부 급전**(USB 분리)으로 전환. 광고 RSSI −44, 5분 연속 수신·정지 없음, raw_adc 변화 없음 |
| 열린 문제 | 17:28 대시보드 세션 시작 시 센서 설정이 **Rref 10000** 으로 바뀜(R 1370→169 Ω, raw 동일). 새 보드의 실제 Rref 확인 후 맞출 것 |
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
