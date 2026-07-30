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
 *   dir:fwd|rev       회전 방향.
 *   hold:on|off       코일 여자만 켜고 끄기 (EN 배선된 경우).
 *   pincheck:<gpio>   패드 무결성.
 *   broker:<ip> / brokerhost:<name>   브로커 주소. 재flash 없이 바꾼다.
 *   resetpos · wiring · status · help
 */

#include <Preferences.h>
#include "pump_config.h"
#include "pump_drive.h"

// 이 모듈이 조작하는 대상 — .ino 에서 주입한다.
extern PumpConfig  gCfg;
extern PumpDrive   gPump;
extern Preferences gPrefs;
extern bool        gLedState;
extern int         gLedGpio;

// 상태 한 줄을 만든다(.ino 에 정의). 브로커가 없을 때 시리얼로 pos 를 확인하는
// 유일한 수단이므로 명령 파서에서도 쓸 수 있어야 한다.
String buildStatusLine();

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

inline void replyWiring() {
  String m = "wiring step=" + String(gCfg.stepGpio) +
             " dir="   + String(gCfg.dirGpio) +
             " en="    + String(gCfg.enGpio) +
             " enpol=" + String(gCfg.enActiveLow ? "low" : "high") +
             " pulse=" + String(gCfg.pulseUs) + "us" +
             " dirval=" + String(gPump.direction() ? "fwd" : "rev");
  gReply(m);
}

// 배선 변경은 즉시 적용 + NVS 저장. 어느 핀인지 되돌려 알려준다.
inline bool setWiringPin(const String& which, int gpio) {
  if (which == "step") {
    if (gpio < 0) { gReply("err step 핀은 미연결일 수 없습니다"); return false; }
    gCfg.stepGpio = gpio;
  } else if (which == "dir") {
    gCfg.dirGpio = gpio;
  } else if (which == "en") {
    gCfg.enGpio = gpio;
  } else {
    gReply("err pin 대상은 step|dir|en");
    return false;
  }
  gCfg.saveWiring(gPrefs);
  gPump.applyWiring();
  replyWiring();
  return true;
}

inline void handleCommand(const String& raw) {
  String c = raw;
  c.trim();
  if (c.length() == 0) return;

  // ---- 매뉴얼 RevB §4 : 모터 기본 제어 ----
  if (c == "start")        { gPump.start();  gReply("ok start"); }
  else if (c == "stop")    { gPump.stop();   gReply("ok stop"); }
  else if (c == "once")    { gPump.once();   gReply("ok once"); }

  // ---- 매뉴얼 : 수동 스텝 ----
  else if (c.startsWith("manualstep:")) {
    long n = cmdArg(c, 11).toInt();
    if (n <= 0) { gReply("err manualstep 값은 1 이상"); return; }
    gPump.manual(n);
    gReply("ok manualstep " + String(n));
  }

  // ---- 매뉴얼 : 설정값 (NVS 저장) ----
  else if (c.startsWith("spm:")) {
    gCfg.spm = gCfg.clampSpm(cmdArg(c, 4).toInt());
    gCfg.saveDrive(gPrefs);
    if (gPump.mode() == PM_RUN) gPump.start();   // 즉시 반영
    gReply("ok spm " + String(gCfg.spm));
  }
  else if (c.startsWith("spinsteps:")) {
    gCfg.spinsteps = cmdArg(c, 10).toInt();
    gCfg.saveDrive(gPrefs);
    gReply("ok spinsteps " + String(gCfg.spinsteps));
  }
  else if (c.startsWith("spinspm:")) {
    gCfg.spinspm = gCfg.clampSpm(cmdArg(c, 8).toInt());
    gCfg.saveDrive(gPrefs);
    gReply("ok spinspm " + String(gCfg.spinspm));
  }

  // ---- 매뉴얼 : 부가 기능 ----
  else if (c == "led:on")  { gLedState = true;  digitalWrite(gLedGpio, HIGH); gReply("ok led on"); }
  else if (c == "led:off") { gLedState = false; digitalWrite(gLedGpio, LOW);  gReply("ok led off"); }

  // ---- 확장 : 육안 확인용 펄스율 직접 지정 ----
  else if (c.startsWith("hz:")) {
    float hz  = cmdToken(c, 1).toFloat();
    String s2 = cmdToken(c, 2);
    float sec = s2.length() ? s2.toFloat() : 10.0f;
    if (hz <= 0) { gReply("err hz 값은 1 이상"); return; }
    gPump.runHz(hz, sec);
    gReply("ok hz " + String(hz, 0) + " for " + String(sec, 1) + "s");
  }

  // ---- 확장 : 배선 탐색 ----
  else if (c == "sweep" || c.startsWith("sweep:")) {
    String a1 = cmdToken(c, 1), a2 = cmdToken(c, 2);
    float hz = a1.length() ? a1.toFloat() : 250.0f;
    unsigned long ms = a2.length() ? (unsigned long)a2.toInt() : 4000UL;
    gPump.sweep(hz, ms);
    gReply("ok sweep " + String(hz, 0) + "Hz " + String(ms) + "ms/pin, "
           + String(XIAO_D_COUNT) + "핀");
  }

  // ---- 확장 : 배선 런타임 변경 ----
  else if (c.startsWith("pin:")) {
    String which = cmdToken(c, 1), val = cmdToken(c, 2);
    if (!val.length()) { gReply("err 형식: pin:step|dir|en:<gpio>"); return; }
    setWiringPin(which, val.toInt());
  }
  else if (c.startsWith("pinD:")) {
    String which = cmdToken(c, 1), val = cmdToken(c, 2);
    if (!val.length()) { gReply("err 형식: pinD:step|dir|en:<D번호>"); return; }
    int d = val.toInt();
    int g = dPinToGpio(d);
    if (g < 0) { gReply("err D핀 범위는 0~" + String(XIAO_D_COUNT - 1)); return; }
    setWiringPin(which, g);
  }
  else if (c.startsWith("enpol:")) {
    String v = cmdArg(c, 6);
    gCfg.enActiveLow = (v == "low");
    gCfg.saveWiring(gPrefs);
    gPump.applyWiring();
    replyWiring();
  }
  else if (c.startsWith("pulse:")) {
    int us = cmdArg(c, 6).toInt();
    if (us < 1 || us > 5000) { gReply("err pulse 는 1~5000 us"); return; }
    gCfg.pulseUs = us;
    gCfg.saveWiring(gPrefs);
    replyWiring();
  }
  else if (c.startsWith("dir:")) {
    String v = cmdArg(c, 4);
    gPump.setDirection(!(v == "rev" || v == "1" || v == "back"));
    replyWiring();
  }
  else if (c.startsWith("hold:")) {
    bool on = (cmdArg(c, 5) == "on");
    if (gPump.hold(on)) gReply(String("ok hold ") + (on ? "on" : "off"));
    else gReply("warn EN 미배선 — 코일 여자를 끌 수 없습니다. VMOT 를 물리적으로 내리세요.");
  }
  else if (c.startsWith("pincheck:")) {
    int g = cmdArg(c, 9).toInt();
    int hi = -1, lo = -1;
    bool ok = PumpDrive::pinCheck(g, hi, lo);
    gReply("pincheck gpio=" + String(g) + " hi=" + String(hi) + " lo=" + String(lo)
           + (ok ? " 정상" : " 이상(단락/과부하 의심)"));
    gPump.applyWiring();   // 검사로 흐트러진 핀 상태 복구
  }

  // ---- 확장 : 브로커 주소 (재flash 없이) ----
  else if (c.startsWith("broker:")) {
    String v = cmdArg(c, 7);
    v.toCharArray(gCfg.brokerIp, sizeof(gCfg.brokerIp));
    gCfg.saveBroker(gPrefs);
    gReply("ok broker " + String(gCfg.brokerIp) + " (다음 재접속부터)");
  }
  else if (c.startsWith("brokerhost:")) {
    String v = cmdArg(c, 11);
    v.toCharArray(gCfg.brokerHost, sizeof(gCfg.brokerHost));
    gCfg.saveBroker(gPrefs);
    gReply("ok brokerhost " + String(gCfg.brokerHost) + " (다음 재접속부터)");
  }

  else if (c == "resetpos") { gPump.resetPosition(); gReply("ok pos=0"); }
  else if (c == "wiring")   { replyWiring(); }
  else if (c == "status")   { gReply(buildStatusLine()); }
  else if (c == "help") {
    gReply("cmd: start stop once manualstep:N spm:N spinsteps:N spinspm:N led:on|off");
    gReply("     hz:<hz>[:<sec>] sweep[:hz[:ms]] dir:fwd|rev hold:on|off resetpos");
    gReply("     pin:step|dir|en:<gpio> pinD:step|dir|en:<D> enpol:low|high pulse:<us>");
    gReply("     pincheck:<gpio> broker:<ip> brokerhost:<name> wiring status help");
  }
  else {
    gReply("err 알 수 없는 명령: " + c + "  (help 참고)");
  }
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
