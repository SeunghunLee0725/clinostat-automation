/* 음성 비서 위젯 — 기존 대시보드(/)에 덧붙이는 떠 있는 패널 (2026-11-05, TTS·HTTPS 안내 2026-11-08)
 *
 * 설계(콘솔 /console 과 같다):
 * - 말/글 → 서버 /api/assistant/chat(구독제 Claude) 또는 /api/assistant/command(규칙 파서).
 *   어느 쪽인지는 /api/assistant/status 가 정한다.
 * - 서버 답의 reply(글)는 말풍선·TTS 로만 간다. 실행은 action 객체로만, 그것도 페이지가 넘겨 준
 *   executors[kind] 를 통해서만 한다. 시작류는 [실행] 확인, 정지류는 즉시.
 * - 실행 결과는 "[실행 결과] …" 로 비서에게 되돌려 대화가 이어진다. 실험 중에는 5분마다·종료 시 자동 보고.
 *
 * 사용: VoiceAssistant.mount({ executors: { pump_run: async (p) => {…}, … }, log: (msg) => {…} })
 */
(function () {
  'use strict';

  const KINDS = ['pump_run', 'pump_stop', 'clinostat_stop', 'clinostat_start', 'stop_all', 'estop',
    'sensor_start', 'sensor_stop', 'experiment_run', 'experiment_stop', 'strategy_set', 'status'];
  const AUTO_REPORT_MS = 5 * 60 * 1000;
  const HINT = '마이크를 누르고 말하세요. 예: "fibonacci 조건으로 10분 실험 시작", "펌프 2 400헤르츠 5초", "지금 상태 알려 줘"';
  const INSECURE_HINT = 'HTTP 주소에서는 Chrome 이 마이크를 막습니다(비보안 출처). HTTPS 주소로 접속하면 마이크가 열립니다. 글 입력은 그대로 됩니다.';
  // Web Speech API 오류 코드 → 운영자용 문구. not-allowed 는 거의 항상 HTTP 출처 때문이다.
  const SR_ERRORS = {
    'not-allowed': '마이크 사용이 막혔습니다. HTTP 주소라면 HTTPS 로 접속하고, HTTPS 라면 주소창 자물쇠에서 마이크를 허용하세요.',
    'service-not-allowed': '이 브라우저에서 음성 인식 서비스를 쓸 수 없습니다.',
    'no-speech': '말소리가 들리지 않았습니다. 마이크를 다시 누르고 말하세요.',
    'audio-capture': '마이크를 찾지 못했습니다. 연결과 시스템 마이크 권한을 확인하세요.',
    'network': '음성 인식 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.',
    'aborted': '음성 인식이 중단됐습니다.',
    'language-not-supported': '이 브라우저는 한국어 음성 인식을 지원하지 않습니다.',
  };
  // TTS: macOS 의 장난 목소리(Eddy·Grandma…)는 제외하고, 자연스러운 순서로 고른다. 운영자가 고른 목소리가 우선.
  const NOVELTY_VOICES = ['Eddy', 'Flo', 'Grandma', 'Grandpa', 'Reed', 'Rocko', 'Sandy', 'Shelley', 'Albert', 'Bad', 'Bahh',
    'Bells', 'Boing', 'Bubbles', 'Cellos', 'Good', 'Jester', 'Organ', 'Superstar', 'Trinoids', 'Whisper', 'Wobble', 'Zarvox'];
  const PREFERRED_VOICES = ['Natural', 'Google 한국의', 'Yuna', '유나', 'SunHi', 'InJoon', 'Heami'];
  // 읽기용 치환: 영문 상태어·단위·기호를 한국어 발음으로. 화면 말풍선에는 원문이 남는다.
  const SPEECH_WORDS = [
    [/\bdegraded\b/gi, '저하'], [/\bunreachable\b/gi, '연결 안 됨'], [/\bfault\b/gi, '고장'], [/\bidle\b/gi, '대기'],
    [/\bstopped\b/gi, '정지'], [/\brunning\b/gi, '동작 중'], [/\bcompleted\b/gi, '완료'], [/\bestop\b/gi, '비상 정지'],
    [/\bok\b/gi, '정상'], [/\bnot_connected\b/gi, '연결 안 됨'],
    [/(\d)\s*rpm\b/gi, '$1 알피엠'], [/(\d)\s*k?hz\b/gi, '$1 헤르츠'], [/(\d)\s*rad\/s/gi, '$1 라디안 매초'],
    [/(\d)\s*kΩ/g, '$1 킬로옴'], [/(\d)\s*Ω/g, '$1 옴'], [/(\d)\s*mV\b/g, '$1 밀리볼트'], [/(\d)\s*ms\b/g, '$1 밀리초'],
    [/(\d)\s*s\b/g, '$1 초'], [/(\d)\s*%/g, '$1 퍼센트'], [/(\d)\s*°C/g, '$1 도'],
    [/[\[\]*_`#]/g, ' '], [/→/g, ' 에서 '], [/\s·\s/g, ', '], [/(\d)\s*~\s*(\d)/g, '$1 에서 $2'],
  ];

  const CSS = `
#vaRoot{position:fixed;right:18px;bottom:18px;z-index:9000;font-family:inherit;color:var(--text,#eee)}
#vaFab{width:60px;height:60px;border-radius:50%;border:0;background:var(--primary,#e94560);color:var(--on-primary,#fff);box-shadow:0 6px 18px rgba(0,0,0,.45);cursor:pointer;display:flex;align-items:center;justify-content:center}
#vaFab.listening{background:var(--green,#0cca4a);color:#fff;animation:vaPulse 1.1s infinite}
#vaFab.open{display:none}
@keyframes vaPulse{0%{box-shadow:0 0 0 0 rgba(12,202,74,.6)}100%{box-shadow:0 0 0 16px rgba(12,202,74,0)}}
#vaPanel{display:none;width:380px;max-width:calc(100vw - 36px);background:var(--surface,#16213e);border:1px solid var(--line,#0f3460);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.5);overflow:hidden}
#vaPanel.open{display:flex;flex-direction:column}
#vaHead{display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--fill-soft,#0f3460);border-bottom:1px solid var(--line,#0f3460);font-size:14px;font-weight:600}
#vaHead .va-chip{font-size:11px;font-weight:500;padding:2px 8px;border-radius:999px;background:#555;margin-left:auto;white-space:nowrap;max-width:170px;overflow:hidden;text-overflow:ellipsis}
#vaHead .va-chip.ok{background:var(--green,#0cca4a);color:#fff}
#vaHead .va-chip.warn{background:#f8961e;color:#321}
#vaHead .va-chip.bad{background:#e94560}
#vaClose{background:none;border:0;color:var(--text-dim,#ccc);font-size:18px;cursor:pointer;padding:0 4px}
#vaBody{display:flex;gap:10px;align-items:center;padding:10px 12px}
#vaMic{width:52px;height:52px;border-radius:50%;border:0;background:var(--primary,#e94560);color:var(--on-primary,#fff);cursor:pointer;flex:none;display:flex;align-items:center;justify-content:center}
#vaMic.on{background:var(--green,#0cca4a);color:#fff;animation:vaPulse 1.1s infinite}
#vaMic:disabled{background:#555;cursor:not-allowed}
#vaHint{font-size:12px;color:var(--text-dim,#bbb);line-height:1.35}
#vaChat{height:200px;overflow-y:auto;padding:6px 12px;display:flex;flex-direction:column;gap:6px;background:var(--bg,#1a1a2e)}
.va-bubble{max-width:92%;padding:7px 10px;border-radius:10px;font-size:13px;line-height:1.4;white-space:pre-wrap;word-break:break-word}
.va-bubble.me{align-self:flex-end;background:var(--fill-soft,#0f3460);border:1px solid var(--line,transparent)}
.va-bubble.ai{align-self:flex-start;background:var(--surface,#243b6b);border:1px solid var(--line,transparent)}
.va-bubble.sys{align-self:center;background:transparent;color:var(--text-dim,#999);font-size:11px}
#vaConfirm{display:none;gap:8px;padding:8px 12px;background:var(--danger-soft,#3a1d2a);border-top:1px solid var(--danger-line,#e94560)}
#vaConfirm.show{display:flex}
#vaConfirm span{flex:1;font-size:12px;align-self:center}
.va-btn{border:1px solid var(--line-strong,#4cc9f0);background:var(--field,transparent);color:var(--text,#eee);border-radius:8px;padding:7px 12px;font-size:13px;cursor:pointer}
.va-btn.primary{background:var(--primary,#e94560);border-color:var(--primary,#e94560);color:var(--on-primary,#fff)}
#vaFoot{display:flex;gap:6px;padding:8px 12px;border-top:1px solid var(--line,#0f3460);flex-wrap:wrap}
#vaText{flex:1;min-width:140px;background:var(--field,#1a1a2e);border:1px solid var(--line-strong,#0f3460);color:var(--text,#eee);border-radius:8px;padding:7px 9px;font-size:13px}
#vaOpts{display:flex;gap:12px;padding:0 12px 8px;font-size:12px;color:var(--text-dim,#bbb)}
#vaOpts label{display:flex;align-items:center;gap:4px;cursor:pointer}
#vaOpts select{margin-left:auto;max-width:150px;font-size:11px;background:var(--field,#1a1a2e);color:var(--text,#eee);border:1px solid var(--line-strong,#0f3460);border-radius:6px}
#vaHint a{color:var(--primary,#e94560);font-weight:600}
`;

  const MIC_SVG = '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5 11a7 7 0 0 0 14 0M12 18v3"></path></svg>';

  const HTML = `
<button type="button" id="vaFab" aria-label="음성 비서 열기" title="음성 비서">${MIC_SVG}</button>
<div id="vaPanel" role="dialog" aria-label="음성 비서">
  <div id="vaHead">음성 비서 <span class="va-chip" id="vaChip">LLM 확인 중</span><button type="button" id="vaClose" aria-label="닫기">×</button></div>
  <div id="vaBody">
    <button type="button" id="vaMic" aria-label="음성 명령 듣기">${MIC_SVG}</button>
    <div id="vaHint">${HINT}</div>
  </div>
  <div id="vaChat"><div class="va-bubble sys">시작 명령은 [실행]을 눌러야 보내고, 정지는 바로 보냅니다. 비서의 글은 실행되지 않습니다.</div></div>
  <div id="vaConfirm"><span id="vaConfirmText"></span><button type="button" class="va-btn primary" id="vaConfirmRun">실행</button><button type="button" class="va-btn" id="vaConfirmCancel">취소</button></div>
  <div id="vaFoot"><input id="vaText" type="text" placeholder="글로 입력해도 됩니다" autocomplete="off"><button type="button" class="va-btn" id="vaSend">보내기</button><button type="button" class="va-btn" id="vaSummary">상황 보고</button></div>
  <div id="vaOpts"><label><input type="checkbox" id="vaTts" checked>음성으로 읽기</label><label><input type="checkbox" id="vaAutoReport" checked>실험 자동 보고</label><select id="vaVoice" title="읽어 주는 목소리"><option value="">목소리 자동</option></select></div>
</div>`;

  const state = {
    mode: 'rule',            // 'claude' | 'rule'
    recognition: null,
    listening: false,
    pending: null,
    executors: {},
    log: function () {},
    experimentRunning: false,
    lastAutoReport: 0,
    sessionId: 'default',
    insecure: false,          // HTTP 출처 — Chrome 이 마이크를 막는다
    httpsUrl: null,
  };

  function $(id) { return document.getElementById(id); }

  function sessionId() {
    try {
      let id = sessionStorage.getItem('vaSession');
      if (!id) { id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : 's' + Date.now(); sessionStorage.setItem('vaSession', id); }
      return id.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64) || 'default';
    } catch (err) { return 'default'; }
  }

  async function post(path, body) {
    const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const payload = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      const err = (payload && payload.error) || {};
      throw new Error(err.message || err.code || res.statusText);
    }
    return payload;
  }

  function bubble(kind, text) {
    const host = $('vaChat');
    const el = document.createElement('div');
    el.className = 'va-bubble ' + kind;
    el.textContent = text;
    host.appendChild(el);
    while (host.childElementCount > 40) host.firstElementChild.remove();
    host.scrollTop = host.scrollHeight;
    return el;
  }

  function setChip(cls, text) { const chip = $('vaChip'); chip.className = 'va-chip ' + cls; chip.textContent = text; }

  // ─── 음성 출력 ───
  function koreanVoices() {
    if (!window.speechSynthesis) return [];
    return window.speechSynthesis.getVoices().filter(function (v) { return v.lang && v.lang.replace('_', '-').toLowerCase().startsWith('ko'); });
  }

  function savedVoiceName() {
    try { return localStorage.getItem('vaVoice') || ''; } catch (err) { return ''; }
  }

  function pickVoice() {
    const voices = koreanVoices();
    const saved = savedVoiceName();
    const chosen = saved && voices.find(function (v) { return v.name === saved; });
    if (chosen) return chosen;
    const usable = voices.filter(function (v) { return NOVELTY_VOICES.indexOf(v.name.split(' ')[0]) === -1; });
    for (let i = 0; i < PREFERRED_VOICES.length; i += 1) {
      const hit = usable.find(function (v) { return v.name.indexOf(PREFERRED_VOICES[i]) !== -1; });
      if (hit) return hit;
    }
    return usable[0] || voices[0] || null;
  }

  function normalizeForSpeech(text) {
    return SPEECH_WORDS.reduce(function (acc, pair) { return acc.replace(pair[0], pair[1]); }, String(text)).replace(/\s{2,}/g, ' ').trim();
  }

  // 문장 단위로 나눠 읽는다 — Chrome 은 긴 발화(≈15 s)를 중간에 끊고, 문장 사이 쉼이 자연스러워진다.
  function splitSentences(text) {
    return text.split(/(?<=[.!?。])\s+|\n+/).map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function speak(text) {
    if (!$('vaTts').checked || !window.speechSynthesis || !text) return;
    window.speechSynthesis.cancel();
    const voice = pickVoice();
    splitSentences(normalizeForSpeech(text)).forEach(function (sentence) {
      const utter = new SpeechSynthesisUtterance(sentence);
      utter.lang = 'ko-KR';
      if (voice) utter.voice = voice;
      utter.rate = 1.0;
      utter.pitch = 1.0;
      window.speechSynthesis.speak(utter);
    });
  }

  function fillVoiceList() {
    const sel = $('vaVoice');
    if (!sel) return;
    const saved = savedVoiceName();
    const auto = pickVoice();
    sel.innerHTML = '';
    const first = document.createElement('option');
    first.value = '';
    first.textContent = '목소리 자동' + (auto ? ' (' + auto.name.split(' ')[0] + ')' : '');
    sel.appendChild(first);
    koreanVoices().forEach(function (v) {
      const opt = document.createElement('option');
      opt.value = v.name;
      opt.textContent = v.name + (v.localService ? '' : ' · 온라인');
      if (v.name === saved) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function onVoiceChange() {
    try { if ($('vaVoice').value) localStorage.setItem('vaVoice', $('vaVoice').value); else localStorage.removeItem('vaVoice'); } catch (err) { /* 저장 못 해도 이번 세션엔 적용 */ }
    speak('이 목소리로 읽겠습니다.');
  }

  // ─── 음성 인식 ───
  function initSpeech() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      $('vaHint').textContent = '이 브라우저는 음성 인식을 지원하지 않습니다 (Chrome 권장). 아래 입력창으로 명령하세요.';
      $('vaMic').disabled = true;
      return;
    }
    if (!window.isSecureContext) {
      state.insecure = true;
      $('vaMic').disabled = true;
      showInsecureHint();
      return;
    }
    const rec = new SR();
    rec.lang = 'ko-KR';
    rec.interimResults = true;
    rec.continuous = false;
    rec.onstart = function () { setListening(true); };
    rec.onend = function () { setListening(false); };
    rec.onerror = function (ev) {
      const msg = SR_ERRORS[ev.error] || ('음성 인식 오류: ' + ev.error);
      state.log('음성 인식 오류(' + ev.error + '): ' + msg);
      setListening(false);
      $('vaHint').textContent = msg;
    };
    rec.onresult = function (ev) {
      let interim = '', finalText = '';
      for (let i = ev.resultIndex; i < ev.results.length; i += 1) {
        const chunk = ev.results[i][0].transcript;
        if (ev.results[i].isFinal) finalText += chunk; else interim += chunk;
      }
      if (interim) $('vaHint').textContent = '듣는 중… ' + interim;
      if (finalText.trim()) sendCommand(finalText.trim());
    };
    state.recognition = rec;
  }

  function showInsecureHint() {
    const hint = $('vaHint');
    hint.textContent = INSECURE_HINT + ' ';
    if (!state.httpsUrl) return;
    const a = document.createElement('a');
    a.href = state.httpsUrl.replace(/\/$/, '') + location.pathname + location.search;
    a.textContent = 'HTTPS 로 열기';
    hint.appendChild(a);
  }

  function setListening(on) {
    state.listening = on;
    $('vaMic').classList.toggle('on', on);
    $('vaFab').classList.toggle('listening', on);
    if (!on) $('vaHint').textContent = HINT; else $('vaHint').textContent = '듣는 중…';
  }

  function toggleMic() {
    if (!state.recognition) return;
    if (state.listening) { state.recognition.stop(); return; }
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    try { state.recognition.start(); } catch (err) { state.log('마이크를 열 수 없습니다: ' + err.message); }
  }

  // ─── 대화 ───
  async function sendCommand(text, opts) {
    const quiet = !!(opts && opts.quiet);
    bubble(quiet ? 'sys' : 'me', text);
    const thinking = bubble('sys', state.mode === 'claude' ? 'Claude 생각 중…' : '해석 중…');
    try {
      const data = state.mode === 'claude'
        ? await post('/api/assistant/chat', { session_id: state.sessionId, text: text })
        : await post('/api/assistant/command', { text: text });
      thinking.remove();
      await handleReply(data);
    } catch (err) {
      thinking.textContent = '서버 오류: ' + err.message;
    }
  }

  async function handleReply(data) {
    const action = data.action;
    const latency = data.latency_ms ? ' · ' + (data.latency_ms / 1000).toFixed(1) + ' s' : '';
    const source = data.source === 'claude' ? 'Claude ' + (data.model || '') + latency
      : data.source === 'llm' ? 'LLM ' + (data.llm_host || '') + latency
      : (data.source === 'rule' ? '규칙' : '응답 없음' + (data.error ? ' — ' + data.error : ''));
    bubble('ai', data.reply);
    bubble('sys', source);
    speak(data.reply);
    state.log('음성 · "' + data.text + '" → ' + (action ? action.kind : '대화') + ' (' + source + ')');
    if (!action || action.kind === 'invalid' || action.kind === 'status') { showConfirm(null); return; }
    if (action.requires_confirm) { showConfirm(action); return; }
    await executeAction(action);
  }

  function showConfirm(action) {
    state.pending = action;
    $('vaConfirm').classList.toggle('show', !!action);
    $('vaConfirmText').textContent = action ? action.summary : '';
  }

  async function executeAction(action) {
    showConfirm(null);
    const run = state.executors[action.kind];
    try {
      if (action.kind === 'status') return;
      if (typeof run !== 'function') throw new Error('이 화면에서 지원하지 않는 명령: ' + action.kind);
      await run(action.params || {});
      bubble('sys', '실행함 — ' + action.summary);
      await reportResult(action, true, '');
    } catch (err) {
      const detail = (err && err.message) || '원인 미상';
      bubble('sys', '실행 실패 — ' + detail);
      speak('실행하지 못했습니다. ' + detail);
      await reportResult(action, false, detail);
    }
  }

  async function reportResult(action, ok, detail) {
    if (state.mode !== 'claude') return;
    await sendCommand('[실행 결과] ' + action.summary + ' → ' + (ok ? '성공' : '실패: ' + detail), { quiet: true });
  }

  async function requestSummary() {
    if (state.mode === 'claude') { await sendCommand('지금 상황을 한두 문장으로 보고해 줘'); return; }
    const thinking = bubble('sys', '상황 요약 생성 중…');
    try {
      const data = await post('/api/assistant/summary', {});
      thinking.remove();
      await handleReply(Object.assign({}, data, { text: '상황 요약', action: { kind: 'status' } }));
    } catch (err) { thinking.textContent = '요약 실패: ' + err.message; }
  }

  async function probe() {
    try {
      const res = await fetch('/api/assistant/status');
      const data = await res.json();
      if (data.https_url && data.https_url !== state.httpsUrl) {
        state.httpsUrl = data.https_url;
        if (state.insecure) showInsecureHint();
      }
      const claude = data.claude || {};
      if (claude.configured && claude.logged_in) {
        state.mode = 'claude';
        setChip('ok', 'Claude ' + (claude.model || ''));
        return;
      }
      state.mode = 'rule';
      const up = (data.hosts || []).find(function (h) { return h.reachable; });
      setChip(up ? 'warn' : 'bad', up ? '규칙 + ' + up.name : '규칙 파서만' + (claude.error ? ' · ' + claude.error : ''));
    } catch (err) {
      state.mode = 'rule';
      setChip('bad', 'LLM 상태 불명');
    }
  }

  // ─── 실험 자동 보고 ───
  function autoReportDue() {
    return $('vaAutoReport').checked && state.mode === 'claude' && (Date.now() - state.lastAutoReport) >= AUTO_REPORT_MS;
  }

  async function pollExperiment() {
    let st;
    try { st = await (await fetch('/api/experiment/status')).json(); } catch (err) { return; }
    const running = !!st.running;
    if (running && !state.experimentRunning) state.lastAutoReport = Date.now();
    if (running && autoReportDue()) {
      state.lastAutoReport = Date.now();
      await sendCommand('[자동 보고] 실험 진행 상황(경과·잔여 시간, 지표)을 한두 문장으로 보고해 줘', { quiet: true });
    } else if (!running && state.experimentRunning && $('vaAutoReport').checked && state.mode === 'claude') {
      await sendCommand('[자동 보고] 방금 끝난 실험 결과를 판정과 함께 두 문장으로 보고해 줘', { quiet: true });
    }
    state.experimentRunning = running;
  }

  // ─── 마운트 ───
  function mount(options) {
    const opts = options || {};
    state.executors = opts.executors || {};
    state.log = typeof opts.log === 'function' ? opts.log : function () {};
    state.sessionId = sessionId();
    KINDS.forEach(function (kind) {
      if (kind !== 'status' && typeof state.executors[kind] !== 'function') state.log('음성 비서: ' + kind + ' 실행기가 없습니다');
    });

    const style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);
    const root = document.createElement('div');
    root.id = 'vaRoot';
    root.innerHTML = HTML;
    document.body.appendChild(root);

    const open = function (on) { $('vaPanel').classList.toggle('open', on); $('vaFab').classList.toggle('open', on); };
    $('vaFab').onclick = function () { open(true); };
    $('vaClose').onclick = function () { open(false); if (state.listening) toggleMic(); };
    $('vaMic').onclick = toggleMic;
    $('vaSummary').onclick = requestSummary;
    $('vaConfirmRun').onclick = function () { if (state.pending) executeAction(state.pending); };
    $('vaConfirmCancel').onclick = function () { bubble('sys', '취소함'); showConfirm(null); };
    const sendTyped = function () { const t = $('vaText').value.trim(); if (!t) return; $('vaText').value = ''; sendCommand(t); };
    $('vaSend').onclick = sendTyped;
    $('vaText').addEventListener('keydown', function (ev) { if (ev.key === 'Enter') sendTyped(); });
    $('vaTts').onchange = probe;

    initSpeech();
    probe();
    setInterval(probe, 60000);
    setInterval(pollExperiment, 10000);
    $('vaVoice').onchange = onVoiceChange;
    fillVoiceList();
    if (window.speechSynthesis) window.speechSynthesis.onvoiceschanged = fillVoiceList;
  }

  window.VoiceAssistant = { mount: mount, KINDS: KINDS.slice() };
})();
