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
        default_factory=lambda: os.environ.get("LLM_MODEL", "deepseek-flash")
    )
    llm_timeout: float = 30.0

    # ------------------------------------------------------------------
    # 火山引擎豆包实时流式 ASR
    # ------------------------------------------------------------------
    volc_api_key: str = field(
        default_factory=lambda: os.environ.get("VOLC_API_KEY", "")
    )
    volc_resource_id: str = field(
        default_factory=lambda: os.environ.get("VOLC_RESOURCE_ID", "volc.seedasr.sauc.duration")
    )

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
        default_factory=lambda: [
            "小会", "小會", "小慧", "小惠", "小汇", "小匯",
            "喂小会", "喂小會", "喂小慧", "喂小惠", "会议助手", "會議助手",
        ]
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
    host: str = field(default_factory=lambda: os.environ.get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "8000")))
    log_level: str = "info"


# 全局单例
config = Config()
