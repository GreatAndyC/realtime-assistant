"""Realtime meeting server and self-managed WebSocket session workflow."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import time
import uuid
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import config
from .models import AgentState, ConfigMessage, ControlType, Language, Utterance, WSEventType
from .storage import Storage


log = logging.getLogger(__name__)
app = FastAPI(title="Realtime Meeting Assistant")
_client_dir = Path(__file__).resolve().parent.parent / "client"
app.mount("/client", StaticFiles(directory=_client_dir), name="client")
app.mount("/static", StaticFiles(directory=_client_dir), name="static")
storage = Storage()
_active: dict[str, "MeetingSession"] = {}


@app.get("/")
async def home():
    index = _client_dir / "index.html"
    if not index.exists():
        raise HTTPException(503, "Frontend is still being prepared")
    return FileResponse(index)


@app.get("/health")
async def health():
    return {"ok": True, "asr_provider": "volcengine", "asr_configured": bool(config.volc_api_key),
            "llm_configured": bool(config.deepseek_api_key)}


@app.get("/api/meetings/{meeting_id}")
async def meeting_detail(meeting_id: str):
    try:
        storage.validate_id(meeting_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    meeting = storage.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, "Meeting not found")
    return {"meeting": meeting, "utterances": storage.list_utterances(meeting_id),
            "minutes": storage.get_minutes(meeting_id)}


class MeetingSession:
    def __init__(self, ws: WebSocket, meeting_id: str, settings: ConfigMessage):
        if not config.volc_api_key:
            raise ValueError("VOLC_API_KEY is required for speech recognition")
        self.ws = ws
        self.meeting_id = meeting_id
        self.settings = settings
        prior_labels = (re.fullmatch(r"说话人(\d+)", item.speaker)
                        for item in storage.list_utterances(meeting_id))
        last_index = max((int(match.group(1)) for match in prior_labels if match), default=0)
        self.speaker_labels: dict[str, str] = {}
        self.next_speaker_index = last_index
        self.send_lock = asyncio.Lock()
        self.answer_task: asyncio.Task | None = None
        self.minutes_tasks: set[asyncio.Task] = set()
        self.volc = None
        self.volc_reader: asyncio.Task | None = None
        self.volc_finishing = False
        self.volc_error: Exception | None = None
        self.volc_pending = bytearray()
        self.volc_pending_start: int | None = None
        self.volc_session_start = 0
        self.volc_last_seq = storage.audio_position(meeting_id)[0]
        checkpoint = storage.cloud_asr_checkpoint(meeting_id)
        if checkpoint == 0 and storage.count_utterances(meeting_id):
            # Historical meetings recognized by the removed local backend have
            # no cloud checkpoint; do not transcribe their full history again.
            checkpoint = storage.audio_position(meeting_id)[1]
            storage.set_cloud_asr_checkpoint(meeting_id, checkpoint)
        self.volc_pending = bytearray(storage.read_audio_since(meeting_id, checkpoint))
        if self.volc_pending:
            self.volc_pending_start = checkpoint
        self.state = AgentState.LISTENING
        self.ending = False

    async def send(self, kind: WSEventType | str, data=None, *, utterance_id=None, request_id=None):
        payload = {"type": kind.value if isinstance(kind, WSEventType) else kind,
                   "meeting_id": self.meeting_id, "data": data}
        if utterance_id:
            payload["utterance_id"] = utterance_id
        if request_id:
            payload["request_id"] = request_id
        async with self.send_lock:
            await self.ws.send_json(payload)

    async def error(self, code: str, message: str, recoverable: bool = True, request_id=None):
        await self.send(WSEventType.ERROR,
                        {"code": code, "message": message, "recoverable": recoverable},
                        request_id=request_id)

    async def set_state(self, state: AgentState):
        self.state = state
        storage.set_state(self.meeting_id, state)
        await self.send(WSEventType.AGENT_STATE, {"state": state.value})

    async def audio(self, frame: bytes):
        if self.ending:
            await self.error("MEETING_ENDING", "Meeting is ending")
            return
        if len(frame) < 14 or len(frame) > 12 + 16000 * 2 * 2:
            await self.error("AUDIO_FRAME", "Audio frame has invalid length")
            return
        seq, offset = struct.unpack_from("<IQ", frame)
        pcm = frame[12:]
        if len(pcm) % 2:
            await self.error("AUDIO_FRAME", "PCM16 frame length must be even")
            return
        try:
            fresh = storage.append_audio(self.meeting_id, seq, offset, pcm)
        except ValueError as exc:
            await self.error("AUDIO_SEQUENCE", str(exc))
            return
        await self.send(WSEventType.ACK, {"seq": seq})
        if not fresh:
            return
        if self.volc_pending_start is None:
            self.volc_pending_start = offset
        self.volc_pending.extend(pcm)
        self.volc_last_seq = seq
        try:
            await self._send_volc(pcm)
        except Exception as exc:
            log.exception("Volcengine ASR send failed")
            await self.error("ASR_FAILED", f"火山语音连接失败：{exc}")

    async def flush(self):
        await self._flush_volc()

    async def _open_volc(self):
        """Open a provider session and replay audio since the last confirmed sentence."""
        from .volcengine_asr import VolcengineASRClient

        if self.volc_reader and not self.volc_reader.done():
            self.volc_reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.volc_reader
        if self.volc is not None:
            await self.volc.close()
        client = VolcengineASRClient(config.volc_api_key, config.volc_resource_id)
        await client.connect()
        self.volc = client
        self.volc_error = None
        self.volc_finishing = False
        self.volc_session_start = self.volc_pending_start or 0
        # Speaker IDs are scoped to one provider connection. Reusing the map
        # after reconnect could silently assign a new person an old label.
        self.speaker_labels.clear()
        self.volc_reader = asyncio.create_task(self._read_volc(client))
        # The adapter accepts 16 kHz PCM16 and internally sends 200 ms packets.
        replay = bytes(self.volc_pending)
        for start in range(0, len(replay), 6400):
            await client.send_pcm(replay[start:start + 6400])

    async def _send_volc(self, pcm: bytes):
        if self.volc is None or (self.volc_reader and self.volc_reader.done()):
            await self._open_volc()
            return
        try:
            await self.volc.send_pcm(pcm)
        except Exception:
            log.warning("Volcengine ASR connection dropped; reopening", exc_info=True)
            await self._open_volc()

    async def resume_asr(self):
        if self.volc_pending:
            try:
                await self._open_volc()
            except Exception as exc:
                log.warning("Could not resume persisted cloud audio: %s", exc)
                await self.error("ASR_FAILED", f"火山语音恢复失败：{exc}")

    async def _read_volc(self, client):
        try:
            async for event in client.events():
                await self._handle_volc_event(event)
            if not self.volc_finishing and self.volc is client:
                self.volc_error = ConnectionError("火山语音连接意外关闭")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.volc_error = exc
            log.warning("Volcengine ASR receiver stopped: %s", exc)
            with suppress(Exception):
                await self.error("ASR_FAILED", f"火山语音连接中断：{exc}")

    async def _handle_volc_event(self, event):
        text = event.text.strip()
        if not text:
            return
        raw_speaker = str(event.speaker_id) if event.speaker_id is not None else ""
        if raw_speaker:
            if raw_speaker not in self.speaker_labels:
                self.next_speaker_index += 1
                self.speaker_labels[raw_speaker] = f"说话人{self.next_speaker_index}"
            speaker = self.speaker_labels[raw_speaker]
        else:
            speaker = "待确认发言人"
        if not event.final:
            await self.send(WSEventType.ASR_PARTIAL,
                            {"text": text, "speaker": speaker, "seq": self.volc_last_seq})
            return
        if self.settings.language != Language.AUTO:
            language = self.settings.language
        else:
            language = Language.YUE if event.language == "yue" else Language.ZH
        end_sample = None
        if event.end_ms is not None and self.volc_pending_start is not None:
            _, stored_end = storage.audio_position(self.meeting_id)
            end_sample = min(stored_end,
                             self.volc_session_start + max(0, round(event.end_ms * 16)))
        frame_info = (storage.audio_frame_at(self.meeting_id, end_sample)
                      if end_sample is not None else None)
        seq, timestamp_ms = (frame_info if frame_info is not None
                             else (self.volc_last_seq, int(time.time() * 1000)))
        identity = (f"{end_sample}:{text}" if end_sample is not None
                    else f"{self.volc_session_start}:fallback:{text}")
        utterance = Utterance(
            utterance_id=uuid.uuid5(uuid.NAMESPACE_URL,
                                    f"{self.meeting_id}:volc:{identity}").hex,
            meeting_id=self.meeting_id,
            speaker=speaker, language=language, text=text,
            timestamp_ms=timestamp_ms, seq=seq,
        )
        inserted = storage.save_cloud_asr_utterance(utterance, end_sample)
        # Provider times are relative to this upstream session. Only discard PCM
        # through the finalized utterance; newer audio must remain replayable.
        if end_sample is not None and self.volc_pending_start is not None:
            consumed = max(0, min(len(self.volc_pending),
                                  (end_sample - self.volc_pending_start) * 2))
            consumed -= consumed % 2
            del self.volc_pending[:consumed]
            self.volc_pending_start += consumed // 2
        if not inserted:
            return
        with suppress(Exception):
            await self.send(WSEventType.ASR_FINAL,
                            {"utterance_id": utterance.utterance_id, "text": text,
                             "speaker": speaker, "language": language.value,
                             "seq": utterance.seq, "timestamp_ms": utterance.timestamp_ms},
                            utterance_id=utterance.utterance_id)
        await self._handle_utterance(text, language)
        self._maybe_checkpoint()

    async def _flush_volc(self):
        if self.volc is None and not self.volc_pending:
            return
        try:
            if self.volc is None or (self.volc_reader and self.volc_reader.done()):
                await self._open_volc()
            self.volc_finishing = True
            await self.volc.finish()
            if self.volc_reader:
                await asyncio.wait_for(self.volc_reader, timeout=20)
            if self.volc_error:
                raise self.volc_error
            storage.set_cloud_asr_checkpoint(
                self.meeting_id, storage.audio_position(self.meeting_id)[1]
            )
            self.volc_pending.clear()
            self.volc_pending_start = None
        except Exception as exc:
            log.exception("Volcengine ASR flush failed")
            with suppress(Exception):
                await self.error("ASR_FAILED", f"火山语音收尾失败：{exc}")
        finally:
            if self.volc_reader and not self.volc_reader.done():
                self.volc_reader.cancel()
                with suppress(asyncio.CancelledError):
                    await self.volc_reader
            if self.volc is not None:
                await self.volc.close()
            self.volc = None
            self.volc_reader = None
            self.volc_finishing = False

    async def _handle_utterance(self, text: str, language: Language):
        from .agent import extract_wake_query, is_stop_command

        if is_stop_command(text):
            await self.stop_answer()
            return
        question = extract_wake_query(text)
        if question is None or self.ending:
            return
        if self.answer_task and not self.answer_task.done():
            asyncio.create_task(self.error("AGENT_BUSY", "Assistant is answering another question"))
            return
        self.answer_task = asyncio.create_task(self._answer(question, language))

    async def stop_answer(self):
        """Cancel generation and tell the browser to stop any audio already playing."""
        if self.answer_task and not self.answer_task.done():
            self.answer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.answer_task
        await self.send(WSEventType.TTS_STOP, {})
        if not self.ending and self.state != AgentState.LISTENING:
            await self.set_state(AgentState.LISTENING)

    async def _answer(self, question: str, language: Language):
        from .agent import build_messages, stream_answer
        from .tool_agent import gather_agent_evidence
        from .tts import synthesize_speech

        request_id = uuid.uuid4().hex
        await self.set_state(AgentState.ANSWERING)
        try:
            async def emit_step(step):
                await self.send(WSEventType.AGENT_STEP, step, request_id=request_id)

            snippets = await gather_agent_evidence(question, storage, self.meeting_id, emit_step)
            await self.send(WSEventType.SEARCH_RESULT,
                            {"request_id": request_id, "source": "web" if any(
                             s.get("source") == "web" for s in snippets) else "local",
                             "snippets": snippets}, request_id=request_id)
            recent = storage.list_utterances(self.meeting_id, config.max_recent_utterances)
            summaries = storage.list_phase_summaries(self.meeting_id)
            messages = build_messages(question, recent, summaries, snippets, language=language)
            full = ""
            async for delta in stream_answer(messages):
                full += delta
                await self.send(WSEventType.LLM_DELTA,
                                {"request_id": request_id, "delta": delta}, request_id=request_id)
            await self.send(WSEventType.LLM_DONE,
                            {"request_id": request_id, "full_text": full}, request_id=request_id)
            if full.strip():
                try:
                    audio = await synthesize_speech(full, language)
                    await self.send(WSEventType.TTS_AUDIO,
                                    {"request_id": request_id,
                                     "audio_b64": base64.b64encode(audio).decode("ascii"),
                                     "mime_type": "audio/mpeg"}, request_id=request_id)
                except Exception as exc:
                    await self.error("TTS_FAILED", str(exc), request_id=request_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Agent answer failed")
            with suppress(Exception):
                await self.error("AGENT_FAILED", str(exc), request_id=request_id)
        finally:
            if not self.ending:
                with suppress(Exception):
                    await self.set_state(AgentState.LISTENING)

    def _maybe_checkpoint(self):
        from .minutes import MinutesService

        count = storage.count_utterances(self.meeting_id)
        if count and count % config.summary_trigger_count == 0:
            task = asyncio.create_task(MinutesService(storage).maybe_checkpoint(self.meeting_id))
            self.minutes_tasks.add(task)
            task.add_done_callback(self.minutes_tasks.discard)

    async def minutes(self, phase: str):
        from .minutes import MinutesService

        result = await MinutesService(storage).build(self.meeting_id, phase=phase)
        storage.save_minutes(self.meeting_id, result)
        await self.send(WSEventType.MINUTES_READY, result.model_dump(mode="json"))

    async def end(self):
        if self.ending:
            return
        self.ending = True
        await self.set_state(AgentState.ENDING)
        await self.flush()
        if self.answer_task and not self.answer_task.done():
            await self.answer_task
        if self.minutes_tasks:
            await asyncio.gather(*self.minutes_tasks, return_exceptions=True)
        try:
            await self.minutes("final")
        except Exception as exc:
            await self.error("MINUTES_FAILED", str(exc))
        await self.set_state(AgentState.ENDED)

    async def close(self):
        if self.answer_task and not self.answer_task.done():
            self.answer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.answer_task
        if self.volc_reader and not self.volc_reader.done():
            self.volc_reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.volc_reader
        if self.volc is not None:
            await self.volc.close()


@app.websocket("/ws/meetings/{meeting_id}")
async def meeting_socket(ws: WebSocket, meeting_id: str):
    await ws.accept()
    session = None
    try:
        storage.validate_id(meeting_id)
        if meeting_id in _active:
            await ws.send_json({"type": "error", "meeting_id": meeting_id,
                                "data": {"code": "ALREADY_CONNECTED", "message": "Meeting already connected"}})
            await ws.close(code=1008)
            return
        first = await ws.receive_json()
        settings = ConfigMessage.model_validate(first)
        if first.get("type") != ControlType.CONFIG.value:
            raise ValueError("First message must be config")
        if not config.volc_api_key:
            raise ValueError("VOLC_API_KEY is required for speech recognition")
        storage.create_or_resume(meeting_id, settings.language, settings.speaker[:100])
        session = MeetingSession(ws, meeting_id, settings)
        _active[meeting_id] = session
        await session.set_state(AgentState.LISTENING)
        last_seq, sample_offset = storage.audio_position(meeting_id)
        await session.send(WSEventType.ACK, {"seq": last_seq, "sample_offset": sample_offset})
        for utterance in storage.list_utterances(meeting_id):
            await session.send(WSEventType.ASR_FINAL,
                               {"utterance_id": utterance.utterance_id, "text": utterance.text,
                                "speaker": utterance.speaker, "language": utterance.language.value,
                                "seq": utterance.seq, "timestamp_ms": utterance.timestamp_ms},
                               utterance_id=utterance.utterance_id)
        await session.resume_asr()
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await session.audio(msg["bytes"])
                continue
            if msg.get("text") is None:
                continue
            try:
                control = json.loads(msg["text"])
                kind = control.get("type")
                if kind == ControlType.FLUSH.value:
                    await session.flush()
                elif kind == ControlType.PARTIAL_MINUTES.value:
                    task = asyncio.create_task(session.minutes("partial"))
                    session.minutes_tasks.add(task)
                    task.add_done_callback(session.minutes_tasks.discard)
                elif kind == ControlType.STOP_ANSWER.value:
                    await session.stop_answer()
                elif kind == ControlType.END_MEETING.value:
                    await session.end()
                    break
                else:
                    await session.error("CONTROL_UNKNOWN", "Unknown control message")
            except json.JSONDecodeError:
                await session.error("CONTROL_JSON", "Invalid JSON")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("WebSocket session failed")
        with suppress(Exception):
            if session:
                await session.error("SESSION_FAILED", str(exc), recoverable=False)
            else:
                await ws.send_json({"type": "error", "meeting_id": meeting_id,
                                    "data": {"code": "SESSION_FAILED", "message": str(exc)}})
    finally:
        if session:
            with suppress(Exception):
                await session.flush()
            await session.close()
            _active.pop(meeting_id, None)
        with suppress(Exception):
            await ws.close()
