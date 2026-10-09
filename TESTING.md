# 测试与验收

本文件记录 2026-10-09 在 macOS、Python 3.11 上执行的测试。`tests/results/` 保存真实接口回放的机器可读结果；`examples/minutes/` 保存由系统实际生成的纪要。测试录音是用 macOS 普通话与粤语系统声音生成的虚构会议，不含真实参会者资料。

## 准备与运行

按 [README](README.md#安装与启动) 安装依赖并在本机 `.env` 配置 `VOLC_API_KEY`、`VOLC_RESOURCE_ID` 和 `DEEPSEEK_API_KEY`。真实回放会消耗火山语音及语言模型服务的额度。启动一个独立的验收数据库：

~~~bash
DATA_DIR=data/acceptance DB_PATH=data/acceptance/meetings.db PORT=8000 ./scripts/start.sh
~~~

在另一个终端运行自动化测试：

~~~bash
.venv/bin/python -m pytest -q
node --test tests/test_client_audio.mjs
.venv/bin/python scripts/check_tts.py --language zh
.venv/bin/python scripts/check_tts.py --language yue
.venv/bin/python scripts/check_knowledge_followup.py --report data/acceptance/knowledge-followup.json
~~~

本次自动化结果：Python 测试 **39 项通过**，Node 客户端播放测试 **1 项通过**。Python 测试出现 1 条来自 Starlette `TestClient` 的依赖弃用警告，不影响本次断言。

仓库已附带四段短录音和一段 1800 秒 Ogg 录音；`ffprobe` 可核对时长。若需重新生成，运行 `.venv/bin/python scripts/generate_test_audio.py`。该命令依赖 macOS `say` 和 FFmpeg，会覆盖这些测试音频，因此不要在回放期间运行。所有录音先解码为 16 kHz、单声道、PCM16，按 200 ms 帧经项目 WebSocket 发送。每次回放使用新的 `--meeting-id`。

以下命令会调用真实 ASR、LLM 和 TTS，并将本次结果写入 Git 忽略的 `data/acceptance/`：

~~~bash
.venv/bin/python scripts/replay_meeting.py tests/fixtures/audio/mandarin.wav --meeting-id demo-zh-001 --language zh --expect-language zh --expect-answer --expect-answer-text 发布 --expect-source meeting --expect-transcript 决定 --partial-at 7 --report data/acceptance/mandarin.json
.venv/bin/python scripts/replay_meeting.py tests/fixtures/audio/cantonese.wav --meeting-id demo-yue-001 --language yue --expect-language yue --expect-answer --expect-answer-text 发布 --expect-source meeting --expect-transcript 决定 --partial-at 7 --report data/acceptance/cantonese.json
.venv/bin/python scripts/replay_meeting.py tests/fixtures/audio/knowledge.wav --meeting-id demo-knowledge-001 --language zh --expect-language zh --expect-answer --expect-answer-text 百万 --expect-source local --report data/acceptance/knowledge.json
.venv/bin/python scripts/replay_meeting.py tests/fixtures/audio/web.wav --meeting-id demo-web-001 --language zh --expect-language zh --expect-answer --expect-answer-text 机器学习 --expect-source web --report data/acceptance/web.json
.venv/bin/python scripts/replay_meeting.py tests/fixtures/audio/meeting-30m.ogg --meeting-id demo-long-001 --language auto --partial-at 900 --min-seconds 1800 --finish-timeout 900 --report data/acceptance/long.json
~~~

`replay_meeting.py` 校验音频帧全部确认、收到临时与最终字幕、最终纪要覆盖所有已保存发言、回答后回到监听状态，并在指定时检查语言、来源、回答事实和阶段纪要。报告中的 `passed: true` 表示这些检查均通过。`--minutes-example` 可把本次最终纪要导出为 JSON。

如果会后整理被中断，可用同一个会议编号恢复收尾；脚本会从服务端已保存的音频检查点继续，不重新上传完整录音：

~~~bash
.venv/bin/python scripts/finalize_meeting.py --meeting-id demo-long-001 --report data/acceptance/long-recovery.json --minutes-example data/acceptance/long-recovery-minutes.json
~~~

若改进纪要规则后要用已保存的完整转写重新生成最终纪要，运行：

~~~bash
.venv/bin/python scripts/rebuild_minutes.py --meeting-id demo-long-001 --db-path data/acceptance/meetings.db --data-dir data/acceptance --phase final --output data/acceptance/long-rebuilt.json --save --extractive
~~~

上游最终字幕去重可用真实接口单独复验。先从附带的长录音裁出 70 秒，再统计最终字幕及不同音频时间段的数量：

~~~bash
ffmpeg -hide_banner -loglevel error -y -i tests/fixtures/audio/meeting-30m.ogg -t 70 -ar 16000 -ac 1 -c:a pcm_s16le data/acceptance/asr-dedup.wav
.venv/bin/python -m src.volcengine_asr data/acceptance/asr-dedup.wav | .venv/bin/python -c 'import json,sys; events=[json.loads(line) for line in sys.stdin]; finals=[event for event in events if event["final"]]; print("finals=", len(finals), "unique_audio_times=", len({(event["start_ms"], event["end_ms"]) for event in finals}))'
~~~

## 实际结果

| 场景 | 实际结果 | 证据 |
| --- | --- | --- |
| 普通话、唤醒及会议上下文 | 17.03 秒音频，86/86 帧确认，26 条临时字幕、2 条最终字幕；普通决定句未唤醒，问句触发 1 次回答，回答引用“下周三发布蓝色方案”；状态回到 `LISTENING`；阶段与最终纪要均收到。 | [mandarin-live.json](tests/results/mandarin-live.json) |
| 最终代码普通话回归 | 同一段音频用最终代码重跑，86/86 帧、26 条临时字幕、3 条最终字幕，1 次回答及 MP3 输出，阶段与最终纪要均完成；纪要仅将“下周三发布”列为决定，将“先完成回归测试”列为待办，未把“什么时候发布”的追问当决定。 | [mandarin-current-live.json](tests/results/mandarin-current-live.json)、[纪要](examples/minutes/mandarin-current-final.json) |
| 粤语、唤醒及会议上下文 | 15.75 秒音频，79/79 帧确认，26 条临时字幕、3 条 `yue` 最终字幕；回答引用“下个礼拜三发布蓝色方案”；阶段与最终纪要均收到。 | [cantonese-live.json](tests/results/cantonese-live.json) |
| 本地知识库 | 问“DeepSeek-V4 支持多长上下文”，系统搜索本地论文摘要，回答“一百万 token”；完成 TTS 和最终纪要。论文摘要资料注明来源及其不是论文全文。 | [knowledge-live.json](tests/results/knowledge-live.json)、[资料](knowledge/05-DeepSeek-V4-论文摘要.md) |
| 论文追问与多轮上下文 | 保存上一问“支持多长上下文”后追问“哪些方式提升长上下文效率”；Agent 检索会议记录和本地摘要，回答提到 CSA 与 HCA，并附本地资料来源。 | [knowledge-followup-live.json](tests/results/knowledge-followup-live.json) |
| 网络搜索与并发收音 | 搜索 `search_web` 于回放第 6.66 秒开始、第 11.70 秒完成；另一句会议发言于第 9.85 秒成为最终字幕。回答引用百科来源，后续仍生成最终纪要。 | [web-live.json](tests/results/web-live.json) |
| 搜索失败后继续会议 | 注入网络超时；WebSocket 发出 `SEARCH_FAILED`，失败期间继续确认音频及保存下一句最终字幕，状态回到 `LISTENING`。 | `tests/test_protocol.py::test_failed_web_search_reports_error_while_audio_continues` |
| 阶段纪要失败后继续会议 | 注入阶段纪要异常；WebSocket 发出 `MINUTES_FAILED`，随后仍可完成最终转写和最终纪要。 | `tests/test_protocol.py::test_partial_minutes_failure_is_visible_and_meeting_can_finish` |
| 纪要后备提取 | 模型返回单条字符串时系统可规范成列表；模型输出空对象或不可用时，仍从“由测试组完成回归测试”“需要同步更新”提取待办，不把条件讨论误判为待办，也不会把追问“什么时候发布”误判为决定。阶段摘要漏掉待办时，最终纪要会从完整转写补回。 | `tests/test_minutes.py::test_minutes_accepts_safe_scalar_fields_from_model`、`tests/test_minutes.py::test_empty_model_minutes_fall_back_to_meeting_facts`、`tests/test_minutes.py::test_extractive_fallback_catches_explicit_assignments_and_needed_updates`、`tests/test_minutes.py::test_extractive_minutes_do_not_turn_wake_question_into_decision`、`tests/test_minutes.py::test_final_minutes_restore_action_omitted_by_phase_summary` |
| ASR 收尾失败恢复 | 注入首次 `finish()` 失败；系统保持可重试状态，第二次结束后保存最终字幕和纪要。 | `tests/test_volcengine_integration.py::test_failed_asr_finish_keeps_meeting_retryable` |
| 上游最终字幕去重 | 从长会议录音裁出前 70 秒，用真实 ASR 观察到同一音频时间段会再次返回去掉标点的最终文本；修正后得到 13 条最终字幕，对应 13 个不同起止时间。模拟测试还验证持久化层对同一结束位置只保存一次。 | [asr-dedup-live.json](tests/results/asr-dedup-live.json)、`tests/test_volcengine_asr.py::test_utterances_emit_new_finals_once_and_language`、`tests/test_volcengine_integration.py::test_corrected_final_at_same_audio_time_is_not_saved_twice` |
| 长会议中断后恢复 | 首次 30 分钟真实回放的旧版纪要合并超过 300 秒等待期限，音频和 524 条转写已保存；重启优化后的服务，用 `finalize_meeting.py` 在 5.95 秒内完成最终纪要，524 个源发言 ID 全部覆盖，状态转为 `ENDED`。纪要包含 12 条讨论要点、16 条决定和 20 条待办。 | [long-recovery.json](tests/results/long-recovery.json)、[纪要样例](examples/minutes/long-recovered-final.json) |
| TTS 输出与客户端播放调用 | `check_tts.py` 检查到普通话火山 MP3 时长 1.872 秒、粤语系统 Sinji MP3 时长 1.959 秒，均可由 FFprobe 解码。客户端收到 `tts.audio` 后调用浏览器 `Audio.play()`，失败时保留手动播放按钮。 | [tts-live.json](tests/results/tts-live.json)、`tests/test_client_audio.mjs` |
| 30 分钟连续会议 | 1800 秒音频按实时速率回放，耗时 1809.98 秒；9000/9000 帧确认，2628 条临时字幕、599 条最终字幕（普通话 477、粤语 122），尾段最终字幕在第 8997 帧，未唤醒时 0 次回答；第 15 分钟阶段纪要和会后最终纪要均收到。最终纪要覆盖全部 599 条发言，状态 `ENDED`，无错误。结束前服务端 RSS 约 79 MB。 | [long-live.json](tests/results/long-live.json)、[当时的最终纪要](examples/minutes/long-final.json) |
| 完整转写重建纪要 | 上述完整回放使用的是去重和待办补偿修正前已启动的服务进程。修正后用 `rebuild_minutes.py` 从同场 599 条转写重建纪要，得到 12 条讨论、3 条决定、3 条待办，其中“由测试组完成回归测试”保留负责人和“发布前”期限，599 个来源 ID 均保留。 | [重建后的纪要](examples/minutes/long-final-rebuilt.json) |

测试音频中的普通话声线为 Tingting 与 Eddy，粤语声线为 Sinji。当前火山接口对这组合成录音没有提供可用的说话人 ID，界面会显示“待确认发言人”；说话人姓名识别不属于本题必做项。合成录音验证协议、语言链路与长时运行，不代表真人多人会议的识别准确率。
