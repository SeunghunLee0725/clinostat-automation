#pragma once
/*
 * pump_drive.h — 펌프 구동부 (재작성).
 *
 * 이전 구현의 문제와 그 해결:
 *
 *  1) AccelStepper 를 쓰는 정상 경로(start/manualstep)와, SPM 상한을 우회하려고 핀을
 *     직접 토글하는 진단 경로(spin/probe/slowstep)가 **따로 놀았다.** 진단 경로로 낸
 *     펄스는 pos 에 안 잡혀서, 상태 보고와 실제 동작이 어긋났다.
 *     → 모든 펄스를 이 엔진 하나로 낸다. pos 는 항상 맞는다.
 *
 *  2) 진단 함수들이 **블로킹**이었다(while 루프 안에서 최대 30초). 그동안 status 발행이
 *     멈춰 브로커가 타임아웃으로 끊고, 시리얼 진단도 먹통이 됐다.
 *     → tick() 기반 비블로킹. 구동 중에도 status·명령 수신이 계속된다.
 *
 *  3) SPM 상한 2400 = 40펄스/초. 1/16 마이크로스텝이면 1회전에 3200펄스 = 80초라
 *     **육안으로 회전을 확인할 수 없다.** 매뉴얼 호환을 위해 spm 은 그대로 두되,
 *     펄스율을 직접 주는 hz 모드를 별도로 둔다(같은 엔진 → pos 도 정상 증가).
 *
 *  4) DIR 을 명시적으로 구동하지 않았다. 부동 상태면 방향이 불정이다.
 *     → 항상 정해진 값을 쓰고, dir 명령으로 바꾼다.
 *
 *  5) 급출발하면 탈조(stall)한다 → 목표 펄스율까지 선형 램프업.
 *
 * 2026-10-15 — 엔진 인스턴스를 모터 수만큼 둔다. 각 인스턴스는 PumpConfig 의
 *   motor[idx] 만 본다(핀·spm). 펄스폭·EN 극성은 보드 공통. 두 엔진이 한 loop() 안에서
 *   번갈아 tick 되므로, 한쪽의 버스트(최대 24펄스 × pulseUs)가 다른 쪽을 그만큼 늦추지만
 *   캐치업 로직이 평균 펄스율을 지킨다.
 */

#include "pump_config.h"

enum PumpMode { PM_IDLE, PM_SPINUP, PM_RUN, PM_MANUAL, PM_HZ, PM_SWEEP };

class PumpDrive {
public:
  void begin(PumpConfig* cfg, int idx) {
    _cfg = cfg;
    _idx = idx;
    applyWiring();
  }

  int index() const { return _idx; }

  // 배선 설정이 바뀌면 호출. 이전 STEP 핀은 LOW 로 내려둔다.
  void applyWiring() {
    const MotorConfig& m = motor();
    if (_activeStepGpio >= 0 && _activeStepGpio != m.stepGpio) {
      digitalWrite(_activeStepGpio, LOW);
    }
    pinMode(m.stepGpio, OUTPUT);
    digitalWrite(m.stepGpio, LOW);
    _activeStepGpio = m.stepGpio;

    if (m.dirGpio >= 0) {
      pinMode(m.dirGpio, OUTPUT);
      digitalWrite(m.dirGpio, _dirForward ? HIGH : LOW);
    }
    if (m.enConnected()) {
      pinMode(m.enGpio, OUTPUT);
      setEnable(_energized);
    }
  }

  // ---- 상태 조회 ----
  PumpMode mode()      const { return _mode; }
  long     position()  const { return _pos; }
  long     spinRem()   const { return _spinRemaining; }
  long     manualRem() const { return _manualRemaining; }
  bool     running()   const { return _mode != PM_IDLE; }
  int      sweepIndex()const { return _sweepIdx; }

  const char* modeName() const {
    switch (_mode) {
      case PM_SPINUP: return "SPINUP";
      case PM_RUN:    return "RUN";
      case PM_MANUAL: return "MANUAL";
      case PM_HZ:     return "HZ";
      case PM_SWEEP:  return "SWEEP";
      default:        return "IDLE";
    }
  }

  // 매뉴얼의 current_spm. hz 모드는 분당 스텝으로 환산해 보고한다.
  long currentSpm() const {
    switch (_mode) {
      case PM_RUN:    return motor().spm;
      case PM_SPINUP: return motor().spinspm;
      case PM_MANUAL: return motor().spm;
      case PM_HZ:     return (long)(_targetHz * 60.0f);
      default:        return 0;
    }
  }

  // ---- 명령 ----
  void start() {
    setEnable(true);
    if (motor().spinsteps > 0) { _mode = PM_SPINUP; _spinRemaining = motor().spinsteps; }
    else                       { _mode = PM_RUN; }
    beginMotion(spmToHz(currentSpm()));
  }

  void stop() {
    _mode = PM_IDLE;
    _targetHz = 0;
    _spinRemaining = _manualRemaining = 0;
    _sweepIdx = -1;
    digitalWrite(motor().stepGpio, LOW);
    // EN 이 배선돼 있으면 코일 전류를 끊는다. 미배선이면 드라이버가 계속 여자 상태다
    // (2026-07-29 현장: 회전 없이 전류만 흘러 드라이버가 과열됐다 — VMOT 를 내려야 한다).
    setEnable(false);
  }

  void once() { manual(1); }

  void manual(long steps) {
    if (steps <= 0) return;
    _prevMode = (_mode == PM_MANUAL || _mode == PM_HZ || _mode == PM_SWEEP) ? PM_IDLE : _mode;
    _manualRemaining = steps;
    _mode = PM_MANUAL;
    setEnable(true);
    // 요청된 spm 으로 돈다. 예전엔 SPM_MAX(2400=40Hz) 고정이라 게이트웨이가 보낸 spm 이
    // 무시됐고, 그 속도는 현장 모터가 진동만 하는 대역이었다(2026-10-15).
    beginMotion(spmToHz(motor().spm));
  }

  // 육안 확인용: 펄스율을 직접 준다. 매뉴얼 SPM 상한을 우회하지만 같은 엔진을 쓰므로
  // pos 는 정상 증가하고, 구동 중에도 status 가 계속 나간다.
  void runHz(float hz, float seconds) {
    if (hz < 1) hz = 1;
    if (hz > 20000) hz = 20000;
    if (seconds <= 0) seconds = 5;
    _hzEndMs = millis() + (unsigned long)(seconds * 1000.0f);
    _mode = PM_HZ;
    setEnable(true);
    beginMotion(hz);
  }

  void setDirection(bool forward) {
    _dirForward = forward;
    if (motor().dirGpio >= 0) digitalWrite(motor().dirGpio, forward ? HIGH : LOW);
  }
  bool direction() const { return _dirForward; }

  void resetPosition() { _pos = 0; }

  // 코일 여자만 켜고 끄기. EN 미배선이면 false 를 돌려주어 호출부가 알 수 있게 한다.
  bool hold(bool on) {
    if (!motor().enConnected()) return false;
    setEnable(on);
    return true;
  }

  // 핀 무결성: 쓴 값이 패드에서 그대로 읽히는지. HIGH 를 썼는데 LOW 로 읽히면
  // 그 선이 GND 로 단락됐거나 과부하다. (MCU 패드만 보는 검사 — 배선 단선은 못 잡는다)
  static bool pinCheck(int gpio, int& hi, int& lo) {
    pinMode(gpio, OUTPUT);
    digitalWrite(gpio, HIGH); delay(20); hi = digitalRead(gpio);
    digitalWrite(gpio, LOW);  delay(20); lo = digitalRead(gpio);
    return (hi == 1 && lo == 0);
  }

  // 배선 탐색: 헤더 핀을 하나씩 정해진 시간 동안 펄스한다. 비블로킹.
  // 어느 구간에서 모터가 반응하는지 보면 실제 STEP 배선을 찾을 수 있다.
  // 다른 모터의 핀은 건너뛴다 — 그 모터가 같이 돌면 결과를 읽을 수 없다.
  void sweep(float hz, unsigned long msPerPin) {
    _sweepHz    = (hz < 1) ? 1 : hz;
    _sweepMs    = msPerPin ? msPerPin : 4000;
    _sweepIdx   = 0;
    while (_sweepIdx < XIAO_D_COUNT && sweepSkip(_sweepIdx)) _sweepIdx++;
    if (_sweepIdx >= XIAO_D_COUNT) { _sweepIdx = -1; return; }
    _sweepUntil = millis() + _sweepMs;
    _mode       = PM_SWEEP;
    setEnable(true);
    applySweepPin();
  }

  // ---- 매 loop() 마다 호출 ----
  void tick() {
    if (_mode == PM_IDLE) return;

    if (_mode == PM_SWEEP) { tickSweep(); return; }

    if (_mode == PM_HZ && (long)(millis() - _hzEndMs) >= 0) { stop(); return; }

    const unsigned long now = micros();
    float hz = rampedHz(now);
    if (hz <= 0) return;
    const unsigned long interval = (unsigned long)(1000000.0f / hz);

    // 한 번의 tick 에서 최대 MAX_BURST 개까지 따라잡는다. loop() 가 잠시 늦어져도
    // 평균 펄스율이 유지되고, 무한 캐치업으로 loop 를 굶기지도 않는다.
    const int MAX_BURST = 24;
    int emitted = 0;
    while (emitted < MAX_BURST && (unsigned long)(now - _lastStepUs) >= interval) {
      _lastStepUs += interval;
      emitStep();
      emitted++;
      if (!advanceCounters()) break;   // 목표 스텝수를 다 채우면 모드가 바뀐다
    }
    // 너무 오래 밀렸으면(예: WiFi 재접속으로 loop 가 수백 ms 멈춤) 기준시각을 당겨
    // 폭주 캐치업을 막는다.
    if ((unsigned long)(now - _lastStepUs) > interval * (MAX_BURST * 4)) _lastStepUs = now;
  }

private:
  PumpConfig* _cfg = nullptr;
  int _idx = 0;

  PumpMode _mode = PM_IDLE;
  PumpMode _prevMode = PM_IDLE;
  long _pos = 0;
  long _spinRemaining = 0;
  long _manualRemaining = 0;
  bool _dirForward = true;
  bool _energized = false;
  int  _activeStepGpio = -1;

  unsigned long _lastStepUs = 0;
  unsigned long _hzEndMs = 0;

  // 램프업
  float _targetHz = 0;
  float _startHz  = 0;
  unsigned long _rampStartMs = 0;
  unsigned long _rampMs = 0;

  // 스윕
  int  _sweepIdx = -1;
  float _sweepHz = 250;
  unsigned long _sweepMs = 4000, _sweepUntil = 0;

  const MotorConfig& motor() const { return _cfg->motor[_idx]; }

  static float spmToHz(long spm) { return (float)spm / 60.0f; }

  void setEnable(bool on) {
    _energized = on;
    if (!motor().enConnected()) return;   // 미배선 → 드라이버 내부 풀다운으로 상시 활성
    digitalWrite(motor().enGpio, _cfg->enActiveLow ? (on ? LOW : HIGH) : (on ? HIGH : LOW));
  }

  // 목표 펄스율로 이행 시작. 정지 상태에서 고속으로 급출발하면 탈조하므로 램프를 건다.
  // 이미 돌고 있으면(운전 중 재명령) 지금 속도에서 이어간다 — 시작 속도로 되돌리면
  // 저속 대역을 다시 지나며 탈조한다. 램프 시작 속도·길이는 설정값(ramp:)이다.
  void beginMotion(float hz) {
    const float from = (_targetHz > 0) ? rampedHz(micros()) : (float)_cfg->rampStartHz;
    _targetHz    = hz;
    _startHz     = (from < hz) ? from : hz;
    _rampMs      = (_startHz < hz) ? (unsigned long)_cfg->rampMs : 0;
    _rampStartMs = millis();
    _lastStepUs  = micros();
    if (motor().dirGpio >= 0) digitalWrite(motor().dirGpio, _dirForward ? HIGH : LOW);
  }

  float rampedHz(unsigned long /*nowUs*/) const {
    if (_rampMs == 0) return _targetHz;
    unsigned long el = millis() - _rampStartMs;
    if (el >= _rampMs) return _targetHz;
    return _startHz + (_targetHz - _startHz) * ((float)el / (float)_rampMs);
  }

  void emitStep() {
    digitalWrite(motor().stepGpio, HIGH);
    delayMicroseconds(_cfg->pulseUs);
    digitalWrite(motor().stepGpio, LOW);
    _pos++;
  }

  // 스텝 하나를 낸 뒤의 모드 전이. 계속 돌아도 되면 true.
  bool advanceCounters() {
    if (_mode == PM_SPINUP) {
      if (--_spinRemaining <= 0) {
        _mode = PM_RUN;
        beginMotion(spmToHz(motor().spm));
        return false;
      }
    } else if (_mode == PM_MANUAL) {
      if (--_manualRemaining <= 0) {
        _mode = _prevMode;
        if (_mode == PM_IDLE) { stop(); }
        else                  { beginMotion(spmToHz(currentSpm())); }
        return false;
      }
    }
    return true;
  }

  // ENABLE 로 쓰는 핀과 다른 모터의 핀은 스윕에서 건너뛴다. EN 을 250Hz 로 두들기면
  // 드라이버가 껐다 켜졌다 반복해 결과를 읽을 수 없게 되고, 코일에 불필요한 열이 쌓인다.
  bool sweepSkip(int idx) const {
    return _cfg->sweepReserved(XIAO_D_GPIO[idx], _idx);
  }

  void applySweepPin() {
    int g = XIAO_D_GPIO[_sweepIdx];
    pinMode(g, OUTPUT);
    digitalWrite(g, LOW);
    _lastStepUs = micros();
  }

  void tickSweep() {
    if ((long)(millis() - _sweepUntil) >= 0) {
      digitalWrite(XIAO_D_GPIO[_sweepIdx], LOW);
      do { _sweepIdx++; } while (_sweepIdx < XIAO_D_COUNT && sweepSkip(_sweepIdx));
      if (_sweepIdx >= XIAO_D_COUNT) { _sweepIdx = -1; stop(); return; }
      _sweepUntil = millis() + _sweepMs;
      applySweepPin();
      return;
    }
    const unsigned long now = micros();
    const unsigned long interval = (unsigned long)(1000000.0f / _sweepHz);
    int emitted = 0;
    while (emitted < 24 && (unsigned long)(now - _lastStepUs) >= interval) {
      _lastStepUs += interval;
      int g = XIAO_D_GPIO[_sweepIdx];
      digitalWrite(g, HIGH);
      delayMicroseconds(_cfg->pulseUs);
      digitalWrite(g, LOW);
      emitted++;
    }
    if ((unsigned long)(now - _lastStepUs) > interval * 96) _lastStepUs = now;
  }
};
