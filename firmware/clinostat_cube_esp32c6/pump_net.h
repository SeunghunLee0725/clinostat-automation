#pragma once
/*
 * pump_net.h — WiFi + MQTT (비블로킹).
 *
 * 이전 구현의 문제: ensureMqtt() 가 `while (!mqtt.connected())` 로 **접속될 때까지 막았다.**
 * 2026-07-29 현장에서 방화벽이 1883 을 drop 하자 보드가 이 루프에 갇혀
 * rc=-2 를 무한 재시도하며 아무것도 못 했다 — 시리얼 진단도, 모터 구동도.
 * → 상태기계로 바꿔 접속을 못 해도 loop() 가 계속 돈다. 오프라인이어도
 *   USB 시리얼로 펌프를 구동·진단할 수 있다.
 *
 * 브로커 탐색: mDNS 우선, 실패하면 설정된 고정 IP.
 *   ⚠ fallback 에 서브넷 일치 검증을 걸지 않는다. 2026-07-25 에 보드와 브로커가
 *     같은 SSID 인데 다른 /24 를 받는 환경에서 그 검증 때문에 접속을 시도조차
 *     못 한 적이 있다. mDNS 결과에만 검증을 남긴다(도달 불가 주소를 거르기 위해).
 *
 * 2026-10-15 — 펌프별 명령 토픽을 모두 구독하고, 어느 토픽으로 왔는지(펌프 번호)를
 *   핸들러에 넘긴다. 상태도 펌프별 토픽으로 낸다. MQTT 접속은 하나다.
 */

#include <WiFi.h>
#include <WiFiClient.h>
#include <ESPmDNS.h>
#include <PubSubClient.h>
#include "pump_config.h"
#include "sensor_ads1115.h"
#include "sensor_muscle.h"

// (펌프 번호 0-base, 명령 문자열)
typedef void (*CommandHandler)(int, const String&);

class PumpNet {
public:
  void begin(PumpConfig* cfg, CommandHandler onCommand) {
    _cfg = cfg;
    _onCommand = onCommand;
    _mqtt.setClient(_net);
    _mqtt.setCallback(&PumpNet::trampoline);
    _mqtt.setBufferSize(512);   // 센서 묶음 210 B + 헤더 — 기본 256 B 는 빠듯하다
    _self = this;

    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    // 2026-10-15 실측: 절전 모드에서 보드가 SYN 은 보내는데 AP 가 되돌려주는 SYN-ACK 을
    // 못 받아 rc=-2 를 무한 반복했다(리셋하면 바로 붙음). 절전을 끄고, 아래 tick() 의
    // 연속 실패 복구(WiFi 재연결 → 재부팅)로 살아 있는 채 갇히는 일을 막는다.
    startWifi();
  }

  bool wifiUp()  const { return WiFi.status() == WL_CONNECTED; }
  bool mqttUp()        { return _mqtt.connected(); }
  IPAddress ip() const { return WiFi.localIP(); }
  int  rssi()    const { return WiFi.RSSI(); }
  const char* brokerDesc() const { return _brokerDesc; }

  bool publishStatus(int idx, const String& payload) {
    if (!_mqtt.connected()) return false;
    if (idx < 0 || idx >= PUMP_COUNT) return false;
    return _mqtt.publish(TOPIC_STATUS[idx], payload.c_str());
  }

  // 저항센서 묶음(바이너리). sensor_ads1115.h 가 200 ms 마다 부른다.
  bool publishSensor(const uint8_t* data, size_t len) {
    if (!_mqtt.connected()) return false;
    return _mqtt.publish(TOPIC_SENSOR, data, len);
  }

  // 근육 응답 캡처 조각(바이너리, ≤396 B). sensor_muscle.h 가 5 ms 간격으로 부른다.
  bool publishEmg(const uint8_t* data, size_t len) {
    if (!_mqtt.connected()) return false;
    return _mqtt.publish(TOPIC_EMG, data, len);
  }

  // 근육 연속 측정 조각 — 64샘플마다(≈115 ms).
  bool publishEmgStream(const uint8_t* data, size_t len) {
    if (!_mqtt.connected()) return false;
    return _mqtt.publish(TOPIC_EMG_STREAM, data, len);
  }

  // 명령 응답 — status 와 다른 토픽으로 보낸다(게이트웨이 파서와 섞이지 않게).
  bool publishReply(const String& payload) {
    if (!_mqtt.connected()) return false;
    return _mqtt.publish(TOPIC_REPLY, payload.c_str());
  }

  // 매 loop() 마다. 절대 막지 않는다.
  //
  // pumpBusy = 모터가(하나라도) 구동 중. 이때는 **접속 시도를 건너뛴다.**
  //   MDNS.queryHost() 와 mqtt.connect() 는 수백 ms~수초를 블로킹한다. 구동 중에
  //   이게 끼어들면 STEP 펄스 간격이 무너져 실제 속도가 명령의 절반까지 떨어진다
  //   (2026-07-29 실측: 40Hz 명령이 22Hz 로 나왔다). 재접속은 몇 초 늦어도 되지만
  //   펄스 타이밍은 늦으면 안 된다. 이미 연결돼 있으면 mqtt.loop() 만 도는데,
  //   이건 논블로킹이라 구동 중에도 안전하다.
  void tick(bool pumpBusy) {
    const unsigned long now = millis();

    if (!wifiUp()) {
      if (now - _lastWifiTryMs >= 8000) { _lastWifiTryMs = now; startWifi(); }
      return;                       // WiFi 없으면 MQTT 는 의미 없음
    }
    if (!_wifiAnnounced) {
      _wifiAnnounced = true;
      Serial.printf("WiFi OK  SSID=%s  IP=%s  mask=%s  rssi=%d\n",
                    WIFI_SSID, WiFi.localIP().toString().c_str(),
                    WiFi.subnetMask().toString().c_str(), WiFi.RSSI());
      MDNS.begin("s25007board");
      WiFi.setSleep(false);   // 연결 전에 부르면 STA 가 뜨지 않았다(2026-10-15 실측)
    }

    if (_mqtt.connected()) { _mqtt.loop(); return; }

    if (pumpBusy) return;                      // 구동 중엔 블로킹 재접속을 하지 않는다
    if (now - _lastMqttTryMs < 3000) return;   // 재시도 간격
    _lastMqttTryMs = now;

    if (!resolveBroker()) return;
    Serial.print("MQTT 접속...");
    if (_mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS)) {
      Serial.println("성공");
      _mqttFails = 0; _wifiRestarts = 0;
      for (int i = 0; i < PUMP_COUNT; i++) _mqtt.subscribe(TOPIC_CMD[i]);
    } else {
      _mqttFails++;
      Serial.printf("실패 rc=%d (%d회째, 3s 후 재시도) — 오프라인이어도 시리얼로 제어 가능\n",
                    _mqtt.state(), _mqttFails);
      recoverAfterRepeatedFailure();
    }
  }

private:
  // 연속 실패 복구: MQTT_FAILS_PER_WIFI_RESTART 번 실패마다 WiFi 를 끊고 다시 붙는다
  // (lwIP/무선 상태 초기화). 그걸 WIFI_RESTARTS_BEFORE_REBOOT 번 해도 안 되면 재부팅.
  // 호출 시점은 pumpBusy 가 아닐 때뿐이라 구동 중에 재부팅하지 않는다.
  static const int MQTT_FAILS_PER_WIFI_RESTART = 8;    // ≈ 30 s
  static const int WIFI_RESTARTS_BEFORE_REBOOT = 3;    // ≈ 2 min

  void recoverAfterRepeatedFailure() {
    if (_mqttFails < MQTT_FAILS_PER_WIFI_RESTART) return;
    _mqttFails = 0;
    _wifiRestarts++;
    if (_wifiRestarts >= WIFI_RESTARTS_BEFORE_REBOOT) {
      Serial.println("MQTT 복구 실패 — 재부팅");
      Serial.flush();
      ESP.restart();
    }
    Serial.printf("MQTT 연속 실패 — WiFi 재연결 (%d/%d)\n", _wifiRestarts, WIFI_RESTARTS_BEFORE_REBOOT);
    _mqtt.disconnect();
    WiFi.disconnect();
    _lastWifiTryMs = millis();
    startWifi();
  }

  PumpConfig*    _cfg = nullptr;
  CommandHandler _onCommand = nullptr;
  WiFiClient     _net;
  PubSubClient   _mqtt;
  char           _brokerDesc[48] = "(미정)";
  unsigned long  _lastWifiTryMs = 0, _lastMqttTryMs = 0;
  bool           _wifiAnnounced = false;
  int            _mqttFails = 0, _wifiRestarts = 0;

  static PumpNet* _self;

  // 토픽 → 펌프 번호. 모르는 토픽은 -1 (구독한 적 없으므로 오지 않지만 방어).
  static int topicIndex(const char* topic) {
    for (int i = 0; i < PUMP_COUNT; i++) if (strcmp(topic, TOPIC_CMD[i]) == 0) return i;
    return -1;
  }

  static void trampoline(char* topic, byte* payload, unsigned int len) {
    if (!_self || !_self->_onCommand) return;
    int idx = topicIndex(topic);
    if (idx < 0) return;
    String msg;
    msg.reserve(len);
    for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
    _self->_onCommand(idx, msg);
  }

  void startWifi() {
    _wifiAnnounced = false;
    Serial.printf("WiFi 접속 시도: %s\n", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }

  bool sameSubnet(const IPAddress& addr) const {
    uint32_t mask = (uint32_t)WiFi.subnetMask();
    return ((uint32_t)addr & mask) == ((uint32_t)WiFi.localIP() & mask);
  }

  bool resolveBroker() {
    if (strlen(_cfg->brokerHost) > 0) {
      // 타임아웃을 짧게 — 이 호출은 블로킹이라 길게 잡으면 loop 가 그만큼 멈춘다.
      IPAddress found = MDNS.queryHost(_cfg->brokerHost, 600);
      if (found != IPAddress((uint32_t)0) && sameSubnet(found)) {
        _mqtt.setServer(found, _cfg->brokerPort);
        snprintf(_brokerDesc, sizeof(_brokerDesc), "%s:%d (mDNS)", found.toString().c_str(), _cfg->brokerPort);
        Serial.printf("브로커 = %s\n", _brokerDesc);
        return true;
      }
    }
    IPAddress fixed;
    if (strlen(_cfg->brokerIp) > 0 && fixed.fromString(_cfg->brokerIp)) {
      _mqtt.setServer(fixed, _cfg->brokerPort);
      snprintf(_brokerDesc, sizeof(_brokerDesc), "%s:%d (고정)", fixed.toString().c_str(), _cfg->brokerPort);
      Serial.printf("브로커 = %s\n", _brokerDesc);
      return true;
    }
    Serial.println("브로커 주소 미확인 — mDNS 실패이고 고정 IP도 없음. `broker:<ip>` 로 지정하세요.");
    return false;
  }
};

PumpNet* PumpNet::_self = nullptr;
