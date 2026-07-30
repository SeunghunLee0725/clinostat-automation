/*
 * clinostat_cube_esp32c6 — 무선 펌프 컨트롤러 (구동부 재작성, 2026-07-29)
 *
 * 프로토콜은 S25007_Manual_RevB.pdf 를 그대로 지킨다 → 기존 Flutter 앱과
 * spacebio-gateway 가 수정 없이 붙는다.
 *   명령 토픽 s25007/board1/cmd  ·  상태 토픽 s25007/board1/status (2초 주기)
 *   상태 필드 ip,rssi,run,mode,current_spm,spin_rem,manual_rem,spinsteps,spinspm,spm
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
 * ── 라이브러리 ────────────────────────────────────────────────────────────
 *   PubSubClient  (AccelStepper 는 더 이상 쓰지 않는다 — 자체 엔진으로 대체)
 *
 * ── 처음 붙일 때 ──────────────────────────────────────────────────────────
 *   USB 시리얼(115200)에 `help` 를 치면 명령 목록이 나온다.
 *   배선을 모르면 `sweep` → 모터가 반응한 구간의 gpio 를 `pin:step:<gpio>` 로 고정.
 */

#include <Preferences.h>
#include "pump_config.h"
#include "pump_drive.h"
#include "pump_net.h"

// ---- 전역 (pump_commands.h 가 extern 으로 참조) ----
PumpConfig  gCfg;
PumpDrive   gPump;
Preferences gPrefs;
PumpNet     gNet;
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

static unsigned long lastStatusMs = 0;

// 매뉴얼 RevB §5 형식 + pos(게이트웨이 요구 확장).
// pos = 실제로 발생시킨 STEP 펄스 누적수.
// ⚠ 이 값이 늘어난다는 것은 MCU 가 펄스를 냈다는 뜻일 뿐, 모터가 돌았다는 증거가 아니다.
//   (엔코더가 없어 회전은 원격으로 확인할 수 없다 — 반드시 눈/손으로 확인할 것)
String buildStatusLine() {
  char buf[300];
  snprintf(buf, sizeof(buf),
    "ip=%s,rssi=%d,run=%s,mode=%s,current_spm=%ld,spin_rem=%ld,manual_rem=%ld,"
    "spinsteps=%ld,spinspm=%ld,spm=%ld,pos=%ld",
    gNet.ip().toString().c_str(), gNet.rssi(),
    gPump.running() ? "on" : "off",
    gPump.modeName(), gPump.currentSpm(),
    gPump.spinRem(), gPump.manualRem(),
    gCfg.spinsteps, gCfg.spinspm, gCfg.spm,
    gPump.position());
  return String(buf);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== clinostat_cube boot (pump drive rev.2026-07-29) ===");

  pinMode(gLedGpio, OUTPUT);
  digitalWrite(gLedGpio, LOW);

  gPrefs.begin("s25007", false);
  gCfg.load(gPrefs);

  gPump.begin(&gCfg);
  gPump.stop();                 // 부팅 직후엔 반드시 정지 상태에서 시작

  replyWiring();
  Serial.printf("설정: spm=%ld spinsteps=%ld spinspm=%ld  브로커호스트=%s 고정IP=%s\n",
                gCfg.spm, gCfg.spinsteps, gCfg.spinspm,
                gCfg.brokerHost, strlen(gCfg.brokerIp) ? gCfg.brokerIp : "(없음)");
  Serial.println("USB 시리얼로도 모든 명령을 쓸 수 있습니다. `help` 입력.");

  gNet.begin(&gCfg, handleCommand);
}

void loop() {
  // 순서가 중요하다: 구동 엔진을 먼저 돌려 펄스 타이밍이 네트워크 처리에 밀리지 않게 한다.
  gPump.tick();
  pollSerialCommands();
  gPump.tick();
  gNet.tick(gPump.running());   // 구동 중엔 블로킹 재접속을 미룬다 (펄스 타이밍 우선)

  if (millis() - lastStatusMs >= 2000) {
    lastStatusMs = millis();
    gNet.publishStatus(buildStatusLine());
  }
}
