# 小会 · 实时会议助手

在浏览器里收听会议、显示普通话或粤语字幕，并在句首唤醒“小会”时检索资料、回答问题、朗读结果；会议中和结束后均可生成纪要。

> 当前状态：可在 macOS 单机运行的原型。默认 Whisper 使用分段识别和周期快照；可切换到火山引擎豆包 2.0 实时流式识别。尚无 30 分钟会议的正式性能验收结果。

---

### 功能与题目要求

| 题目要求 | 当前实现 |
| --- | --- |
| 实时通信与会话状态由候选人实现 | 浏览器 `AudioWorklet` 发送 PCM16，服务端自行处理 WebSocket 音频序号、确认、会议状态与重连后的记录恢复。 |
| 普通话、粤语语音转写 | Whisper 模式逐段判断语言；火山模式读取接口返回的语种标签。最终字幕保存语言到 SQLite，唤醒回答和语音跟随该语言；识别不准时可手动指定。 |
| 唤醒式 Agent 问答 | 以“小会”等唤醒词开头后，DeepSeek 自行选择会议记录、本地资料或网页检索工具，并可依据前一步结果继续检索（最多三次）；页面显示每步工具调用，再流式返回回答及 MP3 语音。 |
| 长会议与结构化纪要 | 原始音频和完整转写落盘；每 10 条发言生成阶段摘要，可手动生成阶段纪要，结束时生成讨论、决定和待办事项。30 分钟持续运行仍需专项验证。 |

实时通信、会话调度、检索路由和纪要流程位于本仓库源码；Whisper、DeepSeek API、`ddgs` 与系统语音用于各自的模型或搜索能力。实现取舍和验收方案见 [架构说明](ARCHITECTURE.md)。

### 安装

需要 **macOS、Python 3.11、[uv](https://docs.astral.sh/uv/)、FFmpeg**，以及系统自带的 `say`。默认转写使用本地 GGML 格式的 Whisper 模型；当前机器使用 `ggml-large-v3-turbo-q5_0.bin`。其他格式的 `.pt` 权重不能直接用于 `WHISPER_MODEL_PATH`。使用火山云端 ASR 时无需下载 Whisper 模型。

在仓库根目录运行：

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
test -f .env || cp .env.example .env
```

若使用默认 Whisper，请在启动服务前下载约 547 MB 的模型；已有同名文件可跳过：

```bash
mkdir -p models
test -f models/ggml-large-v3-turbo-q5_0.bin || curl --fail --location --output models/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
```

配置完成后运行：

```bash
.venv/bin/uvicorn src.server:app --host 127.0.0.1 --port 8000 --env-file .env
```

按需在本机 `.env` 填入 `DEEPSEEK_API_KEY`；不要提交该文件。未配置密钥时仍可收音、转写和生成基于原文提取的纪要，但唤醒问答会返回配置错误。Whisper 模型路径无效时服务可以启动，转写时会报错。

#### 切换到火山引擎实时识别

在[豆包语音控制台](https://console.volcengine.com/speech/app)开通豆包流式语音识别模型 2.0，把 API Key **仅填入本机 `.env`**：

```dotenv
ASR_PROVIDER=volcengine
VOLC_API_KEY=在控制台获取的密钥
VOLC_RESOURCE_ID=volc.seedasr.sauc.duration
```

重启服务后，`GET /health` 的 `asr_provider` 应为 `volcengine` 且 `asr_configured` 为 `true`。浏览器的收音与字幕界面无需改变；后端把 16 kHz、单声道 PCM16 帧发往火山 WebSocket。可先用一段 **16 kHz、单声道、16 位 PCM WAV** 检查连接：

```bash
.venv/bin/python -m src.volcengine_asr /path/to/sample.wav
```

火山接口返回说话人编号时会自动显示“说话人1/2”；若无编号，则显示“待确认发言人”。普通话/粤语自动标签依赖接口返回的语种信息；接口未返回时暂按普通话处理，也可在页面手动选择粤语。火山官方将流式说话人分离注明为中英文能力，粤语与说话人编号同时使用的效果仍需真人会议录音验证。[流式接口](https://www.volcengine.com/docs/6561/1354869?lang=zh) · [说话人分离](https://docs.volcengine.com/docs/DoubaoVoice/speaker-separation?lang=zh)

### 使用

打开 <http://127.0.0.1:8000>，允许麦克风访问，输入会议编号后点击“开始收音”。默认自动判断每段发言是普通话还是粤语，无需事先选择；手动选项仅用于识别不准时。

1. 正常发言会进入字幕和完整会议记录。
2. 在一句话开头说“**小会，刚才决定了什么？**”可触发问答；助手会显示工具调用步骤、检索来源、逐步输出回答，并提供语音播放。
   回答过程中说“**小慧暂停**”（也识别“小会暂停”），或点击“停止回答”，会中断当前回答和音频播放，会议继续收音。
3. 点击“生成阶段纪要”可在会议中查看讨论、决定和待办；点击“结束并生成最终纪要”完成收音并保存最终结果。

`knowledge/` 内含四份标明 Mock 的虚构演示资料；也可放入 Markdown、TXT 或可提取文本的 PDF 供本地检索，扫描版 PDF 不支持 OCR。会议音频、转写数据库保存在 `data/`，两者均不纳入 Git。本地资料不足或问题涉及最新信息时会尝试网页搜索，结果取决于外部搜索服务。

### 配置

复制 [.env.example](.env.example) 后可设置：

| 变量 | 用途 |
| --- | --- |
| `WHISPER_MODEL_PATH` | 本地 GGML 模型路径；转写必需。 |
| `ASR_PROVIDER` | `whisper`（默认）或 `volcengine`。 |
| `VOLC_API_KEY`、`VOLC_RESOURCE_ID` | 火山识别密钥与豆包 2.0 资源 ID；仅火山模式需要。 |
| `DEEPSEEK_API_KEY` | 唤醒问答和模型生成纪要所需的 API 密钥。 |
| `LLM_BASE_URL`、`LLM_MODEL` | OpenAI 兼容接口地址与模型名。 |
| `DATA_DIR`、`DB_PATH`、`KNOWLEDGE_DIR` | 会议文件、SQLite 数据库和本地资料目录。 |

服务默认由上面的命令绑定在 `127.0.0.1:8000`。`GET /health` 可查看配置状态，`GET /api/meetings/{meeting_id}` 可读取会议、完整转写和最终纪要。WebSocket 路径为 `WS /ws/meetings/{meeting_id}`；消息格式和事件定义见 [src/models.py](src/models.py) 与 [src/server.py](src/server.py)。

### 验证与限制

```bash
.venv/bin/python -m pytest -q
```

自动化测试覆盖音频帧顺序、重复帧和从 WebSocket 转写到最终纪要的流程，其中 ASR、问答和语音合成使用模拟实现。现有本机合成语音试验覆盖普通话、粤语转写及一条完整的问答链路；这不代表真人语音识别质量或长会议稳定性。

自动语言判断会分别用普通话、粤语解码最终语音段并比较模型置信度，因此最终字幕会比单一语言解码更慢；短句、噪声或两种语言混说仍可能误判。本机用 `Tingting` 和 `Sinji` 合成的各一段语音实际检查，两段分别判为普通话和粤语；尚未用真人混合会议验证准确率。

- VAD 使用能量阈值，安静发言或嘈杂环境可能漏检；本地声纹模型按语音片段自动标记“说话人1/2”。短句会沿用上一标签，重叠发言或相似声线可能误分；重连后从新编号继续，无法把同一个人关联到旧编号。声纹向量只保存在当前会话内存，不写入数据库。
- Whisper 的 `partial` 是周期快照，`final` 在语音切段后给出；火山模式使用上游流式结果。云端模式需要网络、已开通的资源和可用额度，会议音频会发往所配置的火山服务。
- 网络搜索和模型问答依赖外部服务；LLM 调用会向配置的接口发送问题、相关会议片段及检索片段。
- 30 分钟连续会议、真实多人语音效果及延迟尚未完成可复现验收；具体步骤见[架构说明](ARCHITECTURE.md#5-验收设计实际运行后填写结果)。

### 文档

- [架构说明与验收设计](ARCHITECTURE.md)
- [测试用例](tests/test_protocol.py)

### 许可证

仓库目前没有附带开源许可证。
