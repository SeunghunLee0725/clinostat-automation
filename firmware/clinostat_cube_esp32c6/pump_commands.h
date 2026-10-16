#pragma once
/*
 * pump_commands.h — 명령 파서. MQTT 와 USB 시리얼이 **같은 어휘**를 쓴다.
 *
 * 이전 구현엔 Serial.read 계열이 전혀 없어 USB 가 로그 전용이었다. 그래서 네트워크가
 * 막히면 보드를 전혀 조작할 수 없었다(2026-07-29 방화벽 사고 때 실제로 그랬다).
 * → 같은 파서를 시리얼에도 물린다. 브로커가 없어도 USB 만으로 전부 진단·구동 가능.
 *
 * 매뉴얼(RevB §4) 명령은 의미까지 그대로 유지한다 — 기존 앱/게이트웨이 호환:
 *   start · stop · once · manualstep:N · spm:N · spinsteps:N · spinspm:N · led:on|off
 *
 * 추가한 명령 (진단·배선 탐색용):
 *   hz:<hz>[:<sec>]   목표 펄스율 직접 지정. 매뉴얼 SPM 상한(2400=40펄스/초)으로는
 *                     마이크로스텝 때문에 육안 확인이 불가능해서 필요하다.
 *   sweep[:<hz>[:<ms>]]  헤더 핀을 하나씩 펄스해 실제 STEP 배선을 찾는다.
 *   pin:step|dir|en:<gpio>   배선을 런타임에 바꾼다(-1 = 미연결). NVS 저장.
 *   pinD:step|dir|en:<D번호> 같은 것을 D핀 번호로 지정 (D9 → GPIO20).
 *   enpol:low|high    ENABLE 극성.
 *   pulse:<us>        STEP HIGH 폭. 드라이버가 펄스를 놓치는 의심이 들면 올린다.
 *   ramp:<hz>[,<ms>]  출발 램프 시작 펄스율과 길이. 저속에서 못 도는 모터는 목표와 같게.
 *   dir:fwd|rev       회전 방향.
 *   hold:on|off       코일 여자만 켜고 끄기 (EN 배선된 경우).
 *   pincheck:<gpio>   패드 무결성.
 *   broker:<ip>[:<port>] / brokerhost:<name>   브로커 주소. 재flash 없이 바꾼다.
 *   resetpos · wiring · status · help
 *
 * 2026-10-15 — 펌프 2대. 모터 명령은 **대상 펌프 접두어** `p1:` / `p2:` 를 받는다.
 *   접두어가 없으면 펌프 1 이다 (매뉴얼·기존 앱·게이트웨이 호환). MQTT 로는 토픽이
 *   대상을 정하므로(`s25007/board1/p2/cmd` → 펌프 2) 접두어가 필요 없지만, 시리얼에는
 *   토픽이 없어 접두어가 유일한 수단이다. `stopall` 은 두 펌프를 함께 멈춘다.
 *   보드 공통 명령(led·pincheck·broker·brokerhost·help·status)은 접두어를 무시한다.
 */

#include <Preferences.h>
#include "pump_config.h"
#include "pump_drive.h"

// 이 모듈이 조작하는 대상 — .ino 에서 주입한다.
extern PumpConfig  gCfg;
extern PumpDrive   gPumps[PUMP_COUNT];
extern Preferences gPrefs;
extern bool        gLedState;
extern int         gLedGpio;

// 상태 한 줄을 만든다(.ino 에 정의). 브로커가 없을 때 시리얼로 pos 를 확인하는
// 유일한 수단이므로 명령 파서에서도 쓸 수 있어야 한다.
String buildStatusLine(int idx);

// 명령에 대한 사람이 읽는 응답. 시리얼로 출력하고, MQTT 로도 한 줄 보낸다.
// (ReplyFn 은 pump_config.h 에 정의 — 이 헤더보다 먼저 필요하다)
extern ReplyFn gReply;

inline String cmdArg(const String& c, int fromIndex) { return c.substring(fromIndex); }

// "a:b:c" 에서 i 번째 토큰. 없으면 빈 문자열.
inline String cmdToken(const String& s, int idx) {
  int start = 0;
  for (int i = 0; i < idx; i++) {
    int p = s.indexOf(':', start);
    if (p < 0) return "";
    start = p + 1;
  }
  int p = s.indexOf(':', start);
  return (p < 0) ? s.substring(start) : s.substring(start, p);
}

// 응답에 붙는 펌프 표시. 펌프 1 은 기존 응답 그대로(호환), 펌프 2 는 "p2 " 접두어.
inline String pumpTag(int idx) { return idx == 0 ? String("") : "p" + String(idx + 1) + " "; }

inline void replyFor(int idx, const String& msg) { gReply(pumpTag(idx) + msg); }

inline void replyWiring(int idx) {
  const MotorConfig& m = gCfg.motor[idx];
  String s = "wiring step=" + String(m.stepGpio) +
             " dir="   + String(m.dirGpio) +
             " en="    + String(m.enGpio) +
             " enpol=" + String(gCfg.enActiveLow ? "low" : "high") +
             " pulse=" + String(gCfg.pulseUs) + "us" +
             " ramp="  + String(gCfg.rampStartHz) + "Hz/" + String(gCfg.rampMs) + "ms" +
             " dirval=" + String(gPumps[idx].direction() ? "fwd" : "rev");
  replyFor(idx, s);
}

// 배선 변경은 즉시 적용 + NVS 저장. 어느 핀인지 되돌려 알려준다.
// 다른 펌프가 이미 쓰는 핀은 거부한다 — 두 모터가 한 STEP 선을 공유하면 개별 운전이 깨진다.
inline bool setWiringPin(int idx, const String& which, int gpio) {
  for (int i = 0; i < PUMP_COUNT; i++) {
    if (i != idx && gCfg.motor[i].usesGpio(gpio)) {
      replyFor(idx, "err gpio " + String(gpio) + " 은(는) 펌프 " + String(i + 1) + " 이 쓰고 있습니다");
      return false;
    }
  }
  MotorConfig& m = gCfg.motor[idx];
  if (which == "step") {
    if (gpio < 0) { replyFor(idx, "err step 핀은 미연결일 수 없습니다"); return false; }
    m.stepGpio = gpio;
  } else if (which == "dir") {
    m.dirGpio = gpio;
  } else if (which == "en") {
    m.enGpio = gpio;
  } else {
    replyFor(idx, "err pin 대상은 step|dir|en");
    return false;
  }
  gCfg.saveWiring(gPrefs, idx);
  gPumps[idx].applyWiring();
  replyWiring(idx);
  return true;
}

inline void stopAllPumps() {
  for (int i = 0; i < PUMP_COUNT; i++) gPumps[i].stop();
}

// 특정 펌프를 대상으로 한 명령. 접두어는 이미 벗겨진 상태로 들어온다.
inline void handleCommandFor(int idx, const String& raw) {
  String c = raw;
  c.trim();
  if (c.length() == 0) return;
  if (idx < 0 || idx >= PUMP_COUNT) { gReply("err 펌프 번호는 1~" + String(PUMP_COUNT)); return; }

  PumpDrive&   pump = gPumps[idx];
  MotorConfig& m    = gCfg.motor[idx];

  // ---- 두 펌프 공통 ----
  if (c == "stopall") { stopAllPumps(); gReply("ok stopall"); return; }

  // ---- 매뉴얼 RevB §4 : 모터 기본 제어 ----
  if (c == "start")        { pump.start();  replyFor(idx, "ok start"); }
  else if (c == "stop")    { pump.stop();   replyFor(idx, "ok stop"); }
  else if (c == "once")    { pump.once();   replyFor(idx, "ok once"); }

  // ---- 매뉴얼 : 수동 스텝 ----
  else if (c.startsWith("manualstep:")) {
    long n = cmdArg(c, 11).toInt();
    if (n <= 0) { replyFor(idx, "err manualstep 값은 1 이상"); return; }
    pump.manual(n);
    replyFor(idx, "ok manualstep " + String(n));
  }

  // ---- 매뉴얼 : 설정값 (NVS 저장) ----
  else if (c.startsWith("spm:")) {
    m.spm = gCfg.clampSpm(cmdArg(c, 4).toInt());
    gCfg.saveDrive(gPrefs, idx);
    if (pump.mode() == PM_RUN) pump.start();   // 즉시 반영
    replyFor(idx, "ok spm " + String(m.spm));
  }
  else if (c.startsWith("spinsteps:")) {
    m.spinsteps = cmdArg(c, 10).toInt();
    gCfg.saveDrive(gPrefs, idx);
    replyFor(idx, "ok spinsteps " + String(m.spinsteps));
  }
  else if (c.startsWith("spinspm:")) {
    m.spinspm = gCfg.clampSpm(cmdArg(c, 8).toInt());
    gCfg.saveDrive(gPrefs, idx);
    replyFor(idx, "ok spinspm " + String(m.spinspm));
  }

  // ---- 매뉴얼 : 부가 기능 (보드 공통) ----
  else if (c == "led:on")  { gLedState = true;  digitalWrite(gLedGpio, HIGH); gReply("ok led on"); }
  else if (c == "led:off") { gLedState = false; digitalWrite(gLedGpio, LOW);  gReply("ok led off"); }

  // ---- 확장 : 육안 확인용 펄스율 직접 지정 ----
  else if (c.startsWith("hz:")) {
    float hz  = cmdToken(c, 1).toFloat();
    String s2 = cmdToken(c, 2);
    float sec = s2.length() ? s2.toFloat() : 10.0f;
    if (hz <= 0) { replyFor(idx, "err hz 값은 1 이상"); return; }
    pump.runHz(hz, sec);
    replyFor(idx, "ok hz " + String(hz, 0) + " for " + String(sec, 1) + "s");
  }

  // ---- 확장 : 배선 탐색 ----
  else if (c == "sweep" || c.startsWith("sweep:")) {
    String a1 = cmdToken(c, 1), a2 = cmdToken(c, 2);
    float hz = a1.length() ? a1.toFloat() : 250.0f;
    unsigned long ms = a2.length() ? (unsigned long)a2.toInt() : 4000UL;
    pump.sweep(hz, ms);
    replyFor(idx, "ok sweep " + String(hz, 0) + "Hz " + String(ms) + "ms/pin, "
             + String(XIAO_D_COUNT) + "핀 (다른 펌프 핀은 건너뜀)");
  }

  // ---- 확장 : 배선 런타임 변경 ----
  else if (c.startsWith("pin:")) {
    String which = cmdToken(c, 1), val = cmdToken(c, 2);
    if (!val.length()) { replyFor(idx, "err 형식: pin:step|dir|en:<gpio>"); return; }
    setWiringPin(idx, which, val.toInt());
  }
  else if (c.startsWith("pinD:")) {
    String which = cmdToken(c, 1), val = cmdToken(c, 2);
    if (!val.length()) { replyFor(idx, "err 형식: pinD:step|dir|en:<D번호>"); return; }
    int d = val.toInt();
    int g = dPinToGpio(d);
    if (g < 0) { replyFor(idx, "err D핀 범위는 0~" + String(XIAO_D_COUNT - 1)); return; }
    setWiringPin(idx, which, g);
  }
  else if (c.startsWith("enpol:")) {
    String v = cmdArg(c, 6);
    gCfg.enActiveLow = (v == "low");           // 보드 공통 — 두 드라이버가 같은 극성
    gCfg.saveWiring(gPrefs, idx);
    for (int i = 0; i < PUMP_COUNT; i++) gPumps[i].applyWiring();
    replyWiring(idx);
  }
  else if (c.startsWith("pulse:")) {
    int us = cmdArg(c, 6).toInt();
    if (us < 1 || us > 5000) { replyFor(idx, "err pulse 는 1~5000 us"); return; }
    gCfg.pulseUs = us;                         // 보드 공통
    gCfg.saveWiring(gPrefs, idx);
    replyWiring(idx);
  }
  else if (c.startsWith("ramp:")) {
    String v = cmdArg(c, 5);
    int comma = v.indexOf(',');
    int hz = (comma < 0 ? v : v.substring(0, comma)).toInt();
    int ms = (comma < 0) ? gCfg.rampMs : v.substring(comma + 1).toInt();
    if (hz < 1 || hz > 20000 || ms < 0 || ms > 60000) { replyFor(idx, "err ramp 는 1~20000 Hz, 0~60000 ms"); return; }
    gCfg.rampStartHz = hz;                     // 보드 공통
    gCfg.rampMs      = ms;
    gCfg.saveWiring(gPrefs, idx);
    replyWiring(idx);
  }
  else if (c.startsWith("dir:")) {
    String v = cmdArg(c, 4);
    pump.setDirection(!(v == "rev" || v == "1" || v == "back"));
    replyWiring(idx);
  }
  else if (c.startsWith("hold:")) {
    bool on = (cmdArg(c, 5) == "on");
    if (pump.hold(on)) replyFor(idx, String("ok hold ") + (on ? "on" : "off"));
    else replyFor(idx, "warn EN 미배선 — 코일 여자를 끌 수 없습니다. VMOT 를 물리적으로 내리세요.");
  }
  else if (c.startsWith("pincheck:")) {
    int g = cmdArg(c, 9).toInt();
    int hi = -1, lo = -1;
    bool ok = PumpDrive::pinCheck(g, hi, lo);
    gReply("pincheck gpio=" + String(g) + " hi=" + String(hi) + " lo=" + String(lo)
           + (ok ? " 정상" : " 이상(단락/과부하 의심)"));
    for (int i = 0; i < PUMP_COUNT; i++) gPumps[i].applyWiring();   // 검사로 흐트러진 핀 상태 복구
  }

  // ---- 확장 : 브로커 주소 (재flash 없이, 보드 공통) ----
  else if (c.startsWith("broker:")) {
    String v = cmdArg(c, 7);
    int colon = v.indexOf(':');
    if (colon >= 0) {
      int port = v.substring(colon + 1).toInt();
      if (port < 1 || port > 65535) { gReply("err broker 포트는 1~65535"); return; }
      gCfg.brokerPort = port;
      v = v.substring(0, colon);
    }
    v.toCharArray(gCfg.brokerIp, sizeof(gCfg.brokerIp));
    gCfg.saveBroker(gPrefs);
    gReply("ok broker " + String(gCfg.brokerIp) + ":" + String(gCfg.brokerPort) + " (다음 재접속부터)");
  }
  else if (c.startsWith("brokerhost:")) {
    String v = cmdArg(c, 11);
    v.toCharArray(gCfg.brokerHost, sizeof(gCfg.brokerHost));
    gCfg.saveBroker(gPrefs);
    gReply("ok brokerhost " + String(gCfg.brokerHost) + " (다음 재접속부터)");
  }

  else if (c == "resetpos") { pump.resetPosition(); replyFor(idx, "ok pos=0"); }
  else if (c == "wiring")   { for (int i = 0; i < PUMP_COUNT; i++) replyWiring(i); }
  else if (c == "status")   { for (int i = 0; i < PUMP_COUNT; i++) gReply(buildStatusLine(i)); }
  else if (c == "help") {
    gReply("cmd: [p1:|p2:]start stop once manualstep:N spm:N spinsteps:N spinspm:N  stopall  led:on|off");
    gReply("     [p1:|p2:]hz:<hz>[:<sec>] sweep[:hz[:ms]] dir:fwd|rev hold:on|off resetpos");
    gReply("     [p1:|p2:]pin:step|dir|en:<gpio> pinD:step|dir|en:<D> enpol:low|high pulse:<us> ramp:<hz>[,<ms>]");
    gReply("     pincheck:<gpio> broker:<ip>[:<port>] brokerhost:<name> wiring status help");
    gReply("     접두어 없음 = 펌프 1. MQTT 는 토픽이 대상을 정한다 (…/board1/p2/cmd = 펌프 2).");
  }
  else {
    replyFor(idx, "err 알 수 없는 명령: " + c + "  (help 참고)");
  }
}

// 접두어 `p1:`/`p2:` 로 대상 펌프를 고른다. 없으면 펌프 1 (호환).
inline void handleCommand(const String& raw) {
  String c = raw;
  c.trim();
  if (c.length() == 0) return;
  if ((c.charAt(0) == 'p' || c.charAt(0) == 'P') && c.length() >= 3 && c.charAt(2) == ':'
      && isDigit(c.charAt(1))) {
    int n = c.charAt(1) - '0';
    if (n < 1 || n > PUMP_COUNT) { gReply("err 펌프 번호는 1~" + String(PUMP_COUNT)); return; }
    handleCommandFor(n - 1, c.substring(3));
    return;
  }
  handleCommandFor(0, c);
}

// MQTT 콜백 — 토픽이 대상을 정한다. 펌프 1 토픽은 접두어도 허용한다(앱 호환).
inline void handleMqttCommand(int idx, const String& msg) {
  if (idx == 0) handleCommand(msg);
  else          handleCommandFor(idx, msg);
}

// USB 시리얼에서 한 줄씩 읽어 같은 파서로 넘긴다. 비블로킹.
inline void pollSerialCommands() {
  static String buf;
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (buf.length()) { handleCommand(buf); buf = ""; }
    } else if (buf.length() < 120) {
      buf += ch;
    }
  }
}
