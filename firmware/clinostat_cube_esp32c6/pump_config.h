#pragma once
/*
 * pump_config.h — 영속 설정 (NVS).
 *
 * 이 파일의 존재 이유: 지난해 원본 펌웨어 소스가 없어 S25007_Manual_RevB.pdf 로부터
 * 프로토콜만 재현했기 때문에 **STEP/DIR/EN 이 실제로 어느 핀에 물려 있는지 문서가 없다.**
 * 핀을 컴파일 타임 상수로 두면 배선을 한 번 바꿀 때마다 재flash 해야 한다.
 * → 핀·극성·브로커 주소를 전부 NVS 에 두고 런타임에 바꿀 수 있게 한다.
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

// ---- 고정 설정 ----
// WiFi·MQTT 자격증명은 저장소에 두지 않는다. pump_secrets.h.example 을
// pump_secrets.h 로 복사해 이 기계의 값을 채워라 (.gitignore 로 빠져 있다).
// 파일이 없으면 여기서 컴파일이 멈춘다 — 그게 의도다.
#include "pump_secrets.h"

static const int   MQTT_PORT    = 1883;
static const char* TOPIC_CMD    = "s25007/board1/cmd";      // 매뉴얼과 동일 — 앱/게이트웨이 호환
static const char* TOPIC_STATUS = "s25007/board1/status";
// 명령 응답은 status 와 **분리**한다. 게이트웨이의 parse_board_status() 는 run/pos 필드를
// 요구하므로 "ok start" 같은 응답이 status 토픽에 섞이면 파싱 실패 로그만 늘어난다.
static const char* TOPIC_REPLY  = "s25007/board1/reply";
static const char* MQTT_CLIENT_ID = "s25007-board1";

struct PumpConfig {
  // 배선 — 전부 런타임 변경 가능
  int  stepGpio   = 20;      // D9  (2026-07-23 육안 확인값. 미검증이므로 언제든 바꿀 수 있어야 한다)
  int  dirGpio    = 18;      // D10
  int  enGpio     = -1;      // 미연결. StepStick 은 EN 부동 시 내부 풀다운으로 상시 활성.
  bool enActiveLow = true;   // A4988/DRV8825 ENABLE 은 active-low
  int  pulseUs    = 5;       // STEP HIGH 폭. A4988 최소 1us / DRV8825 1.9us → 5us 는 충분한 여유.
                             // 드라이버가 펄스를 놓치는 의심이 들면 20~50 으로 올려 시험한다.

  // 구동 설정 (매뉴얼 §4 "설정값 변경")
  long spm        = 1200;
  long spinsteps  = 0;
  long spinspm    = 2400;

  // 브로커 — mDNS 실패 시 쓸 고정 IP. 빈 문자열이면 mDNS 만 시도.
  //   ⚠ 2026-07-25 교훈: fallback 에 sameSubnet() 검증을 걸었더니 보드와 브로커가
  //     같은 SSID 인데 다른 /24 를 받는 환경에서 **시도조차 하지 않았다.** 검증하지 않는다.
  char brokerIp[16]   = "";
  char brokerHost[40] = "clinostat-pi";   // mDNS 호스트명 (".local" 제외)

  void load(Preferences& p) {
    stepGpio    = p.getInt ("stepGpio",  stepGpio);
    dirGpio     = p.getInt ("dirGpio",   dirGpio);
    enGpio      = p.getInt ("enGpio",    enGpio);
    enActiveLow = p.getBool("enActLow",  enActiveLow);
    pulseUs     = p.getInt ("pulseUs",   pulseUs);
    spm         = p.getLong("spm",       spm);
    spinsteps   = p.getLong("spinsteps", spinsteps);
    spinspm     = p.getLong("spinspm",   spinspm);
    p.getString("brokerIp",   brokerIp,   sizeof(brokerIp));
    p.getString("brokerHost", brokerHost, sizeof(brokerHost));
  }

  void saveDrive(Preferences& p) const {
    p.putLong("spm", spm);
    p.putLong("spinsteps", spinsteps);
    p.putLong("spinspm", spinspm);
  }

  void saveWiring(Preferences& p) const {
    p.putInt ("stepGpio", stepGpio);
    p.putInt ("dirGpio",  dirGpio);
    p.putInt ("enGpio",   enGpio);
    p.putBool("enActLow", enActiveLow);
    p.putInt ("pulseUs",  pulseUs);
  }

  void saveBroker(Preferences& p) const {
    p.putString("brokerIp",   brokerIp);
    p.putString("brokerHost", brokerHost);
  }

  bool enConnected() const { return enGpio >= 0; }

  long clampSpm(long v) const { return v < SPM_MIN ? SPM_MIN : (v > SPM_MAX ? SPM_MAX : v); }
};
