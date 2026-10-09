import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import test from 'node:test';


test('TTS event asks the browser to play the decoded MP3', async () => {
  const elements = new Map();
  const createElement = () => ({
    textContent: '', value: '', disabled: false, hidden: false, children: [],
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; }, querySelector() { return null; },
  });
  const played = [];
  class FakeAudio {
    constructor(src) { this.src = src; }
    async play() {
      played.push(this.src);
      setImmediate(() => this.onended?.());
    }
    pause() {}
  }
  const context = {
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, createElement());
        return elements.get(id);
      },
      createElement,
    },
    localStorage: { getItem() { return null; }, setItem() {} },
    location: { protocol: 'http:', host: '127.0.0.1:8000' },
    URL: { createObjectURL() { return 'blob:answer'; }, revokeObjectURL() {} },
    Audio: FakeAudio,
    Blob, atob, setTimeout, clearTimeout, setImmediate, Uint8Array, DataView,
    ArrayBuffer, Date, Math, console,
  };
  const source = readFileSync(new URL('../client/app.js', import.meta.url), 'utf8');
  runInNewContext(`${source}\n;globalThis.testClient = { handleEvent, app };`, context);
  context.testClient.app.meetingId = 'playback-test';
  context.testClient.handleEvent({
    type: 'tts.audio', meeting_id: 'playback-test',
    data: { mime_type: 'audio/mpeg', audio_b64: Buffer.from('ID3audio').toString('base64') },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(played, ['blob:answer']);
  assert.equal(elements.get('play-button').disabled, false);
});
