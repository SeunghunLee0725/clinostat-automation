#pragma once
/*
 * sensor_ads1115.h — 저항센서(ADS1115)를 펌프 보드에서 직접 읽는다 (2026-10-22).
 *
 * 그 전에는 Nano 33 BLE 가 ADS1115 를 읽어 BLE 로 보냈다. 두 보드 모두 큐브에 올라가므로
 * ESP32-C6 하나로 합치고, 전송은 이미 붙어 있는 MQTT 를 쓴다.
 *
 *   배선:  ADS1115 SDA → D4(GPIO22) · SCL → D5(GPIO23) · VDD → 3V3 · GND → GND · ADDR → GND(0x48)
 *   측정:  Nano 펌웨어와 같은 설정 — AIN0−AIN1 차동, PGA ±2.048 V, 860 SPS 연속 변환(0x04E3).
 *          50 Hz 로 변환 레지스터만 읽는다(I²C 400 kHz 에서 약 0.1 ms).
 *   전송:  `s25007/board1/sensor` 로 **BLE notify 와 같은 21바이트 패킷**을 10개씩 묶어
 *          바이너리로 보낸다(200 ms 마다). 게이트웨이는 BLE 경로의 파서·저항 재계산을
 *          그대로 쓴다(gateway/ble_packet.py).
 *
 * ⚠ 센서는 펌프를 막으면 안 된다. Nano 펌웨어는 ADS1115 가 없으면 치명 오류로 멈췄지만
 *   여기서는 센서만 꺼지고 펌프는 계속 돈다. 5 s 마다 다시 찾는다(핫플러그).
 * ⚠ 펄스 엔진은 loop() 가 잠깐 늦어도 따라잡는다(pump_drive.h MAX_BURST). I²C 읽기 한 번은
 *   펄스 간격(600 Hz = 1.7 ms)보다 훨씬 짧다.
 * 복구: `sensor:off` 로 끄면 NVS 에 남아 재부팅 후에도 꺼진 채 시작한다(재flash 불필요).
 */

#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>

static const int      SENSOR_SDA_GPIO      = 22;      // D4
static const int      SENSOR_SCL_GPIO      = 23;      // D5
static const uint32_t SENSOR_I2C_HZ        = 400000;
static const uint8_t  SENSOR_ADS_ADDR      = 0x48;
static const uint16_t SENSOR_ADS_CONFIG    = 0x04E3;  // MUX 0-1 | PGA 2.048 | 연속 | 860 SPS | 비교기 끔
static const uint32_t SENSOR_PERIOD_MS     = 20;      // 50 Hz — Nano 펌웨어와 같다
static const uint32_t SENSOR_PROBE_MS      = 5000;
static const int      SENSOR_BATCH         = 10;      // 패킷 10개 = 200 ms 마다 발행
static const int      SENSOR_PACKET_BYTES  = 21;
static const char*    TOPIC_SENSOR         = "s25007/board1/sensor";

// 저항 환산 상수 — Nano 펌웨어 config.h(2026-09-04 실측)와 같다.
// 게이트웨이가 raw_adc 로 다시 계산하므로 여기 값은 게이트웨이 Rref 가 없을 때만 쓰인다.
static const float SENSOR_LSB_V     = 2.048f / 32768.0f;
static const float SENSOR_VEXC_V    = 3.2f;
static const float SENSOR_VREF_V    = 1.579f;
static const float SENSOR_RREF_OHM  = 81000.0f;
static const float SENSOR_TEMP_C    = 25.0f;              // TMP102 없음

// 발행 함수 — .ino 가 PumpNet 으로 연결한다. 연결이 없으면 false.
typedef bool (*SensorPublishFn)(const uint8_t*, size_t);

class SensorAds1115 {
public:
  void begin(Preferences* prefs, SensorPublishFn publish) {
    _prefs = prefs;
    _publish = publish;
    _enabled = prefs->getBool("sensorOn", true);
    if (!_enabled) { Serial.println("[SENSOR] 꺼짐(NVS) — `sensor:on` 으로 켠다"); return; }
    startBus();
    probe();
  }

  void tick() {
    if (!_enabled) return;
    const uint32_t now = millis();
    if (!_ready) {
      if (now - _lastProbeMs >= SENSOR_PROBE_MS) probe();
      return;
    }
    if (now - _lastSampleMs < SENSOR_PERIOD_MS) return;
    _lastSampleMs += SENSOR_PERIOD_MS;
    if (now - _lastSampleMs > SENSOR_PERIOD_MS * 10) _lastSampleMs = now;   // 오래 밀렸으면 당긴다

    int16_t raw;
    if (!readConversion(raw)) {
      _readErrors++;
      Serial.printf("[SENSOR] 읽기 실패(%lu회) — 다시 찾는다\n", (unsigned long)_readErrors);
      _ready = false;
      _lastProbeMs = now;
      return;
    }
    _lastRaw = raw;
    appendPacket(now, raw);
    if (_batchCount >= SENSOR_BATCH) flush();
  }

  void setEnabled(bool on) {
    _enabled = on;
    _prefs->putBool("sensorOn", on);
    _batchCount = 0;
    if (on) { startBus(); probe(); }
    else    { _ready = false; }
  }

  String statusLine() const {
    return String("sensor ") + (_enabled ? "on" : "off") +
           " ads=" + (_ready ? "ok" : "none") +
           " raw=" + String(_lastRaw) +
           " sent=" + String(_sentBatches) +
           " dropped=" + String(_droppedBatches) +
           " readerr=" + String(_readErrors) +
           " sda=" + String(SENSOR_SDA_GPIO) + " scl=" + String(SENSOR_SCL_GPIO);
  }

  // 근육 캡처(sensor_muscle.h)는 같은 I²C 버스의 다른 칩(0x49)을 쓴다. 저항센서가 꺼져 있어도
  // 버스는 열어 둬야 하므로 여기서 연다.
  void ensureBus() { startBus(); }

  static bool usesGpio(int gpio) { return gpio == SENSOR_SDA_GPIO || gpio == SENSOR_SCL_GPIO; }

private:
  Preferences*    _prefs = nullptr;
  SensorPublishFn _publish = nullptr;
  bool     _enabled = true, _ready = false, _busStarted = false, _missingLogged = false;
  uint32_t _lastProbeMs = 0, _lastSampleMs = 0;
  uint32_t _sentBatches = 0, _droppedBatches = 0, _readErrors = 0;
  int16_t  _lastRaw = 0;
  float    _baselineOhm = 0.0f;
  uint8_t  _batch[SENSOR_BATCH * SENSOR_PACKET_BYTES];
  int      _batchCount = 0;

  void startBus() {
    if (_busStarted) return;
    Wire.begin(SENSOR_SDA_GPIO, SENSOR_SCL_GPIO, SENSOR_I2C_HZ);
    Wire.setTimeOut(5);   // 버스가 걸려도 loop 를 오래 막지 않게(ms)
    _busStarted = true;
  }

  void probe() {
    _lastProbeMs = millis();
    Wire.beginTransmission(SENSOR_ADS_ADDR);
    if (Wire.endTransmission() != 0) {
      if (!_missingLogged) Serial.println("[SENSOR] ADS1115(0x48) 없음 — 펌프는 그대로, 5 s 마다 재시도");
      _missingLogged = true;
      _ready = false;
      return;
    }
    if (!writeConfig()) { _ready = false; return; }
    delay(3);                      // 첫 변환(1.2 ms) 대기 — 부팅·재발견 때만
    _ready = true;
    _missingLogged = false;
    _baselineOhm = 0.0f;
    _batchCount = 0;
    _lastSampleMs = millis();
    Serial.println("[SENSOR] ADS1115 연결 — 연속 변환 AIN0-AIN1, ±2.048 V, 860 SPS, 50 Hz 발행");
  }

  bool writeConfig() {
    Wire.beginTransmission(SENSOR_ADS_ADDR);
    Wire.write(0x01);
    Wire.write((uint8_t)(SENSOR_ADS_CONFIG >> 8));
    Wire.write((uint8_t)(SENSOR_ADS_CONFIG & 0xFF));
    return Wire.endTransmission() == 0;
  }

  bool readConversion(int16_t& out) {
    Wire.beginTransmission(SENSOR_ADS_ADDR);
    Wire.write(0x00);
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom((int)SENSOR_ADS_ADDR, 2) != 2) return false;
    const uint16_t msb = Wire.read(), lsb = Wire.read();
    out = (int16_t)((msb << 8) | lsb);
    return true;
  }

  // 시료가 Vexc 쪽: R = Rref·(Vexc − Vmid)/Vmid. 범위 밖(개방·포화)이면 0.
  static float toResistance(int16_t raw) {
    const float vmid = SENSOR_VREF_V + raw * SENSOR_LSB_V;
    if (vmid <= 0.0f || vmid >= SENSOR_VEXC_V) return 0.0f;
    const float r = SENSOR_RREF_OHM * (SENSOR_VEXC_V - vmid) / vmid;
    return r < 1.0f ? 1.0f : (r > 1.0e6f ? 1.0e6f : r);
  }

  // gateway/ble_packet.py 와 같은 리틀엔디안 배치 "<IifffB".
  void appendPacket(uint32_t ms, int16_t raw) {
    const float r = toResistance(raw);
    if (_baselineOhm <= 0.0f && r > 0.0f) _baselineOhm = r;
    const float delta = (_baselineOhm > 0.0f && r > 0.0f) ? (r - _baselineOhm) / _baselineOhm : 0.0f;
    const int32_t raw32 = raw;
    const float temp = SENSOR_TEMP_C;
    const uint8_t battery = 0;     // 외부 급전 — Nano VIN 급전 때와 같은 값
    uint8_t* p = _batch + _batchCount * SENSOR_PACKET_BYTES;
    memcpy(p + 0,  &ms,    4);
    memcpy(p + 4,  &raw32, 4);
    memcpy(p + 8,  &r,     4);
    memcpy(p + 12, &delta, 4);
    memcpy(p + 16, &temp,  4);
    p[20] = battery;
    _batchCount++;
  }

  // 브로커가 없으면 묶음을 버린다 — 쌓아 두면 재접속 때 몰려 나가 시각이 뒤섞인다.
  void flush() {
    if (_publish && _publish(_batch, (size_t)_batchCount * SENSOR_PACKET_BYTES)) _sentBatches++;
    else _droppedBatches++;
    _batchCount = 0;
  }
};
