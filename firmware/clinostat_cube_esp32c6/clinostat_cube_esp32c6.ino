/*
 * clinostat_cube_esp32c6 — 무선 펌프 컨트롤러 (구동부 재작성 2026-07-29, 2펌프 2026-10-15)
 *
 * 프로토콜은 S25007_Manual_RevB.pdf 를 그대로 지킨다 → 기존 Flutter 앱과
 * spacebio-gateway 가 수정 없이 붙는다.
 *   펌프 1: 명령 s25007/board1/cmd     · 상태 s25007/board1/status     (2초 주기)
 *   펌프 2: 명령 s25007/board1/p2/cmd  · 상태 s25007/board1/p2/status  (2초 주기)
 *   상태 필드 pump,ip,rssi,run,mode,current_spm,spin_rem,manual_rem,spinsteps,spinspm,spm
 *            + pos (게이트웨이가 요구하는 확장 필드)
 *
 * ── 왜 다시 썼나 ──────────────────────────────────────────────────────────
 * 원본 펌웨어 소스가 없어 매뉴얼로부터 프로토콜만 재현했고, **배선 문서가 없어
 * STEP/DIR/EN 핀은 추측이었다.** 그 결과 "WiFi/MQTT 는 되는데 모터가 안 도는"
 * 상태가 오래 이어졌다. 그래서 이번 재작성의 목표는 기능 추가가 아니라
 * **모르는 배선을 재flash 없이 찾아낼 수 있게 만드는 것**이다.
 *
 *   1) 핀·극성·펄스폭·브로커 주소를 전부 NVS 에 두고 런타임 변경 (pump_config.h)
 *   2) 모든 펄스를 단일 비블로킹 엔진에서 생성 → pos 가 항상 실제와 일치 (pump_drive.h)
 *   3) 오프라인에서도 USB 시리얼로 전 명령 사용 가능 (pump_commands.h)
 *   4) MQTT 재접속이 loop 를 막지 않음 (pump_net.h)
 *
 * ── 펌프 2대 (2026-10-15) ─────────────────────────────────────────────────
 *   보드 하나가 DRV8825 두 개를 몬다. 엔진 인스턴스가 펌프 수만큼 있고 각자 독립된
 *   핀·속도·pos 를 가진다. 시리얼 명령은 `p1:`/`p2:` 접두어로 대상을 고른다(없으면 펌프 1).
 *
 * ── 저항센서 (2026-10-22) ─────────────────────────────────────────────────
 *   Nano 33 BLE 를 빼고 ADS1115 를 D4/D5(I²C)에 직결. `s25007/board1/sensor` 로 발행.
 *   상세는 sensor_ads1115.h. 시리얼/MQTT 명령 `sensor`, `sensor:on|off`.
 *
 * ── 라이브러리 ────────────────────────────────────────────────────────────
 *   PubSubClient  (AccelStepper 는 더 이상 쓰지 않는다 — 자체 엔진으로 대체)
 *
 * ── 처음 붙일 때 ──────────────────────────────────────────────────────────
 *   USB 시리얼(115200)에 `help` 를 치면 명령 목록이 나온다.
 *   배선을 모르면 `p2:sweep` → 모터가 반응한 구간의 gpio 를 `p2:pin:step:<gpio>` 로 고정.
 */

#include <Preferences.h>
#include "pump_config.h"
#include "pump_drive.h"
#include "pump_net.h"
#include "sensor_ads1115.h"

// ---- 전역 (pump_commands.h 가 extern 으로 참조) ----
PumpConfig  gCfg;
PumpDrive   gPumps[PUMP_COUNT];
Preferences gPrefs;
PumpNet     gNet;
SensorAds1115 gSensor;
bool        gLedState = false;
int         gLedGpio  = LED_BUILTIN;      // XIAO ESP32-C6 내장 LED

static void reply(const String& msg);
ReplyFn gReply = reply;

#include "pump_commands.h"

// 명령 응답은 시리얼에 항상, MQTT 에는 연결돼 있을 때만(status 와 별도 토픽).
static void reply(const String& msg) {
  Serial.println(msg);
  gNet.publishReply(msg);
}

static bool publishSensorBatch(const uint8_t* data, size_t len) {
  return gNet.publishSensor(data, len);
}

static unsigned long lastStatusMs = 0;

static bool anyPumpRunning() {
  for (int i = 0; i < PUMP_COUNT; i++) if (gPumps[i].running()) return true;
  return false;
}

// 매뉴얼 RevB §5 형식 + pump(펌프 번호) + pos(게이트웨이 요구 확장).
// pos = 실제로 발생시킨 STEP 펄스 누적수.
// ⚠ 이 값이 늘어난다는 것은 MCU 가 펄스를 냈다는 뜻일 뿐, 모터가 돌았다는 증거가 아니다.
//   (엔코더가 없어 회전은 원격으로 확인할 수 없다 — 반드시 눈/손으로 확인할 것)
String buildStatusLine(int idx) {
  const PumpDrive&   p = gPumps[idx];
  const MotorConfig& m = gCfg.motor[idx];
  char buf[320];
  snprintf(buf, sizeof(buf),
    "pump=%d,ip=%s,rssi=%d,run=%s,mode=%s,current_spm=%ld,spin_rem=%ld,manual_rem=%ld,"
    "spinsteps=%ld,spinspm=%ld,spm=%ld,pos=%ld",
    idx + 1,
    gNet.ip().toString().c_str(), gNet.rssi(),
    p.running() ? "on" : "off",
    p.modeName(), p.currentSpm(),
    p.spinRem(), p.manualRem(),
    m.spinsteps, m.spinspm, m.spm,
    p.position());
  return String(buf);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== clinostat_cube boot (pump drive rev.2026-10-15, 2 pumps) ===");

  pinMode(gLedGpio, OUTPUT);
  digitalWrite(gLedGpio, LOW);

  gPrefs.begin("s25007", false);
  gCfg.load(gPrefs);

  for (int i = 0; i < PUMP_COUNT; i++) {
    gPumps[i].begin(&gCfg, i);
    gPumps[i].stop();           // 부팅 직후엔 반드시 정지 상태에서 시작
    replyWiring(i);
    Serial.printf("펌프 %d 설정: spm=%ld spinsteps=%ld spinspm=%ld\n", i + 1,
                  gCfg.motor[i].spm, gCfg.motor[i].spinsteps, gCfg.motor[i].spinspm);
  }
  Serial.printf("브로커호스트=%s 고정IP=%s\n",
                gCfg.brokerHost, strlen(gCfg.brokerIp) ? gCfg.brokerIp : "(없음)");
  Serial.println("USB 시리얼로도 모든 명령을 쓸 수 있습니다. `help` 입력. (p1:/p2: 접두어로 펌프 선택)");

  gNet.begin(&gCfg, handleMqttCommand);
  gSensor.begin(&gPrefs, publishSensorBatch);
}

void loop() {
  // 순서가 중요하다: 구동 엔진을 먼저 돌려 펄스 타이밍이 네트워크 처리에 밀리지 않게 한다.
  for (int i = 0; i < PUMP_COUNT; i++) gPumps[i].tick();
  pollSerialCommands();
  for (int i = 0; i < PUMP_COUNT; i++) gPumps[i].tick();
  gNet.tick(anyPumpRunning());   // 구동 중엔 블로킹 재접속을 미룬다 (펄스 타이밍 우선)
  for (int i = 0; i < PUMP_COUNT; i++) gPumps[i].tick();
  gSensor.tick();                // I²C 읽기 ≈0.1 ms, 20 ms 마다

  if (millis() - lastStatusMs >= 2000) {
    lastStatusMs = millis();
    for (int i = 0; i < PUMP_COUNT; i++) gNet.publishStatus(i, buildStatusLine(i));
  }
}
