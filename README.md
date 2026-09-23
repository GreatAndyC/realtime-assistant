# 小会：实时会议助手

单机版会议助手。浏览器持续发送 16 kHz、单声道 PCM16；服务端用 WebSocket 接收音频、切分语音、转写并保存完整记录。句首说“小会，……”会触发资料检索、流式回答和语音播放。会议期间可生成阶段纪要，结束时生成最终纪要。

## 运行

需要 Python 3.11、`uv`、本机可用的 `say` 与 FFmpeg，以及 **GGML 格式**的 Whisper 模型文件。Buzz 缓存中的 `.pt` 权重不能直接填入 `WHISPER_MODEL_PATH`。

本工作区已下载并验证 `models/ggml-large-v3-turbo-q5_0.bin`。新机器可用以下命令从 whisper.cpp 模型仓库下载约 547 MB 的量化模型：

```bash
mkdir -p models
curl --fail --location --output models/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
```

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
test -f .env || cp .env.example .env
# 模型路径已填入示例；如需问答，在本地 .env 填入 DEEPSEEK_API_KEY
.venv/bin/uvicorn src.server:app --host 127.0.0.1 --port 8000 --env-file .env
```

打开 <http://127.0.0.1:8000>，输入会议编号，选择普通话或粤语，再点击“开始收音”。浏览器需允许麦克风访问。可把 Markdown、TXT 或可提取文本的 PDF 放进 `knowledge/`，供本地检索。音频和转写保存在 `data/`；`.env`、虚拟环境和会议数据已排除在 Git 之外。

无 `DEEPSEEK_API_KEY` 时，收音与转写仍可用，唤醒问答会返回配置错误，纪要使用保守的原文提取。无有效 `WHISPER_MODEL_PATH` 时，服务端仍可启动，但识别会返回错误，不会生成真实字幕。

## 接口

- `WS /ws/meetings/{meeting_id}`：连接后先发 `{"type":"config","language":"zh","speaker":"我"}`。音频帧为小端 `seq:uint32`、`sample_offset:uint64` 和 PCM16。服务端以 `ack` 确认序号；重发已确认帧不会重复写盘。
- 控制消息：`flush`、`partial_minutes`、`end_meeting`。服务端返回 `asr.partial`、`asr.final`、`agent.state`、`search.started`、`search.result`、`llm.delta`、`llm.done`、`tts.audio`、`minutes.ready`、`error`。
- `GET /api/meetings/{meeting_id}`：读取会议、完整转写和最终纪要。`GET /health`：检查服务状态及模型配置是否存在。

## 验证与限制

运行 `.venv/bin/python -m pytest -q` 可验证音频帧顺序、重复帧、WebSocket 转写到最终纪要的流程；自动化测试使用模拟 ASR。本机另以合成普通话和粤语分别完成真实 Whisper 转写，并以两段合成普通话跑通 WebSocket 收音、转写、入库、句首唤醒、联网检索、真实 DeepSeek 流式回答、MP3 播放事件和最终纪要；这不代表真人语音识别质量。当前 VAD 是能量阈值法，安静发言或嘈杂环境可能漏检。Whisper 字幕是分段和周期快照，不是原生逐帧流式解码。PDF 不做扫描件 OCR，网页搜索依赖外部搜索服务。

架构取舍和验收方案见 [ARCHITECTURE.md](ARCHITECTURE.md)。
