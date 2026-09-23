const $ = id => document.getElementById(id);
const ui = {
  meeting: $('meeting-id'), language: $('language'), speaker: $('speaker'),
  start: $('start-button'), pause: $('pause-button'), flush: $('flush-button'),
  minutesButton: $('minutes-button'), end: $('end-button'), play: $('play-button'),
  connection: $('connection-pill'), mic: $('mic-indicator'), transcript: $('transcript'),
  partial: $('partial'), state: $('agent-state'), search: $('search-status'),
  answer: $('answer'), sources: $('sources'), minutes: $('minutes'),
  minutesPhase: $('minutes-phase'), notice: $('notice'),
};

const app = {
  socket: null, stream: null, audioContext: null, source: null, worklet: null,
  socketReady: false,
  reconnectTimer: null, reconnectDelay: 500, shouldReconnect: false,
  active: false, recording: false, ending: false, meetingId: '',
  nextSeq: 0, nextOffset: 0, lastAck: -1, pending: new Map(),
  utterances: new Set(), answerRequest: null, latestAudio: null,
  audioQueue: [], playing: false,
};

function notice(message, error = false) {
  ui.notice.textContent = message;
  ui.notice.classList.toggle('error', error);
}

function storageKey(meetingId) { return `xiaohui-audio-v1:${meetingId}`; }
function safeGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function safeSet(key, value) { try { localStorage.setItem(key, value); } catch { /* Storage may be disabled. */ } }
function updateControls() {
  ui.start.disabled = app.recording || app.ending;
  ui.start.textContent = app.active ? '继续收音' : '开始收音';
  ui.pause.disabled = !app.recording;
  ui.flush.disabled = !app.active || app.ending;
  ui.minutesButton.disabled = !app.active || app.ending;
  ui.end.disabled = !app.active || app.ending;
  ui.meeting.disabled = app.active;
  ui.language.disabled = app.active;
  ui.speaker.disabled = app.active;
  ui.mic.textContent = app.recording ? '● 正在收音' : '麦克风关闭';
  ui.mic.classList.toggle('live', app.recording);
  ui.connection.classList.toggle('recording', app.recording);
}

function initializeMeeting() {
  const id = ui.meeting.value.trim();
  if (!/^[\w-]{1,80}$/.test(id)) throw new Error('会议编号只可包含英文字母、数字、下划线和连字符。');
  app.meetingId = id;
  safeSet('xiaohui-last-meeting', id);
  const checkpoint = safeGet(storageKey(id));
  if (checkpoint) {
    try {
      const parsed = JSON.parse(checkpoint);
      if (Number.isSafeInteger(parsed.seq) && Number.isSafeInteger(parsed.offset) && parsed.seq >= -1 && parsed.offset >= 0) {
        app.lastAck = parsed.seq;
        app.nextSeq = parsed.seq + 1;
        app.nextOffset = parsed.offset;
      }
    } catch { /* Start from the beginning if the checkpoint is invalid. */ }
  }
}

function wsUrl() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${location.host}/ws/meetings/${encodeURIComponent(app.meetingId)}`;
}

function sendControl(type) {
  if (app.socket?.readyState !== WebSocket.OPEN) {
    notice('连接已断开，正在重连。', true);
    return false;
  }
  app.socket.send(JSON.stringify({ type }));
  return true;
}

function connect() {
  if (app.socket?.readyState === WebSocket.OPEN) return Promise.resolve();
  if (app.socket?.readyState === WebSocket.CONNECTING) return app.connectPromise;
  app.connectPromise = new Promise((resolve, reject) => {
    const socket = new WebSocket(wsUrl());
    let handshakeComplete = false;
    app.socket = socket;
    app.socketReady = false;
    ui.connection.textContent = '连接中';
    ui.connection.classList.remove('connected');
    socket.binaryType = 'arraybuffer';
    socket.onopen = () => {
      if (app.socket !== socket) return;
      app.reconnectDelay = 500;
      socket.send(JSON.stringify({
        type: 'config', language: ui.language.value, speaker: ui.speaker.value.trim() || '未知发言人',
        resume_from: app.lastAck >= 0 ? app.lastAck : null,
      }));
      // Wait for the server checkpoint before sending new or buffered audio.
    };
    socket.onmessage = event => {
      try {
        const message = JSON.parse(event.data);
        if (!handshakeComplete && message.type === 'ack' && Number.isSafeInteger(Number(message.data?.sample_offset))
            && Number(message.data.seq) < app.lastAck) {
          app.shouldReconnect = false;
          socket.close();
          reject(new Error('服务端音频记录比本地检查点更早，请使用新的会议编号。'));
          return;
        }
        handleEvent(message);
        if (!handshakeComplete && message.type === 'ack' && Number.isSafeInteger(Number(message.data?.sample_offset))) {
          handshakeComplete = true;
          app.socketReady = true;
          for (const [seq, frame] of app.pending) if (seq > app.lastAck) socket.send(frame.packet);
          ui.connection.textContent = '已连接';
          ui.connection.classList.add('connected');
          notice(app.pending.size ? `已恢复连接，正在补发 ${app.pending.size} 帧音频。` : '已连接，可开始发言。');
          resolve();
        }
      }
      catch (error) { console.warn('Invalid server event:', error); }
    };
    socket.onerror = () => { if (socket.readyState !== WebSocket.OPEN) reject(new Error('WebSocket 连接失败。')); };
    socket.onclose = () => {
      if (app.socket !== socket) return;
      app.socketReady = false;
      if (!handshakeComplete) reject(new Error('连接在配置完成前关闭。'));
      ui.connection.textContent = '已断开';
      ui.connection.classList.remove('connected');
      if (app.shouldReconnect && !app.ending) {
        clearTimeout(app.reconnectTimer);
        app.reconnectTimer = setTimeout(() => connect().catch(() => {}), app.reconnectDelay);
        app.reconnectDelay = Math.min(app.reconnectDelay * 2, 8000);
      }
    };
  });
  return app.connectPromise;
}

function sendPCM(buffer) {
  if (!app.recording || !(buffer instanceof ArrayBuffer)) return;
  if (app.pending.size >= 500) {
    notice('连接中断过久，收音已暂停以避免音频丢失。', true);
    stopMicrophone();
    return;
  }
  const pcm = new Uint8Array(buffer);
  const samples = pcm.byteLength / 2;
  const packet = new ArrayBuffer(12 + pcm.byteLength);
  const view = new DataView(packet);
  view.setUint32(0, app.nextSeq, true);
  view.setBigUint64(4, BigInt(app.nextOffset), true);
  new Uint8Array(packet, 12).set(pcm);
  app.pending.set(app.nextSeq, { packet, offsetEnd: app.nextOffset + samples });
  app.nextSeq++;
  app.nextOffset += samples;
  if (app.socketReady && app.socket?.readyState === WebSocket.OPEN) app.socket.send(packet);
}

async function startMicrophone() {
  if (app.recording) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    throw new Error('当前浏览器不支持 AudioWorklet 麦克风收音，请使用 HTTPS 或 localhost 打开。');
  }
  app.stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  try {
    app.audioContext = new AudioContext();
    await app.audioContext.audioWorklet.addModule('/static/pcm-worklet.js');
    app.source = app.audioContext.createMediaStreamSource(app.stream);
    app.worklet = new AudioWorkletNode(app.audioContext, 'pcm16-downsampler');
    const mute = app.audioContext.createGain();
    mute.gain.value = 0;
    app.worklet.port.onmessage = event => sendPCM(event.data);
    app.source.connect(app.worklet);
    app.worklet.connect(mute).connect(app.audioContext.destination);
    await app.audioContext.resume();
    app.recording = true;
    updateControls();
    notice('正在收音。普通发言会保存为字幕；句首说“小会”可向助手提问。');
  } catch (error) {
    app.stream.getTracks().forEach(track => track.stop());
    app.stream = null;
    throw error;
  }
}

async function stopMicrophone() {
  if (!app.recording) return;
  app.worklet?.port.postMessage('flush');
  await new Promise(resolve => setTimeout(resolve, 60));
  app.recording = false;
  app.source?.disconnect();
  app.worklet?.disconnect();
  app.stream?.getTracks().forEach(track => track.stop());
  await app.audioContext?.close();
  app.source = app.worklet = app.stream = app.audioContext = null;
  updateControls();
}

function clearEmpty(container) {
  container.querySelector('.empty')?.remove();
}

function timeLabel(timestampMs) {
  if (!Number.isFinite(Number(timestampMs))) return '';
  return new Date(Number(timestampMs)).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function renderUtterance(data) {
  const id = String(data.utterance_id ?? '');
  if (id && app.utterances.has(id)) return;
  if (id) app.utterances.add(id);
  clearEmpty(ui.transcript);
  const article = document.createElement('article');
  article.className = 'entry';
  const head = document.createElement('div');
  head.className = 'entry-head';
  const speaker = document.createElement('span');
  speaker.className = 'entry-speaker';
  speaker.textContent = String(data.speaker || '未知发言人');
  const time = document.createElement('time');
  time.textContent = timeLabel(data.timestamp_ms);
  const text = document.createElement('p');
  text.textContent = String(data.text || '');
  head.append(speaker, time);
  article.append(head, text);
  ui.transcript.append(article);
  ui.transcript.scrollTop = ui.transcript.scrollHeight;
  ui.partial.hidden = true;
}

function appendList(section, title, items) {
  const heading = document.createElement('h3');
  heading.textContent = title;
  section.append(heading);
  const list = document.createElement('ul');
  if (!items.length) {
    const li = document.createElement('li');
    li.textContent = '暂无';
    list.append(li);
  }
  for (const item of items) {
    const li = document.createElement('li');
    li.textContent = item;
    list.append(li);
  }
  section.append(list);
}

function renderMinutes(data) {
  ui.minutes.replaceChildren();
  const discussion = Array.isArray(data.discussion) ? data.discussion.map(String) : [];
  const decisions = Array.isArray(data.decisions) ? data.decisions.map(String) : [];
  const actions = Array.isArray(data.action_items) ? data.action_items.map(item => {
    const person = item.person ? `${item.person}：` : '';
    const deadline = item.deadline ? `（截止：${item.deadline}）` : '';
    return `${person}${item.action || ''}${deadline}`;
  }) : [];
  appendList(ui.minutes, '讨论要点', discussion);
  appendList(ui.minutes, '已确认的决定', decisions);
  appendList(ui.minutes, '待办事项', actions);
  ui.minutesPhase.textContent = data.phase === 'final' ? '最终纪要' : '阶段纪要';
}

function renderSources(snippets) {
  ui.sources.replaceChildren();
  if (!Array.isArray(snippets)) return;
  for (const snippet of snippets.slice(0, 6)) {
    const label = String(snippet.title || snippet.source || snippet.url || '来源');
    let url;
    try { url = new URL(snippet.url); } catch { /* Local source. */ }
    const element = document.createElement(url && ['http:', 'https:'].includes(url.protocol) ? 'a' : 'span');
    element.className = 'source';
    element.textContent = label;
    if (element.tagName === 'A') {
      element.href = url.href;
      element.target = '_blank';
      element.rel = 'noopener noreferrer';
    }
    ui.sources.append(element);
  }
}

function enqueueAudio(data) {
  if (!data.audio_b64) return;
  try {
    const binary = atob(data.audio_b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const blob = new Blob([bytes], { type: data.mime_type || 'audio/mpeg' });
    app.latestAudio = URL.createObjectURL(blob);
    app.audioQueue.push(app.latestAudio);
    ui.play.disabled = false;
    playQueuedAudio();
  } catch { notice('回答音频无法解码。', true); }
}

async function playQueuedAudio() {
  if (app.playing) return;
  app.playing = true;
  while (app.audioQueue.length) {
    const audio = new Audio(app.audioQueue.shift());
    try {
      await audio.play();
      await new Promise(resolve => { audio.onended = resolve; audio.onerror = resolve; });
    } catch {
      notice('浏览器限制了自动播放，请点击“播放最近回答”。');
      break;
    }
  }
  app.playing = false;
}

function handleEvent(event) {
  if (!event || event.meeting_id !== app.meetingId) return;
  const data = event.data || {};
  switch (event.type) {
    case 'ack': {
      const seq = Number(data.seq);
      if (!Number.isSafeInteger(seq) || seq < -1) break;
      if (Number.isSafeInteger(Number(data.sample_offset))) {
        const serverOffset = Number(data.sample_offset);
        if (seq < app.lastAck || serverOffset < 0) {
          notice('服务端音频进度与本地记录不一致，请检查会议编号。', true);
          break;
        }
        for (const pendingSeq of app.pending.keys()) {
          if (pendingSeq > seq) break;
          app.pending.delete(pendingSeq);
        }
        const first = app.pending.values().next().value;
        if (first && Number(new DataView(first.packet).getBigUint64(4, true)) !== serverOffset) {
          app.pending.clear();
          notice('未确认音频与服务端进度不一致，已按服务端位置继续收音。', true);
        }
        app.lastAck = seq;
        if (!app.pending.size) {
          app.nextSeq = seq + 1;
          app.nextOffset = serverOffset;
        }
        safeSet(storageKey(app.meetingId), JSON.stringify({ seq, offset: serverOffset }));
        break;
      }
      if (seq <= app.lastAck) break;
      let offset = null;
      for (const [pendingSeq, frame] of app.pending) {
        if (pendingSeq > seq) break;
        offset = frame.offsetEnd;
        app.pending.delete(pendingSeq);
      }
      if (offset !== null) {
        app.lastAck = seq;
        safeSet(storageKey(app.meetingId), JSON.stringify({ seq, offset }));
      }
      break;
    }
    case 'asr.partial':
      ui.partial.textContent = `${data.speaker || ''} ${data.text || ''}`.trim();
      ui.partial.hidden = !data.text;
      break;
    case 'asr.final': renderUtterance(data); break;
    case 'agent.state':
      ui.state.textContent = ({ LISTENING: '正在倾听', ANSWERING: '正在回答', ENDING: '正在整理', ENDED: '已结束' })[data.state] || data.state || '待命';
      if (data.state === 'ENDED') {
        app.active = false;
        app.ending = false;
        app.shouldReconnect = false;
        updateControls();
      }
      break;
    case 'search.started': ui.search.textContent = `正在搜索${data.source === 'web' ? '网页' : '本地资料'}…`; break;
    case 'search.result': ui.search.textContent = `已检索${data.source === 'web' ? '网页' : '本地资料'}`; renderSources(data.snippets); break;
    case 'llm.delta':
      if (app.answerRequest !== event.request_id) {
        app.answerRequest = event.request_id;
        ui.answer.textContent = '';
        ui.sources.replaceChildren();
      }
      ui.answer.textContent += data.delta || '';
      break;
    case 'llm.done':
      if (data.full_text) ui.answer.textContent = data.full_text;
      ui.search.textContent = '';
      break;
    case 'tts.audio': enqueueAudio(data); break;
    case 'minutes.ready': renderMinutes(data); break;
    case 'error': notice(`${data.message || '服务端发生错误'}${data.recoverable === false ? '，请重新开始会议。' : ''}`, true); break;
  }
}

ui.start.addEventListener('click', async () => {
  try {
    if (!app.active) initializeMeeting();
    app.shouldReconnect = true;
    await connect();
    app.active = true;
    await startMicrophone();
    updateControls();
  } catch (error) { notice(error.message || '无法启动收音。', true); }
});
ui.pause.addEventListener('click', async () => {
  await stopMicrophone();
  sendControl('flush');
  notice('收音已暂停，会议仍可继续。');
});
ui.flush.addEventListener('click', () => { if (sendControl('flush')) notice('正在整理当前语音片段…'); });
ui.minutesButton.addEventListener('click', () => { if (sendControl('partial_minutes')) notice('正在生成阶段纪要…'); });
ui.end.addEventListener('click', async () => {
  app.ending = true;
  updateControls();
  await stopMicrophone();
  if (sendControl('end_meeting')) notice('正在处理最后一段发言并生成最终纪要…');
  else { app.ending = false; updateControls(); }
});
ui.play.addEventListener('click', async () => {
  if (app.latestAudio) {
    app.audioQueue.push(app.latestAudio);
    await playQueuedAudio();
  }
});

ui.meeting.value = safeGet('xiaohui-last-meeting') || `meeting-${new Date().toISOString().slice(0, 10)}-${Math.random().toString(36).slice(2, 7)}`;
updateControls();
