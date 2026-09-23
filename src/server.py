"""Realtime meeting server and self-managed WebSocket session workflow."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
import uuid
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import config
from .models import AgentState, AudioSegment, ConfigMessage, ControlType, Language, Utterance, WSEventType
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
    return {"ok": True, "asr_configured": bool(config.whisper_model_path),
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
        from .asr import EnergyVAD, get_asr

        self.ws = ws
        self.meeting_id = meeting_id
        self.settings = settings
        self.vad = EnergyVAD()
        self.asr = get_asr()
        self.queue: asyncio.Queue[AudioSegment] = asyncio.Queue(maxsize=32)
        self.send_lock = asyncio.Lock()
        self.answer_task: asyncio.Task | None = None
        self.partial_task: asyncio.Task | None = None
        self.minutes_tasks: set[asyncio.Task] = set()
        self.worker = asyncio.create_task(self._asr_worker())
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
        for segment in self.vad.push(pcm, self.meeting_id, self.settings.speaker,
                                     self.settings.language, seq):
            await self.queue.put(segment)
        if self.vad.partial_due() and (self.partial_task is None or self.partial_task.done()):
            snapshot = self.vad.partial_snapshot()
            if snapshot is not None:
                self.partial_task = asyncio.create_task(self._partial(snapshot))

    async def _partial(self, snapshot: AudioSegment):
        try:
            text = await self.asr.transcribe(snapshot)
            if text.strip():
                await self.send(WSEventType.ASR_PARTIAL,
                                {"text": text.strip(), "speaker": snapshot.speaker,
                                 "seq": snapshot.seq_end})
        except Exception as exc:
            log.warning("Partial ASR failed: %s", exc)

    async def flush(self):
        for segment in self.vad.flush():
            await self.queue.put(segment)
        await self.queue.join()

    async def _asr_worker(self):
        while True:
            segment = await self.queue.get()
            try:
                text = (await self.asr.transcribe(segment)).strip()
                if text:
                    utterance = Utterance(
                        utterance_id=uuid.uuid4().hex, meeting_id=self.meeting_id,
                        speaker=segment.speaker, language=segment.language, text=text,
                        timestamp_ms=int(time.time() * 1000), seq=segment.seq_end,
                    )
                    storage.save_utterance(utterance)
                    await self.send(WSEventType.ASR_FINAL,
                                    {"utterance_id": utterance.utterance_id, "text": text,
                                     "speaker": utterance.speaker, "language": utterance.language.value,
                                     "seq": utterance.seq, "timestamp_ms": utterance.timestamp_ms},
                                    utterance_id=utterance.utterance_id)
                    self._maybe_start_answer(text)
                    self._maybe_checkpoint()
            except Exception as exc:
                log.exception("ASR segment failed")
                with suppress(Exception):
                    await self.error("ASR_FAILED", str(exc))
            finally:
                self.queue.task_done()

    def _maybe_start_answer(self, text: str):
        from .agent import extract_wake_query

        question = extract_wake_query(text)
        if question is None or self.ending:
            return
        if self.answer_task and not self.answer_task.done():
            asyncio.create_task(self.error("AGENT_BUSY", "Assistant is answering another question"))
            return
        self.answer_task = asyncio.create_task(self._answer(question))

    async def _answer(self, question: str):
        from .agent import build_messages, stream_answer
        from .search import needs_web_search, search_local, search_web
        from .tts import synthesize_speech

        request_id = uuid.uuid4().hex
        await self.set_state(AgentState.ANSWERING)
        try:
            await self.send(WSEventType.SEARCH_STARTED,
                            {"request_id": request_id, "source": "local"}, request_id=request_id)
            snippets = await asyncio.to_thread(search_local, question)
            local_snippets = snippets
            if needs_web_search(question, local_snippets):
                await self.send(WSEventType.SEARCH_STARTED,
                                {"request_id": request_id, "source": "web"}, request_id=request_id)
                try:
                    web_snippets = await asyncio.wait_for(search_web(question), config.web_search_timeout)
                    snippets = (local_snippets + web_snippets)[:config.max_search_snippets]
                except Exception as exc:
                    await self.error("WEB_SEARCH_FAILED", str(exc), request_id=request_id)
            snippets = snippets[:config.max_search_snippets]
            await self.send(WSEventType.SEARCH_RESULT,
                            {"request_id": request_id, "source": "web" if any(
                             s.get("source") == "web" for s in snippets) else "local",
                             "snippets": snippets}, request_id=request_id)
            recent = storage.list_utterances(self.meeting_id, config.max_recent_utterances)
            summaries = storage.list_phase_summaries(self.meeting_id)
            messages = build_messages(question, recent, summaries, snippets)
            full = ""
            async for delta in stream_answer(messages):
                full += delta
                await self.send(WSEventType.LLM_DELTA,
                                {"request_id": request_id, "delta": delta}, request_id=request_id)
            await self.send(WSEventType.LLM_DONE,
                            {"request_id": request_id, "full_text": full}, request_id=request_id)
            if full.strip():
                try:
                    audio = await synthesize_speech(full, self.settings.language)
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
        for task in (self.answer_task, self.partial_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if not self.worker.done():
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker


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
