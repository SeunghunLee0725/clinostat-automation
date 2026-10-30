#pragma once
/*
 * sensor_muscle.h — 근육 전기자극 응답 전압 캡처 (2026-10-30).
 *
 * **근육 전용 ADS1115(0x49)** 를 쓴다(2026-10-30 저녁 분리). 처음엔 저항센서 칩(0x48)의
 * AIN2·AIN3 를 빌려 저항센서를 멈춰야 했고, 근육 회로의 60 Hz·접지 흔들림이 같은 칩의
 * 저항센서로 번졌다(10-30 실측). 칩을 나눠 두 측정을 동시에 한다.
 *
 *   배선:  새 칩 VDD → 3V3 · GND → GND · SDA/SCL → D4/D5(저항센서 칩과 같은 버스) · ADDR → VDD(0x49)
 *          opamp 출력 → AIN0 · opamp 기준(Vref) → AIN1. AIN0−AIN1 차동이라 양방향 신호도
 *          Vref 기준으로 읽힌다. 두 핀 모두 0 V~VDD(3.3 V) 밖으로 나가면 안 된다.
 *   측정:  `emg:cap:<ms>` 동안 **단발 변환**을 연달아 건다(860 SPS 설정,
 *          실효 ≈700 S/s). 연속 변환을 시간으로 읽으면 ADS 내부 클럭 오차(±10 %) 때문에
 *          같은 값을 두 번 읽거나 건너뛴다 — 단발은 OS 비트로 완료를 확인하므로 샘플마다
 *          새 변환이고 시각도 실제 시작 시각이다.
 *   전송:  끝나면 `s25007/board1/emg` 로 조각(헤더 12 B + 샘플 6 B × 최대 64) 발행 —
 *          loop 한 번에 한 조각씩(펌프 펄스를 막지 않게). 형식은 gateway/emg_packet.py.
 *          요약(min/max/pp mV, 포화 수)은 명령 응답으로 바로 나온다.
 *
 * 연속 측정(2026-10-30): `emg:stream:on` 이면 끝없이 읽어 64샘플마다
 *   `s25007/board1/emg/stream` 으로 보낸다(조각 수 = 0, 조각 번호 = 일련번호). 20 s 동안
 *   `emg:stream:on`(keepalive)이 다시 오지 않으면 스스로 멈춘다 — 화면이 닫히면 멈추게.
 *   `emg:stream:off` 로 바로 멈춘다. 저항센서는 그동안에도 50 Hz 로 계속 돈다.
 *
 * ⚠ 860 SPS 가 상한이다. ms 단위 M파 모양까지 보려면 빠른 외장 ADC 가 필요하다.
 * ⚠ 0x49 칩이 없으면 캡처·연속 측정만 거절한다 — 저항센서·펌프는 그대로.
 */

#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>
#include "pump_config.h"
#include "sensor_ads1115.h"

static const char*    TOPIC_EMG             = "s25007/board1/emg";
static const char*    TOPIC_EMG_STREAM      = "s25007/board1/emg/stream";
static const uint32_t EMG_STREAM_KEEPALIVE_MS = 20000;
static const uint8_t  EMG_ADS_ADDR          = 0x49;    // ADDR → VDD. 저항센서 칩은 0x48(ADDR → GND)
static const uint16_t EMG_CFG_BASE          = 0x8000 | 0x0000 | 0x0100 | 0x00E0 | 0x0003;  // 단발 시작 | AIN0-AIN1 | 단발 | 860 SPS | 비교기 끔
static const char*    EMG_NO_CHIP = "err emg 칩(0x49) 없음 — 새 ADS1115 의 ADDR→VDD·SDA/SCL·3V3 배선 확인";
static const uint32_t EMG_MAX_MS            = 2000;
static const int      EMG_MAX_SAMPLES       = 1800;    // 2 s × 860 + 여유
static const uint32_t EMG_SETTLE_US         = 1100;    // 860 SPS 한 변환 ≈ 1.16 ms — 그 전엔 묻지 않는다
static const uint32_t EMG_CONV_TIMEOUT_US   = 5000;
static const int      EMG_CHUNK_SAMPLES     = 64;      // 12 + 64×6 = 396 B < MQTT 버퍼 512 B
static const uint32_t EMG_CHUNK_GAP_MS      = 5;

// PGA 풀스케일(mV) → 설정 비트(11:9). ±6.144 V 는 VDD 3.3 V 에서 의미가 없어 뺐다.
static const uint16_t EMG_PGA_MV[]   = {4096, 2048, 1024, 512, 256};
static const uint16_t EMG_PGA_BITS[] = {0x0200, 0x0400, 0x0600, 0x0800, 0x0A00};

typedef bool (*EmgPublishFn)(const uint8_t*, size_t);

class MuscleCapture {
public:
  void begin(Preferences* prefs, SensorAds1115* sensor, EmgPublishFn publish,
             EmgPublishFn publishStream, ReplyFn reply) {
    _prefs = prefs; _sensor = sensor; _publish = publish; _publishStream = publishStream; _reply = reply;
    if (pgaIndex(prefs->getUShort("emgPga", 2048)) >= 0) _pgaMv = prefs->getUShort("emgPga", 2048);
    _sensor->ensureBus();
    Serial.println(chipPresent() ? "[EMG] ADS1115(0x49) 연결 — AIN0-AIN1"
                                 : "[EMG] ADS1115(0x49) 없음 — 근육 캡처만 꺼짐");
  }

  bool chipPresent() const {
    Wire.beginTransmission(EMG_ADS_ADDR);
    return Wire.endTransmission() == 0;
  }

  bool busy() const { return _capturing || _publishing || _streaming; }
  bool streaming() const { return _streaming; }

  bool setPga(int mv) {
    if (pgaIndex(mv) < 0) return false;
    _pgaMv = (uint16_t)mv;
    _prefs->putUShort("emgPga", _pgaMv);
    return true;
  }

  // ms 동안 캡처. 이미 진행 중이거나 범위 밖이면 이유를 돌려준다(빈 문자열 = 시작).
  String start(long ms) {
    if (busy()) return "err emg 캡처·전송 중";
    if (ms < 10 || ms > (long)EMG_MAX_MS) return "err emg:cap 은 10~" + String(EMG_MAX_MS) + " ms";
    if (!chipPresent()) return EMG_NO_CHIP;
    _captureId++;
    _durationUs = (uint32_t)ms * 1000UL;
    _n = 0; _converting = false; _capturing = true; _clockStarted = false;
    return "";
  }

  // 켜기와 keepalive 가 같은 명령이다. 이미 켜져 있으면 시계만 늘린다.
  String streamOn() {
    if (_streaming) { _keepaliveMs = millis(); return "ok emg stream 유지"; }
    if (_capturing || _publishing) return "err emg 캡처·전송 중";
    if (!chipPresent()) return EMG_NO_CHIP;
    _streamId++;
    _seq = 0; _n = 0; _converting = false; _clockStarted = false;
    _streaming = true;
    _keepaliveMs = millis();
    return "ok emg stream 시작 id=" + String(_streamId);
  }

  String streamOff(const char* why) {
    if (!_streaming) return "ok emg stream 꺼져 있음";
    _streaming = false;
    _converting = false;
    return String("ok emg stream 종료 (") + why + ", 조각 " + String(_seq) + ")";
  }

  void tick() {
    if (_capturing) tickCapture();
    else if (_publishing) tickPublish();
    else if (_streaming) tickStream();
  }

  String statusLine() const {
    return String("emg ") + (_capturing ? "capturing" : _publishing ? "sending" : _streaming ? "streaming" : "idle") +
           " pga=" + String(_pgaMv) + "mV in=0x49 AIN0-AIN1 chip=" + (chipPresent() ? "ok" : "none") + " last_id=" + String(_captureId) +
           " n=" + String(_n) + " sent=" + String(_sentChunks) + " dropped=" + String(_droppedChunks);
  }

private:
  Preferences*   _prefs = nullptr;
  SensorAds1115* _sensor = nullptr;
  EmgPublishFn   _publish = nullptr;
  EmgPublishFn   _publishStream = nullptr;
  ReplyFn        _reply = nullptr;
  uint16_t _pgaMv = 2048;
  uint32_t _captureId = 0, _startUs = 0, _durationUs = 0, _convStartUs = 0, _lastChunkMs = 0;
  uint32_t _sentChunks = 0, _droppedChunks = 0;
  uint32_t _streamId = 0, _keepaliveMs = 0;
  uint16_t _seq = 0;
  bool     _streaming = false;
  const char* _err = "";
  bool     _capturing = false, _converting = false, _publishing = false, _clockStarted = false;
  int      _n = 0, _chunkIdx = 0, _chunkCount = 0;
  uint32_t _t[EMG_MAX_SAMPLES];
  int16_t  _raw[EMG_MAX_SAMPLES];

  static int pgaIndex(int mv) {
    for (int i = 0; i < (int)(sizeof(EMG_PGA_MV) / sizeof(EMG_PGA_MV[0])); i++) if (EMG_PGA_MV[i] == mv) return i;
    return -1;
  }

  // 단발 변환 한 걸음. 1 = 샘플 하나를 _t/_raw[_n] 에 넣음, 0 = 아직, -1 = 오류(_err).
  // 부르는 쪽이 멈출지(시간·개수)는 !_converting 일 때 먼저 판단한다.
  int stepConversion(uint32_t now) {
    // 시계는 첫 변환에서 시작한다. 시작 응답을 USB 시리얼로 찍는 동안(호스트가 포트를 안 열면
    // 수백 ms 막힌다 — 2026-10-30 실측 n=0) 캡처 시간이 흘러가 버리지 않게.
    if (!_clockStarted) { _startUs = now; _clockStarted = true; }
    if (!_converting) {
      if (!writeReg(0x01, EMG_CFG_BASE | EMG_PGA_BITS[pgaIndex(_pgaMv)])) { _err = "I²C 쓰기 실패"; return -1; }
      _convStartUs = now;
      _converting = true;
      return 0;
    }
    if (now - _convStartUs < EMG_SETTLE_US) return 0;
    uint16_t cfg;
    if (!readReg(0x01, cfg)) { _err = "I²C 읽기 실패"; return -1; }
    if (!(cfg & 0x8000)) {
      if (now - _convStartUs > EMG_CONV_TIMEOUT_US) { _err = "변환 완료 신호 없음"; return -1; }
      return 0;
    }
    uint16_t v;
    if (!readReg(0x00, v)) { _err = "I²C 읽기 실패"; return -1; }
    _converting = false;
    _t[_n] = _convStartUs - _startUs;
    _raw[_n] = (int16_t)v;
    _n++;
    return 1;
  }

  void tickCapture() {
    const uint32_t now = micros();
    if (!_converting && _clockStarted && (now - _startUs >= _durationUs || _n >= EMG_MAX_SAMPLES)) {
      finishCapture("");
      return;
    }
    if (stepConversion(now) < 0) finishCapture(_err);
  }

  // 64샘플이 모이면 바로 한 조각 보낸다(≈115 ms 마다). 버퍼 앞부분만 다시 쓴다.
  void tickStream() {
    if (millis() - _keepaliveMs > EMG_STREAM_KEEPALIVE_MS) {
      _reply(streamOff("keepalive 없음 — 자동 종료"));
      return;
    }
    const int r = stepConversion(micros());
    if (r < 0) { _reply(String("err emg stream 중단: ") + _err); streamOff(_err); return; }
    if (_n < EMG_CHUNK_SAMPLES) return;
    uint8_t buf[12 + EMG_CHUNK_SAMPLES * 6];
    const size_t len = buildChunk(buf, _streamId, _seq, 0, 0, (uint16_t)_n);
    if (_publishStream && _publishStream(buf, len)) _sentChunks++;
    else _droppedChunks++;
    _seq++;
    _n = 0;
  }

  void finishCapture(const char* error) {
    _capturing = false;
    _converting = false;
    if (!error[0] && _n == 0) error = "샘플 없음";
    if (error[0]) { _reply(String("err emg 캡처 중단: ") + error + " (n=" + String(_n) + ")"); return; }
    _reply(summaryLine());
    _chunkCount = (_n + EMG_CHUNK_SAMPLES - 1) / EMG_CHUNK_SAMPLES;
    _chunkIdx = 0;
    _publishing = _chunkCount > 0;
    _lastChunkMs = millis() - EMG_CHUNK_GAP_MS;
  }

  String summaryLine() const {
    int16_t lo = 32767, hi = -32768;
    int sat = 0;
    for (int i = 0; i < _n; i++) {
      if (_raw[i] < lo) lo = _raw[i];
      if (_raw[i] > hi) hi = _raw[i];
      if (_raw[i] == 32767 || _raw[i] == -32768) sat++;
    }
    const float k = _pgaMv / 32768.0f;
    const float sps = _n > 1 ? (_n - 1) * 1.0e6f / (float)(_t[_n - 1] - _t[0]) : 0.0f;
    String s = "emg id=" + String(_captureId) + " n=" + String(_n) + " sps=" + String(sps, 0) +
               " pga=" + String(_pgaMv) + "mV min=" + String(lo * k, 2) + "mV max=" + String(hi * k, 2) +
               "mV pp=" + String((hi - lo) * k, 2) + "mV sat=" + String(sat);
    if (sat > 0) s += " ⚠ 포화 — emg:pga 를 키운다";
    return s;
  }

  void tickPublish() {
    if (millis() - _lastChunkMs < EMG_CHUNK_GAP_MS) return;
    _lastChunkMs = millis();
    const int first = _chunkIdx * EMG_CHUNK_SAMPLES;
    const uint16_t n = (uint16_t)min(EMG_CHUNK_SAMPLES, _n - first);
    uint8_t buf[12 + EMG_CHUNK_SAMPLES * 6];
    const size_t len = buildChunk(buf, _captureId, (uint16_t)_chunkIdx, (uint16_t)_chunkCount, first, n);
    if (_publish && _publish(buf, len)) _sentChunks++;
    else _droppedChunks++;
    if (++_chunkIdx >= _chunkCount) _publishing = false;
  }

  // gateway/emg_packet.py 와 같은 리틀엔디안 "<IHHHH" + n × "<Ih".
  size_t buildChunk(uint8_t* buf, uint32_t id, uint16_t idx, uint16_t count, int first, uint16_t n) const {
    memcpy(buf + 0,  &id,     4);
    memcpy(buf + 4,  &idx,    2);
    memcpy(buf + 6,  &count,  2);
    memcpy(buf + 8,  &n,      2);
    memcpy(buf + 10, &_pgaMv, 2);
    for (int i = 0; i < n; i++) {
      memcpy(buf + 12 + i * 6,     &_t[first + i],   4);
      memcpy(buf + 12 + i * 6 + 4, &_raw[first + i], 2);
    }
    return 12 + (size_t)n * 6;
  }

  bool writeReg(uint8_t reg, uint16_t value) {
    Wire.beginTransmission(EMG_ADS_ADDR);
    Wire.write(reg);
    Wire.write((uint8_t)(value >> 8));
    Wire.write((uint8_t)(value & 0xFF));
    return Wire.endTransmission() == 0;
  }

  bool readReg(uint8_t reg, uint16_t& out) {
    Wire.beginTransmission(EMG_ADS_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom((int)EMG_ADS_ADDR, 2) != 2) return false;
    const uint16_t msb = Wire.read(), lsb = Wire.read();
    out = (msb << 8) | lsb;
    return true;
  }
};
