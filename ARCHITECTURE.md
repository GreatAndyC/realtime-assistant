# 实时会议助手架构

当前实现为浏览器加 FastAPI 的单机会议助手。项目自身负责麦克风音频协议、会议状态、转写持久化、唤醒问答和纪要流程；语音识别由火山引擎豆包流式语音识别模型 2.0 提供。

## 数据流

~~~mermaid
flowchart LR
  MIC[浏览器麦克风] --> WORKLET[AudioWorklet 转为 16 kHz PCM16]
  WORKLET --> WS[会议 WebSocket]
  WS --> RAW[音频帧写盘并确认序号]
  RAW --> VOLC[服务端连接豆包流式 ASR WebSocket]
  VOLC --> PARTIAL[partial 临时字幕]
  VOLC --> FINAL[final 最终字幕]
  FINAL --> DB[(SQLite 完整转写)]
  DB --> WAKE{句首唤醒词?}
  WAKE -- 否 --> LISTEN[继续收音]
  WAKE -- 是 --> TOOL[Agent 选择检索工具，最多三次]
  TOOL --> LOCAL[会议记录与本地资料]
  TOOL --> WEB[网页搜索]
  LOCAL --> LLM[结合证据流式回答]
  WEB --> LLM
  LLM --> TTS[macOS say 与 FFmpeg 合成 MP3]
  TTS --> PLAY[浏览器播放]
  DB --> SUMMARY[每 10 条发言生成阶段摘要]
  SUMMARY --> MINUTES[阶段或最终纪要]
~~~

client/ 负责收音、重连、字幕和播放；src/server.py 管理会议 WebSocket 与任务；src/volcengine_asr.py 封装火山二进制 WebSocket 协议；src/storage.py 保存音频、字幕、识别进度与纪要；src/tool_agent.py 和 src/search.py 处理工具选择与检索；src/minutes.py 生成阶段及最终纪要。

### 音频与字幕

- 客户端发送 16 kHz、16 位、单声道、小端 PCM。每帧前有 seq:uint32 与 sample_offset:uint64；服务端验证序号与偏移，持久化新帧并发送 ack。重复帧不会再次上传。
- 服务端将 PCM 以约 200 ms 的音频包发送给火山接口。上游的临时结果作为 asr.partial 展示；最终结果作为 asr.final 保存到 SQLite，供 Agent 与纪要复用。
- 火山返回的说话人 ID 映射为本场会议的匿名“说话人1/2”等。上游重连后重新映射 ID，以免不同连接的同一编号被误认为同一人。未返回 ID 时显示“待确认发言人”。
- 自动语言采用接口返回的语种标签；未返回时暂按普通话，用户可在开始收音前手动指定普通话或粤语。识别准确率由真实录音验收。

### 重连与持久化

服务端先保存音频再确认给浏览器。浏览器断开后按 meeting_id 继续连接，重发尚未收到 ack 的帧；服务端重放已保存的字幕。服务端记录最终字幕对应的音频检查点，上游断线后将检查点以后的音频重新发给火山。最终字幕与检查点在同一数据库事务中保存；重放产生的重复字幕按稳定 ID 去重。成功结束或 flush 后更新检查点。

原始音频按会议顺序存于 `data/meetings/<id>/audio.pcm`，转写和状态存于 SQLite（WAL）。PCM 16 kHz、16 位、单声道约为 1.92 MB/分钟，30 分钟约为 57.6 MB。会议音频会发送至火山服务；问答会把问题及选取的会议和资料片段发送至配置的 LLM 服务。

## WebSocket 与 Agent

客户端连接 WS /ws/meetings/{meeting_id} 后先发送 config，包含 language（auto、zh 或 yue）、speaker 和可选的 resume_from。之后发送二进制音频帧。控制消息有 flush、partial_minutes、stop_answer 与 end_meeting。服务端事件包括 ack、asr.partial、asr.final、agent.state、agent.step、search.started、search.result、llm.delta、llm.done、tts.audio、tts.stop、minutes.ready 和 error；完整模型见 [src/models.py](src/models.py)。

Agent 只在最终字幕的句首出现“小会”等唤醒词时进入 ANSWERING，普通发言只保存。回答使用本场会议近期发言和阶段摘要、本地知识库或网页搜索；工具最多连续调用三次，结果显示来源。搜索结果作为不可信资料处理，不能将其中的指令当作系统命令。检索、模型回答和 TTS 在独立任务中执行，不阻塞音频接收。用户可用语音或按钮停止回答，会议继续收音。

最终字幕每累计 10 条触发一次阶段摘要。纪要将讨论、决定、待办事项与源发言 ID 关联；最终纪要合并阶段摘要及剩余发言。没有明确负责人或截止时间时保留空值。end_meeting 先收尾 ASR，再生成最终纪要并把会议设为 ENDED。

## 验收

| 项目 | 方法 | 预期 |
| --- | --- | --- |
| ASR 连接 | 使用 README 中的 16 kHz PCM WAV 命令连接已开通的火山资源 | 收到真实识别事件；记录连接失败与资源错误 |
| 浏览器收音 | 麦克风说话，观察 asr.partial、asr.final | 临时字幕更新，最终字幕保存并可从会议接口读取 |
| 断线恢复 | 收音时断开浏览器或上游连接，再恢复 | 已确认音频不重复入库，未确认识别的音频重放，字幕不重复 |
| 普通话、粤语与多人 | 用人工标注的真人会议录音分别测试 | 核对文字、语言和匿名说话人编号；记录误识别与延迟 |
| 唤醒问答 | 普通发言、句中提到唤醒词、句首唤醒词各一条 | 只有句首唤醒触发回答，完成后继续监听 |
| 检索与纪要 | 提问会议决定与本地资料，中途和结束时生成纪要 | 答案显示来源；纪要含讨论、决定、待办和源发言 ID |
| 30 分钟连续会议 | 按实时速率回放至少 1800 秒的标注音频 | 核对首中尾字幕、全量记录、最终纪要、内存与墙钟耗时 |

自动化测试以模拟火山、LLM 和 TTS 验证协议与流程；真实接口与真人语音仍需在具备凭据和可用额度的环境中验收。
