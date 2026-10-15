#pragma once
/*
 * pump_config.h — 영속 설정 (NVS).
 *
 * 이 파일의 존재 이유: 지난해 원본 펌웨어 소스가 없어 S25007_Manual_RevB.pdf 로부터
 * 프로토콜만 재현했기 때문에 **STEP/DIR/EN 이 실제로 어느 핀에 물려 있는지 문서가 없다.**
 * 핀을 컴파일 타임 상수로 두면 배선을 한 번 바꿀 때마다 재flash 해야 한다.
 * → 핀·극성·브로커 주소를 전부 NVS 에 두고 런타임에 바꿀 수 있게 한다.
 *
 * 2026-10-15 — 펌프 2대 확장.
 *   보드 하나(XIAO ESP32-C6)가 DRV8825 두 개를 몬다. 모터별 설정(핀·spm·spin)은
 *   MotorConfig 로 묶고, 보드 공통 설정(EN 극성·펄스폭·브로커)은 그대로 둔다.
 *   펌프 1 의 NVS 키와 MQTT 토픽은 **바꾸지 않는다** — 이미 배포된 보드의 설정과
 *   게이트웨이·Flutter 앱이 그대로 동작해야 한다. 펌프 2 는 키 접미사 "2",
 *   토픽은 `s25007/board1/p2/...` 를 쓴다.
 */

#include <Arduino.h>
#include <Preferences.h>
#include <IPAddress.h>

// 명령 처리 결과를 사람이 읽는 한 줄로 되돌려 주는 콜백.
// (.ino 에서 시리얼 + MQTT 양쪽으로 내보내도록 구현한다)
typedef void (*ReplyFn)(const String&);

// ---- Seeed XIAO ESP32-C6 : D핀 ↔ GPIO 실측 매핑 ----
// 2026-07-29 sweep 로그로 확인한 값. 데이터시트 별칭과 일치한다.
//   D0=0  D1=1  D2=2  D3=21  D4=22  D5=23  D6=16  D7=17  D8=19  D9=20  D10=18
static const int XIAO_D_GPIO[] = {0, 1, 2, 21, 22, 23, 16, 17, 19, 20, 18};
static const int XIAO_D_COUNT  = sizeof(XIAO_D_GPIO) / sizeof(XIAO_D_GPIO[0]);

// D핀 번호를 GPIO 로. 범위를 벗어나면 -1.
inline int dPinToGpio(int d) {
  return (d >= 0 && d < XIAO_D_COUNT) ? XIAO_D_GPIO[d] : -1;
}

// GPIO 가 XIAO 헤더에 나와 있는 핀인지.
inline bool isHeaderGpio(int gpio) {
  for (int i = 0; i < XIAO_D_COUNT; i++) if (XIAO_D_GPIO[i] == gpio) return true;
  return false;
}

// ---- 매뉴얼이 규정한 범위 (RevB §4) ----
static const long SPM_MIN = 1, SPM_MAX = 2400;

// ---- 이 보드가 모는 펌프(모터) 수 ----
static const int PUMP_COUNT = 2;

// ---- 고정 설정 ----
// WiFi·MQTT 자격증명은 저장소에 두지 않는다. pump_secrets.h.example 을
// pump_secrets.h 로 복사해 이 기계의 값을 채워라 (.gitignore 로 빠져 있다).
// 파일이 없으면 여기서 컴파일이 멈춘다 — 그게 의도다.
#include "pump_secrets.h"

static const int   MQTT_PORT    = 1883;
// 펌프 1 은 매뉴얼 토픽 그대로(앱/게이트웨이 호환). 펌프 2 는 그 아래 p2/ 를 붙인다.
static const char* TOPIC_CMD[PUMP_COUNT]    = {"s25007/board1/cmd",    "s25007/board1/p2/cmd"};
static const char* TOPIC_STATUS[PUMP_COUNT] = {"s25007/board1/status", "s25007/board1/p2/status"};
// 명령 응답은 status 와 **분리**한다. 게이트웨이의 parse_board_status() 는 run/pos 필드를
// 요구하므로 "ok start" 같은 응답이 status 토픽에 섞이면 파싱 실패 로그만 늘어난다.
// 응답 토픽은 두 펌프가 공유하고, 펌프 2 의 응답은 "p2 " 접두어로 구분한다.
static const char* TOPIC_REPLY  = "s25007/board1/reply";
static const char* MQTT_CLIENT_ID = "s25007-board1";

// 모터 하나의 설정 — 배선과 구동값. 전부 런타임 변경 가능.
struct MotorConfig {
  int  stepGpio;
  int  dirGpio;
  int  enGpio;       // -1 = 미연결. StepStick 은 EN 부동 시 내부 풀다운으로 상시 활성.
  long spm;          // 매뉴얼 §4 "설정값 변경"
  long spinsteps;
  long spinspm;

  bool enConnected() const { return enGpio >= 0; }
  bool usesGpio(int gpio) const {
    return gpio >= 0 && (gpio == stepGpio || gpio == dirGpio || gpio == enGpio);
  }
};

struct PumpConfig {
  // 모터별 설정. 펌프 1 은 2026-07-23 육안 확인값(D9/D10). 펌프 2 의 기본값 D7/D8 은
  // 아직 미검증이므로 실제 배선에 맞춰 `p2:pin:` 으로 바꿔야 한다.
  MotorConfig motor[PUMP_COUNT] = {
    {20, 18, -1, 1200, 0, 2400},   // 펌프 1: STEP=D9(GPIO20)  DIR=D10(GPIO18)
    {17, 19, -1, 1200, 0, 2400},   // 펌프 2: STEP=D7(GPIO17)  DIR=D8(GPIO19)
  };

  // 보드 공통
  bool enActiveLow = true;   // A4988/DRV8825 ENABLE 은 active-low
  int  pulseUs    = 5;       // STEP HIGH 폭. A4988 최소 1us / DRV8825 1.9us → 5us 는 충분한 여유.
                             // 드라이버가 펄스를 놓치는 의심이 들면 20~50 으로 올려 시험한다.

  // 브로커 — mDNS 실패 시 쓸 고정 IP. 빈 문자열이면 mDNS 만 시도.
  //   ⚠ 2026-07-25 교훈: fallback 에 sameSubnet() 검증을 걸었더니 보드와 브로커가
  //     같은 SSID 인데 다른 /24 를 받는 환경에서 **시도조차 하지 않았다.** 검증하지 않는다.
  char brokerIp[16]   = "";
  char brokerHost[40] = "clinostat-pi";   // mDNS 호스트명 (".local" 제외)
  // 2026-10-15: 현장 상위 방화벽이 1883 을 drop 한다(22·1884 등은 통과). 파이에서
  // 1884→1883 중계를 띄우고 보드는 이 포트로 붙는다. `broker:<ip>:<port>` 로 바꾼다.
  int  brokerPort     = MQTT_PORT;

  // NVS 키. 펌프 1 은 기존 키 그대로(배포된 보드의 설정 유지), 펌프 2 는 접미사 "2".
  // Preferences 키는 15자 제한 — "spinsteps2" 가 가장 길다(10자).
  static String key(const char* base, int idx) {
    return idx == 0 ? String(base) : String(base) + String(idx + 1);
  }

  void load(Preferences& p) {
    for (int i = 0; i < PUMP_COUNT; i++) {
      MotorConfig& m = motor[i];
      m.stepGpio  = p.getInt (key("stepGpio",  i).c_str(), m.stepGpio);
      m.dirGpio   = p.getInt (key("dirGpio",   i).c_str(), m.dirGpio);
      m.enGpio    = p.getInt (key("enGpio",    i).c_str(), m.enGpio);
      m.spm       = p.getLong(key("spm",       i).c_str(), m.spm);
      m.spinsteps = p.getLong(key("spinsteps", i).c_str(), m.spinsteps);
      m.spinspm   = p.getLong(key("spinspm",   i).c_str(), m.spinspm);
    }
    enActiveLow = p.getBool("enActLow",  enActiveLow);
    pulseUs     = p.getInt ("pulseUs",   pulseUs);
    p.getString("brokerIp",   brokerIp,   sizeof(brokerIp));
    p.getString("brokerHost", brokerHost, sizeof(brokerHost));
    brokerPort = p.getInt("brokerPort", brokerPort);
  }

  void saveDrive(Preferences& p, int idx) const {
    const MotorConfig& m = motor[idx];
    p.putLong(key("spm",       idx).c_str(), m.spm);
    p.putLong(key("spinsteps", idx).c_str(), m.spinsteps);
    p.putLong(key("spinspm",   idx).c_str(), m.spinspm);
  }

  void saveWiring(Preferences& p, int idx) const {
    const MotorConfig& m = motor[idx];
    p.putInt (key("stepGpio", idx).c_str(), m.stepGpio);
    p.putInt (key("dirGpio",  idx).c_str(), m.dirGpio);
    p.putInt (key("enGpio",   idx).c_str(), m.enGpio);
    p.putBool("enActLow", enActiveLow);
    p.putInt ("pulseUs",  pulseUs);
  }

  void saveBroker(Preferences& p) const {
    p.putString("brokerIp",   brokerIp);
    p.putString("brokerHost", brokerHost);
    p.putInt   ("brokerPort", brokerPort);
  }

  long clampSpm(long v) const { return v < SPM_MIN ? SPM_MIN : (v > SPM_MAX ? SPM_MAX : v); }

  // 스윕에서 건너뛸 핀: 어느 모터든 EN 으로 쓰는 핀, 그리고 **다른** 모터의 STEP/DIR.
  // 다른 모터 핀을 두들기면 그 모터가 돌아 "어느 핀이 내 STEP 인지" 를 읽을 수 없다.
  bool sweepReserved(int gpio, int forIdx) const {
    for (int i = 0; i < PUMP_COUNT; i++) {
      if (motor[i].enConnected() && motor[i].enGpio == gpio) return true;
      if (i != forIdx && motor[i].usesGpio(gpio)) return true;
    }
    return false;
  }
};
