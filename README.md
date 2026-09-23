# 小会 · 实时会议助手

浏览器收听会议，经服务端 WebSocket 接入火山引擎豆包流式语音识别 2.0，显示普通话或粤语字幕。在句首唤醒“小会”可检索资料、回答问题、朗读结果；会议中和结束后可生成结构化纪要。

目前是 macOS 单机原型。真实多人会议、30 分钟连续运行和粤语说话人区分效果仍需验证。

## 功能

| 能力 | 当前实现 |
| --- | --- |
| 实时收音与字幕 | 浏览器 AudioWorklet 产生 16 kHz、单声道 PCM16，通过项目自有 WebSocket 协议传给服务端；服务端转发至豆包流式 ASR，回传 partial 和 final。 |
| 断线恢复 | 服务端确认每帧音频并持久化；重连后恢复历史字幕，并将尚未确认识别的音频重放给火山接口。 |
| 自动说话人编号 | 使用火山接口返回的说话人 ID 显示“说话人1/2”等；接口未返回编号时显示“待确认发言人”。编号不代表真实姓名。 |
| 唤醒式 Agent 问答 | 句首出现“小会”等唤醒词时，DeepSeek 根据问题选择会议记录、本地资料或网页搜索工具，最多连续调用三次；页面显示工具步骤、流式回答和 MP3 音频。 |
| 会议纪要 | 完整转写保存至 SQLite；每 10 条最终字幕生成阶段摘要，可手动生成阶段纪要，结束时生成讨论、决定和待办事项。 |

实现细节与数据流见 [架构说明](ARCHITECTURE.md)。

## 安装与启动

需要 **macOS、Python 3.11、[uv](https://docs.astral.sh/uv/)、FFmpeg**，以及系统自带的 say（用于回答的语音合成）。语音识别还需要在[火山引擎豆包语音控制台](https://console.volcengine.com/speech/app)开通豆包流式语音识别模型 2.0，并取得 API Key。

在仓库根目录运行：

~~~bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
test -f .env || cp .env.example .env
~~~

仅在本机 `.env` 填入密钥，不要提交该文件：

~~~dotenv
VOLC_API_KEY=在控制台获取的密钥
VOLC_RESOURCE_ID=volc.seedasr.sauc.duration
DEEPSEEK_API_KEY=你的问答服务密钥
~~~

启动服务：

~~~bash
.venv/bin/uvicorn src.server:app --host 127.0.0.1 --port 8000 --env-file .env
~~~

打开 <http://127.0.0.1:8000>。`GET /health` 中的 `asr_configured` 应为 `true`。未配置火山密钥时，服务可以启动，但会议收音无法连接 ASR；未配置 DeepSeek 密钥时，语音转写仍可使用，唤醒问答会报配置错误，纪要使用基于原文的提取结果。

如需先检查火山接口连接，准备一段 **16 kHz、单声道、16 位 PCM WAV**，运行：

~~~bash
.venv/bin/python -m src.volcengine_asr /path/to/sample.wav
~~~

该命令使用本机 `.env` 中的火山配置，并打印流式识别事件。会议音频会发送到所配置的火山服务。

## 使用

打开页面、允许麦克风访问，输入会议编号并点击“开始收音”。默认采用接口返回的语种标签；未返回标签时暂按普通话处理。如果识别不准，可在开始前手动选择普通话或粤语。

1. 正常发言会进入字幕和完整会议记录。
2. 在一句话开头说“**小会，刚才决定了什么？**”可触发问答；页面显示工具调用、检索来源、逐步输出的回答和语音播放。回答过程中说“**小慧暂停**”（也识别“小会暂停”），或点击“停止回答”，可中断回答而继续收音。
3. 点击“生成阶段纪要”可在会议中查看讨论、决定和待办；点击“结束并生成最终纪要”完成收音并保存最终结果。

`knowledge/` 内含四份标明 Mock 的虚构演示资料；也可放入 Markdown、TXT 或可提取文本的 PDF 供本地检索，扫描版 PDF 不支持 OCR。会议音频和转写数据库保存在 `data/`，不纳入 Git。本地资料不足或问题涉及最新信息时会尝试网页搜索，结果取决于外部搜索服务。

## 配置与接口

复制 [.env.example](.env.example) 后可设置：

| 变量 | 用途 |
| --- | --- |
| VOLC_API_KEY | 豆包流式 ASR 的 API Key；收音转写必需。 |
| VOLC_RESOURCE_ID | 豆包流式 ASR 资源 ID，默认 volc.seedasr.sauc.duration。 |
| DEEPSEEK_API_KEY | 唤醒问答和模型生成纪要所需的 API Key。 |
| LLM_BASE_URL、LLM_MODEL | OpenAI 兼容接口地址与模型名。 |
| DATA_DIR、DB_PATH、KNOWLEDGE_DIR | 会议音频、SQLite 数据库和本地资料目录。 |

服务默认绑定 `127.0.0.1:8000`。`GET /health` 可查看配置状态，`GET /api/meetings/{meeting_id}` 可读取会议、完整转写和纪要。客户端使用 `WS /ws/meetings/{meeting_id}`；消息与事件类型见 [src/models.py](src/models.py) 和 [src/server.py](src/server.py)。

## 验证与限制

~~~bash
.venv/bin/python -m pytest -q
~~~

自动化测试通过模拟火山连接覆盖音频帧、重连、字幕到纪要流程，不等同于真实接口或真人语音质量验收。普通话/粤语自动标签依赖火山返回的信息；粤语与说话人编号同时使用的效果仍需真人会议录音验证。火山官方将流式说话人分离标注为中英文能力，见[流式接口](https://www.volcengine.com/docs/6561/1354869?lang=zh)与[说话人分离](https://docs.volcengine.com/docs/DoubaoVoice/speaker-separation?lang=zh)。

- 云端 ASR 需要网络、已开通的资源和可用额度；接口中断时会尝试重连并重放尚未确认识别的音频。说话人 ID 只在单次上游连接内有效，重连后编号可能变化。
- 网络搜索与模型问答依赖外部服务；LLM 请求会发送问题、相关会议片段和检索片段到配置的接口。
- 30 分钟连续会议、真实多人语音效果及延迟尚未完成可复现验收；测试方案见[架构说明](ARCHITECTURE.md#验收)。

## 许可证

仓库目前没有附带开源许可证。
