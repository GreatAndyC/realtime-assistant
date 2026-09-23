"""
全局配置。从环境变量读取，提供合理默认值。
使用方式：from src.config import config
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # ------------------------------------------------------------------
    # LLM（DeepSeek，OpenAI 兼容）
    # ------------------------------------------------------------------
    deepseek_api_key: str = field(
        default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", "")
    )
    llm_base_url: str = field(
        default_factory=lambda: os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
    )
    llm_model: str = field(
        default_factory=lambda: os.environ.get("LLM_MODEL", "deepseek-chat")
    )
    llm_timeout: float = 30.0

    # ------------------------------------------------------------------
    # ASR（pywhispercpp，指向本机 GGML 权重）
    # ------------------------------------------------------------------
    whisper_model_path: str = field(
        default_factory=lambda: os.environ.get("WHISPER_MODEL_PATH", "")
    )
    whisper_language: str = "zh"   # 默认普通话；粤语传 "yue"

    # ------------------------------------------------------------------
    # VAD 参数
    # ------------------------------------------------------------------
    vad_energy_threshold: float = 0.01    # RMS 能量阈值（相对于满幅）
    vad_silence_ms: int = 500             # 静音多久视为切段
    vad_max_segment_ms: int = 28000       # 单段最长（Whisper 30s 窗口留余量）
    vad_min_segment_ms: int = 300         # 太短的片段丢弃

    # ------------------------------------------------------------------
    # 音频格式（固定，前端与服务端必须一致）
    # ------------------------------------------------------------------
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2  # 16-bit PCM

    # ------------------------------------------------------------------
    # 存储路径
    # ------------------------------------------------------------------
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("DATA_DIR", "data"))
    )
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("DB_PATH", "data/meetings.db"))
    )
    knowledge_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("KNOWLEDGE_DIR", "knowledge"))
    )

    # ------------------------------------------------------------------
    # 唤醒词（句首匹配）
    # ------------------------------------------------------------------
    wake_words: list[str] = field(
        default_factory=lambda: ["小会", "小會", "喂小会", "喂小會", "会议助手", "會議助手"]
    )

    # ------------------------------------------------------------------
    # Agent 上下文限制
    # ------------------------------------------------------------------
    max_recent_utterances: int = 20   # 问答上下文最多取最近 N 条
    max_search_snippets: int = 4      # 检索结果最多 N 条
    summary_trigger_count: int = 10   # 每 N 条 final 字幕触发阶段摘要

    # ------------------------------------------------------------------
    # 网络搜索
    # ------------------------------------------------------------------
    web_search_timeout: float = 8.0   # ddgs 超时秒数
    web_search_max_results: int = 5

    # ------------------------------------------------------------------
    # 服务
    # ------------------------------------------------------------------
    host: str = field(default_factory=lambda: os.environ.get("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "8000")))
    log_level: str = "info"


# 全局单例
config = Config()
