"""
共享数据模型与事件类型 — 所有模块的接口合约。
所有模块均从此文件导入类型，禁止在模块内部重复定义。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class WSEventType(str, Enum):
    """WebSocket 事件类型（服务端 → 客户端）"""
    ASR_PARTIAL   = "asr.partial"
    ASR_FINAL     = "asr.final"
    AGENT_STATE   = "agent.state"
    AGENT_STEP    = "agent.step"
    SEARCH_STARTED = "search.started"
    SEARCH_RESULT  = "search.result"
    LLM_DELTA     = "llm.delta"
    LLM_DONE      = "llm.done"
    TTS_AUDIO     = "tts.audio"
    TTS_STOP      = "tts.stop"
    MINUTES_READY = "minutes.ready"
    ERROR         = "error"
    ACK           = "ack"


class ControlType(str, Enum):
    """客户端 → 服务端 JSON 控制消息类型"""
    CONFIG          = "config"
    FLUSH           = "flush"
    PARTIAL_MINUTES = "partial_minutes"
    STOP_ANSWER    = "stop_answer"
    END_MEETING     = "end_meeting"


class AgentState(str, Enum):
    LISTENING = "LISTENING"
    ANSWERING = "ANSWERING"
    ENDING    = "ENDING"
    ENDED     = "ENDED"


class Language(str, Enum):
    AUTO = "auto"  # 按每段发言自动识别
    ZH  = "zh"   # 普通话
    YUE = "yue"  # 粤语


# ---------------------------------------------------------------------------
# 客户端 → 服务端（JSON 控制消息）
# ---------------------------------------------------------------------------

class ConfigMessage(BaseModel):
    type: str = ControlType.CONFIG
    language: Language = Language.AUTO
    speaker: str = "未知发言人"
    resume_from: Optional[int] = None  # 上次确认的音频序号，用于断线续传


class FlushMessage(BaseModel):
    type: str = ControlType.FLUSH


class PartialMinutesMessage(BaseModel):
    type: str = ControlType.PARTIAL_MINUTES


class EndMeetingMessage(BaseModel):
    type: str = ControlType.END_MEETING


# ---------------------------------------------------------------------------
# 服务端 → 客户端（事件载荷）
# ---------------------------------------------------------------------------

class WSEvent(BaseModel):
    """通用事件信封"""
    type: WSEventType
    meeting_id: str
    utterance_id: Optional[str] = None
    request_id: Optional[str] = None
    data: Optional[Any] = None


class AsrPartialData(BaseModel):
    text: str
    speaker: str
    seq: int


class AsrFinalData(BaseModel):
    utterance_id: str
    text: str
    speaker: str
    language: Language
    seq: int
    timestamp_ms: int


class AgentStateData(BaseModel):
    state: AgentState


class SearchStartedData(BaseModel):
    request_id: str
    source: str  # "local" | "web"


class SearchResultData(BaseModel):
    request_id: str
    source: str  # "local" | "web"
    snippets: list[dict]


class LlmDeltaData(BaseModel):
    request_id: str
    delta: str


class LlmDoneData(BaseModel):
    request_id: str
    full_text: str


class TtsAudioData(BaseModel):
    request_id: str
    audio_b64: str   # base64 编码的 MP3
    mime_type: str = "audio/mpeg"


class ActionItem(BaseModel):
    person: Optional[str] = None
    action: str
    deadline: Optional[str] = None


class MinutesData(BaseModel):
    phase: str  # "partial" | "final"
    discussion: list[str]
    decisions: list[str]
    action_items: list[ActionItem]
    source_utterance_ids: list[str]


class AckData(BaseModel):
    seq: int


class ErrorData(BaseModel):
    code: str
    message: str
    recoverable: bool = True


# ---------------------------------------------------------------------------
# 数据库 / 内部模型
# ---------------------------------------------------------------------------

class Utterance(BaseModel):
    utterance_id: str
    meeting_id: str
    speaker: str
    language: Language
    text: str
    timestamp_ms: int
    seq: int


class PhaseSummary(BaseModel):
    summary_id: str
    meeting_id: str
    discussion: list[str]
    decisions: list[str]
    action_items: list[ActionItem]
    source_utterance_ids: list[str]
    created_at_ms: int


class Meeting(BaseModel):
    meeting_id: str
    language: Language
    speaker: str
    started_at_ms: int
    ended_at_ms: Optional[int] = None
    state: AgentState = AgentState.LISTENING
