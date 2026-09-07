/* ForgeDesk browser client: mic -> PCM16/16k -> WebSocket; epoch-tagged PCM <- WebSocket -> AudioWorklet player. */
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    start: $('start'), interrupt: $('interrupt'), delay: $('delay'), typed: $('typed'), typedText: $('typed-text'),
    orb: $('orb'), orbLabel: $('orb-label'), provider: $('provider'), transcript: $('transcript'), caption: $('caption'),
    mStop: $('m-stop'), mAck: $('m-ack'), mAlign: $('m-align'), heard: $('heard'),
    mTtfa: $('m-ttfa'), mLlm: $('m-llm'), mTts: $('m-tts'), mCold: $('m-cold'), tools: $('tools'), rimeConfig: $('rime-config'), log: $('log'),
  };

  let ws = null, ctx = null, player = null, micNode = null, micStream = null, recognizer = null;
  let running = false, config = null, outRate = 24000;
  const assistantBubbles = new Map(); // epoch -> element
  const toolRows = new Map();

  // ---------------------------------------------------------------- helpers
  const log = (line) => { ui.log.textContent = (line + '\n' + ui.log.textContent).slice(0, 20000); };
  const setState = (s) => { ui.orb.className = 'orb ' + s; ui.orbLabel.textContent = s; };
  const send = (obj) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); };

  function bubble(role, text, epoch) {
    const el = document.createElement('div');
    el.className = 'msg ' + role;
    el.textContent = text;
    if (epoch !== undefined) el.dataset.epoch = epoch;
    ui.transcript.appendChild(el);
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
    return el;
  }
  function assistantBubble(epoch) {
    let el = assistantBubbles.get(epoch);
    if (!el) { el = bubble('assistant', '', epoch); assistantBubbles.set(epoch, el); }
    return el;
  }
  function setProvider(name, fallback, detail) {
    const isRime = String(name).startsWith('rime');
    ui.provider.className = 'badge ' + (name === 'unavailable' ? 'badge-bad' : fallback ? 'badge-fallback' : isRime ? 'badge-rime' : 'badge-bad');
    const d = detail || {};
    ui.provider.textContent = name === 'unavailable'
      ? 'TTS: UNAVAILABLE (captions only)'
      : `TTS: ${isRime ? 'Rime' : name.toUpperCase()} · ${d.model_id || ''} · ${d.speaker || ''} · ${d.lang || ''}${fallback ? ' · FALLBACK PATH' : ''}`;
    if (!isRime) ui.provider.title = 'Not Rime — this is not the judged path';
  }
  function renderConfig(engine, stt, llm) {
    const rows = [
      ['provider', engine.provider], ['engine', engine.engine], ['model_id', engine.model_id], ['speaker', engine.speaker], ['lang', engine.lang],
      ['endpoint', engine.endpoint], ['audio_format', engine.audio_format], ['transport', engine.transport], ['alignment', engine.alignment],
      ['stt', (stt && stt.provider) || '-'], ['llm', llm || '-'],
    ];
    ui.rimeConfig.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v ?? '-'}</dd>`).join('');
  }
  function renderHeard(heard, unheard) {
    ui.heard.innerHTML = `<span class="yes">${escapeHtml(heard || '(nothing)')}</span> <span class="no">${escapeHtml(unheard || '')}</span>`;
  }
  function escapeHtml(s) { return String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
  function toolRow(key, name, args, status) {
    let li = toolRows.get(key);
    if (!li) { li = document.createElement('li'); toolRows.set(key, li); ui.tools.prepend(li); }
    if (args) li.dataset.args = Object.values(args).join(', ');
    li.innerHTML = `<span>${escapeHtml(key)} ${escapeHtml(name)}(${escapeHtml(li.dataset.args || '')})</span><span class="st ${status}">${status}</span>`;
    while (ui.tools.children.length > 8) ui.tools.removeChild(ui.tools.lastChild);
  }

  // ---------------------------------------------------------------- audio
  async function startAudio(rate) {
    outRate = rate;
    try { ctx = new AudioContext({ sampleRate: rate }); } catch (_) { ctx = new AudioContext(); }
    if (ctx.state === 'suspended') await ctx.resume();
    await ctx.audioWorklet.addModule('/static/worklets/player.js');
    player = new AudioWorkletNode(ctx, 'player-processor', { outputChannelCount: [1] });
    player.connect(ctx.destination);
    player.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'playhead') send({ type: 'playhead', epoch: m.epoch, played: m.played });
      else if (m.type === 'flushed') send({ type: 'flushed', epoch: m.epoch, played: m.played });
      else if (m.type === 'first_audio') send({ type: 'first_audio', epoch: m.epoch });
    };
    log(`audio ready: context ${ctx.sampleRate} Hz, server ${rate} Hz`);
  }

  async function startMic() {
    try {
      await ctx.audioWorklet.addModule('/static/worklets/mic.js');
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const src = ctx.createMediaStreamSource(micStream);
      micNode = new AudioWorkletNode(ctx, 'mic-processor', { numberOfOutputs: 0 });
      micNode.port.onmessage = (e) => { if (ws && ws.readyState === 1) ws.send(e.data); };
      src.connect(micNode);
      log('microphone streaming (16 kHz PCM16)');
    } catch (err) {
      log('microphone unavailable (' + err.message + '); use the text box and the Interrupt button');
      $('hint').textContent = 'No microphone: type turns below and use the Interrupt button to barge in.';
    }
  }

  function startBrowserSTT() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { log('Web Speech API not available in this browser; type your turns or configure Deepgram.'); return; }
    recognizer = new SR();
    recognizer.continuous = true; recognizer.interimResults = true; recognizer.lang = 'en-US';
    recognizer.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        send({ type: 'transcript', text: r[0].transcript.trim(), final: r.isFinal });
      }
    };
    recognizer.onend = () => { if (running) { try { recognizer.start(); } catch (_) {} } };
    recognizer.onerror = (e) => log('speech recognition error: ' + e.error);
    recognizer.start();
  }

  // ---------------------------------------------------------------- websocket
  function connect() {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { log('connected'); send({ type: 'hello' }); };
    ws.onclose = () => { log('disconnected'); stop(); };
    ws.onmessage = async (e) => {
      if (e.data instanceof ArrayBuffer) {
        const dv = new DataView(e.data);
        const epoch = dv.getUint32(0, true), seq = dv.getUint32(4, true);
        const pcm = e.data.slice(8);
        if (player) player.port.postMessage({ type: 'audio', epoch, seq, pcm, rate: outRate }, [pcm]);
        return;
      }
      const m = JSON.parse(e.data);
      await handle(m);
    };
  }

  async function handle(m) {
    switch (m.type) {
      case 'fatal':
        log('FATAL: ' + m.error + ' — ' + (m.hint || ''));
        ui.provider.className = 'badge badge-bad'; ui.provider.textContent = 'TTS: ' + m.error;
        break;
      case 'config':
        if (config) break; // already set up for this connection
        config = m;
        renderConfig(m.tts_engine, m.stt, m.llm);
        setProvider(m.tts_engine.engine || m.tts_engine.provider, false, m.tts_engine);
        ui.delay.value = String(m.settings.tool_delay_ms || 0);
        try { await startAudio(m.output_sample_rate); } catch (err) { log('audio setup failed: ' + err.message); }
        setState('listening');
        ui.interrupt.disabled = false;
        if (ctx) startMic().then(() => { if (m.stt && m.stt.provider === 'browser-webspeech') startBrowserSTT(); });
        break;
      case 'provider':
        setProvider(m.tts, m.fallback, m.detail);
        break;
      case 'state':
        setState(m.state);
        if (m.state === 'listening') ui.caption.textContent = '';
        break;
      case 'transcript':
        if (m.role === 'user') {
          if (m.final) { ui.caption.textContent = ''; bubble('user', m.text); }
          else ui.caption.textContent = '… ' + m.text;
        } else {
          const el = assistantBubble(m.epoch);
          if (m.final) { el.textContent = m.text; el.dataset.full = '1'; }
          else if (!el.dataset.full) el.textContent = ((el.textContent || '') + ' ' + m.text).trim();
        }
        break;
      case 'speak':
        ui.caption.textContent = '🔊 ' + m.text;
        break;
      case 'flush':
        if (player) player.port.postMessage({ type: 'flush', epoch: m.epoch });
        break;
      case 'heard': {
        renderHeard(m.heard, m.unheard);
        ui.mStop.textContent = m.stop_ms + ' ms';
        ui.mAlign.textContent = m.method;
        ui.caption.textContent = '';
        const el = assistantBubbles.get(m.epoch);
        if (el) {
          el.classList.add('interrupted');
          el.innerHTML = `${escapeHtml(m.heard)} <span class="unheard">${escapeHtml(m.unheard)}</span><span class="tag">interrupted · heard-state via ${escapeHtml(m.method)}</span>`;
          el.dataset.full = '1';
        }
        break;
      }
      case 'tool':
        break; // rows are driven by telemetry events (keyed by pid) in onEvent
      case 'event':
        onEvent(m);
        break;
    }
  }

  function onEvent(ev) {
    const k = ev.kind;
    if (['tts_request', 'state', 'vad_speech_end'].includes(k)) return;
    log(`${(ev.t_ms / 1000).toFixed(2)}s ${k} ${JSON.stringify(Object.fromEntries(Object.entries(ev).filter(([x]) => !['t_ms', 'kind', 'session', 'type'].includes(x)))).slice(0, 220)}`);
    if (k === 'interrupt_stopped') { ui.mStop.textContent = ev.stop_ms + ' ms'; ui.mAck.textContent = ev.flush_ack_ms + ' ms'; ui.mAlign.textContent = ev.method; renderHeard(ev.heard, ev.unheard); }
    if (k === 'response_complete') {
      ui.mTtfa.textContent = ev.ttfa_ms != null ? ev.ttfa_ms + ' ms' : (ev.audio_first_sent_ms != null ? ev.audio_first_sent_ms + ' ms (sent)' : '–');
      ui.mLlm.textContent = ev.llm_first_token_ms != null ? ev.llm_first_token_ms + ' ms' : '–';
      ui.mTts.textContent = ev.tts_first_byte_ms != null ? ev.tts_first_byte_ms + ' ms' : '–';
      ui.mCold.textContent = ev.cold ? 'cold (first synthesis)' : 'warm';
      const el = assistantBubbles.get(ev.epoch);
      if (el && !el.dataset.full) el.dataset.full = '1';
    }
    if (k === 'tool_start') toolRow(ev.pid, ev.name, ev.args, 'running');
    if (k === 'tool_end') toolRow(ev.pid, ev.name, null, ev.status === 'fenced_at_commit' ? 'fenced' : ev.status);
    if (k === 'tool_orphaned') toolRow(ev.pid, ev.name, null, 'orphaned');
    if (k === 'tool_stale_result_fenced') toolRow(ev.pid, ev.name, null, 'fenced');
    if (k === 'tool_result_reconciled') toolRow(ev.pid, ev.name, null, 'reconciled');
    if (k === 'tool_discarded') toolRow(ev.pid, ev.name, null, 'discarded');
    if (k === 'tool_cancelled') toolRow(ev.pid, ev.name, null, 'cancelled');
    if (k === 'resume_unheard') log('resuming from unheard text: ' + ev.unheard);
  }

  // ---------------------------------------------------------------- controls
  async function start() {
    running = true; config = null;
    ui.start.textContent = 'End call'; ui.start.classList.remove('primary'); ui.start.classList.add('danger');
    connect();
  }
  function stop() {
    running = false;
    ui.start.textContent = 'Start call'; ui.start.classList.add('primary'); ui.start.classList.remove('danger');
    ui.interrupt.disabled = true; setState('idle');
    if (recognizer) { try { recognizer.onend = null; recognizer.stop(); } catch (_) {} recognizer = null; }
    if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
    if (ctx) { ctx.close(); ctx = null; player = null; micNode = null; }
    if (ws && ws.readyState === 1) ws.close();
    ws = null;
  }
  ui.start.onclick = () => (running ? stop() : start());
  ui.interrupt.onclick = () => send({ type: 'interrupt' });
  ui.delay.onchange = () => send({ type: 'set', tool_delay_ms: Number(ui.delay.value) });
  ui.typed.onsubmit = (e) => {
    e.preventDefault();
    const t = ui.typedText.value.trim();
    if (!t) return;
    if (!running) { log('start the call first'); return; }
    send({ type: 'text', text: t });
    ui.typedText.value = '';
  };
})();
