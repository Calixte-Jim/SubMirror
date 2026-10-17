# 变更记录（CHANGELOG）

本文件是 SubMirror 的**书面变更记录**：每次交付都记录交付范围、修复的缺陷、依赖变更、已知限制与延期项。
迭代待办见 [ITERATION_BACKLOG.md](ITERATION_BACKLOG.md)，验收口径见 [ACCEPTANCE.md](ACCEPTANCE.md)。

记录格式：`## [版本] - 日期 — 标题`，下分 交付范围 / 修复 / 变更 / 依赖 / 已知限制 / 延期项。

---

<!-- 0.1.19-artifacts -->
## [0.1.19] - 2026-10-21 — 三种工作模式 + 采集来源与混音 + 降噪/爆破音过滤 + 队列背压

### 新增

- **三种工作模式**（`src/core/modes.py`，设置 → 常规「工作模式」下拉）：**全能**（`allround`，默认）、
  **游戏**（`game`）、**对话**（`conversation`）；下拉切换即把该档推荐参数写入配置并立即生效
  （不必等「保存」），单行数值仍可手改。`app.mode_auto_params`（默认开）决定启动时是否重新套用推荐值。
  - 游戏：`asr.vad_threshold_db` −45 → −35 dB、`asr.vad_min_speech_ms` 300 → 450 ms、
    `audio.vad_chunk_seconds` 2.0 → 2.5 s（上限 5.0 s）、`audio.vad_engine` → `energy`、
    `audio.burst_filter` 开（`burst_max_ms` 60 / `burst_margin_db` 10）、`audio.feed_when_silent` 关、
    `asr.preview_translate` 关、`perf.asr_queue_max` 与 `perf.translate_queue_max` 各 2、
    `perf.overlay_max_fps` 15、`ocr.area_fps_normal` 1.0、`ocr.fullscreen_min_interval_ms` 1500。
  - 对话：`audio.source` → `mixed`、`audio.mic_gain` 1.0 / `audio.speaker_gain` 0.35、
    `audio.noise_suppression` 开（`noise_reduction_db` 12）、`asr.vad_threshold_db` −48 dB、
    `audio.vad_hangover_ms` 400 ms、`asr.beam_size` 5。
- **采集来源与混音**（`src/audio/capture_win.py`、`src/audio/loopback.py`）：`audio.source` =
  `speaker` / `microphone` / `mixed`；`audio.mic_device` 指定输入设备（序号或名称片段，留空跟随系统默认）；
  混音按 `audio.mic_gain` / `audio.speaker_gain`（0–4）加权相加，某一路超过 `audio.mix_grace_ms`
  （默认 400 ms）无新音频即自动旁路。`LoopbackCapture` 增加 `kind`（loopback / microphone）、
  `list_input_devices()`、`default_input_device()` 与 `set_block_sink()`。
- **本机 DSP**（`src/audio/dsp.py`）：`audio.noise_suppression`（一阶高通 + 自适应噪声门，强度
  `audio.noise_reduction_db` 默认 12 dB）与 `audio.burst_filter`（把「高于运行中位数 `burst_margin_db`、
  短于 `burst_max_ms`、且被块内更静音频包夹」的片段静音，用于枪声等短促爆破音）。两级只作用于麦克风/混合来源。
- **队列背压**：`perf.pause_capture_on_full`（默认开）——ASR 队列积压到 `perf.asr_queue_max` 时调用
  `pause_capture()` 暂停采集（字幕提示「队列已满，已暂停采集」），队列腾空后自动恢复；
  `AsrService._install_queue_probe()` 把 `TaskScheduler.pending_count()` 接到采集侧。

### 变更

- 版本号：`src/core/config.py` 的 `APP_VERSION` 与 `build_installer.nsi` 的 `APP_VERSION` 同步为 **0.1.19**。
- 默认值：`perf.asr_queue_max` 4 → **3**、`perf.translate_queue_max` 4 → **3**、
  `translate.task_timeout_ms` 700 → **600 ms**、`perf.overlay_min_interval_ms` 400 → **500 ms**。
- 面板键 60 → **72**（工作模式下拉、模式说明摘要、自动套用开关、采集来源与混音等 12 个控件）。
- 语言表新增 32 键 → **zh-CN / en 各 264 键**（集合完全一致）。
- 帧率与刷新语义不变：`perf.overlay_min_interval_ms` 仍是**内容重绘**地板，动画 tick（33 ms / 全屏 67 ms）
  不受影响；渲染始终在 GUI 线程，识别/翻译/网络始终在 worker 线程。

### 验证

- 新增无头验收套件 `_v0119_flow_check.py`（45 项：三模式表与预设、模式切换的面板联动、采集配置与混音、
  降噪/爆破音 DSP、背压暂停与恢复、泳道上限与 600 ms 截止、`main.py` 接线、刷新地板，以及冻结/安装包/文档守门）。
- 既有套件随本轮重基线：`_bl5_tabs_check.py` 面板键 60 → **72**、`submirror_runtime_accept.py` 采集键 51 → **72**、
  `_v0117_perf_check.py` 队列与覆盖键集、`_v0118_flow_check.py` 的 SCOPE 扩展（新音频模块）。
- 结果见 `submirror_verify_log.md`（第五~十七轮）。历史轮次在 `_v018_external_check.py` 段的两项失败是公网依赖
  （MyMemory 超时 / LibreTranslate 域名解析），与本轮无关。

### 产物

- 便携包 / 安装包尺寸与 SHA-256 见 `submirror_verify_log.md` 的「当前开发版产物 (0.1.19 …)」段。

### 已知限制

- 「对话模式」的混合采集需要系统存在可用的默认输入设备；没有麦克风时该路回退为静音占位源（不影响扬声器字幕）。
- 降噪与爆破音过滤是**本机时域**处理（非神经网络降噪），对强混响与持续机械噪声效果有限。
- 模式预设只覆盖它列出的键；其余键始终保持配置文件的取值。

<!-- 0.1.18-artifacts -->
## [0.1.18] - 2026-10-21 — 切片按句翻译 + 翻译上下文 + 过载自适应 + 开机自启

**交付范围**：一轮六项流水线优化 + NSIS/自启四项。ASR 切片从「固定 6 s + 200 ms 重叠」改为
「2.0 s 目标切片（上限 4.0 s，超时/积压自适应）+ 400 ms 尾音重叠 + 静音切片丢弃」；识别结果按
`.!?` 切句，每句一条字幕行与一个翻译任务；翻译单并发、**700 ms 单任务截止**、附带最近 2 句上下文
（仅对声明 `supports_prompt` 的后端）；字幕**内容重绘地板 400 ms**；超时/过载给出友好提示并自动拉长切片。
`src/screen/*`、`src/core/subtitle_queue.py` 与识别算法本身（Whisper / Silero / energy 判定）**零改动**。

**新增**

- **切片与静音**（`src/audio/vad.py`）：`audio.vad_chunk_seconds`（默认 `2.0`，1.0–10.0 s）与
  `audio.vad_chunk_max_seconds`（默认 `4.0`）取代定长 6 s 强制切分；`stretch_chunk()` / `relax_chunk()`
  在持续超时（+0.5 s）与恢复（−0.25 s）时调整切片长度；`SpeechSegment.silent` 标记静音切片，消费端整段丢弃。
- **按句切分与语气词**（`src/ai/text_processor.py`）：新增 `split_sentences()`、`TextProcessor.sentences()` 与
  `strip_fillers()`；`asr.sentence_split`（默认开）、`asr.max_sentences`（默认 4）、`text.filler_filter`（默认开）、
  `text.filler_words`（12 条默认表）、`text.max_sentence_chars`（默认 200）；纯语气词切片以新原因
  `filler-only` 拒绝。词边界匹配保证 `ha` 不误伤 `hard`。
- **语速自适应解码**（`main.py` AsrEngine）：`asr.adaptive_beam`（默认开）、`asr.fast_speech_wps`（默认 3.0）、
  `asr.fast_beam_size`（默认 1）；滚动统计词/秒，语速过快时把束宽降到 `fast_beam_size`，`stats()` 增加
  `beam_used` / `speech_wps` / `greedy_passes`。
- **翻译任务截止与上下文**（`src/ai/base_translator.py`）：`translate.task_timeout_ms`（默认 700）为**单任务总截止**，
  与单次尝试下限（150 ms）取小；`translate.context_sentences`（默认 2）与 `translate.context_max_chars`
  （默认 240）提供滚动上下文，**只传给声明 `supports_prompt` 的后端**；本地调用上下文按调用隔离
  （`call_context()` 读线程局部值）。
- **调度可观测与单并发**（`src/core/task_scheduler.py`）：队列满丢最旧时写
  `lane … queue full (max …): dropped the oldest task #…` 警告，拒绝新任务时写 `dropped the new task #…`；
  新增 `pending_count(lane)` 供上游过载反馈；`perf.translate_workers`（默认 1）让翻译泳道单并发。
- **字幕内容刷新地板**（`src/ui/overlay.py`）：新增 `perf.overlay_min_interval_ms`（默认 400）与
  `repaint_interval_ms()`；`from_config()` / `fade_stats()` 一并暴露该值。
- **开机自启**（`src/core/silent_launch.py` + `src/ui/settings_ui.py`）：新增 `app.autostart`（默认**关闭**）、
  `app_autostart_enabled()`、`build_app_only_script()` 与 `register(..., app_only=True)`；设置「高级」页新增
  【开机自启】勾选；与 LibreTranslate 自启**共用同一个**启动项文件（app 自启优先），未配置（或无效）
  LibreTranslate 目录时只静默启动本体，且不再因无效目录弹警告（改为提示 `settings.libre_dir_optional`）。

**变更**

- 版本号：`src/core/config.py` 的 `APP_VERSION` 与 `build_installer.nsi` 的 `APP_VERSION` 同步为 **0.1.18**。
- 设置面板键集合 **59 → 60**（新增 `app.autostart`）。
- 默认值：`audio.vad_overlap_ms` 200 → **400**；`perf.asr_queue_max` 1 → **4**；`perf.translate_queue_max` 8 → **4**。
- 双语各新增 7 条文案键（`settings.group.application`、`settings.autostart`、`settings.autostart.tip`、
  `settings.libre_dir_optional`、`overlay.status.translate_timeout`、`overlay.status.translate_failed`、
  `overlay.status.busy`），中英各 **231** 条。
- `frame_interval_ms()` **有意不改**：400 ms 地板只加在**内容重绘**（`repaint_interval_ms()`）上，
  否则 180 ms 淡入会被量化成 1 帧、且会破坏 0.1.17 的 33/67 ms 期望。动画 tick 仍为 33 ms（全屏 67 ms）。
- 翻译超时/服务失败/音频积压分别显示「翻译超时」「翻译服务繁忙」「音频积压，已跳过一段」，字幕不空白。
  提示行**刻意不标记 `failed`**：`SubtitleItem.display_text()` 对失败项回退原文，会把提示藏起来；原文仍保留在
  `item.original`，`translate.show_original` 打开时原文与提示两行都显示。

**依赖**：无新增、无升级（仍为 3 个出站域名 + 可选自建 LLM 端点；无新第三方模块）。

**验证**（本机无头）

- 新增套件 `_v0118_flow_check.py`：**39 项**，断言 2.0 s/0.4 s 切片与自适应（2.00 → 2.50 → 4.00 → 2.00）、
  静音字段、按句切分（3 段 / 超长 8 段）、语气词（移除 2 条且 `that was hard` 保留）、队列上限 4 丢最旧且
  运行中任务不被误杀、**700 ms 截止**（超时路径实测在 ~950 ms 内被截止）、上下文 240 字上限与 `0` 关闭、
  语速降束宽（1.5 wps → 束宽 1）、60 面板键与自启脚本（1685 B，无 HTTP 等待/超时弹窗）、
  冻结文件与安装脚本四项断言。
- 既有套件（静态审计、`_v0116_tooltip_check.py`、`_v0117_style_check.py`、`_v0117_perf_check.py`、py_compile）
  全部重跑，逐段结果见 `submirror_verify_log.md`。
- 0.1.16 套件按新面板键数（59 → 60）与新版号下限（>= 0.1.18）同步更新；0.1.17 两套件补充 `--freeze`
  参数与「冻结前序套件存在」检查。
- 随本轮改动的重基线（均已写盘）：`_bl5_tabs_check.py` 的 `BASELINE_KEYS` 51 → 60（补 9 个 0.1.16–0.1.18
  新行）、`submirror_runtime_accept.py` 的 collect() 键数 51 → 60、`_v0117_perf_check.py` 的
  `perf.asr_queue_max` 1 → 4 与 `perf.translate_workers`、强制切分断言改为 `chunk_limit_seconds`、
  energy 片段测试把切片目标抬到 12 s 以免 2 s 新默认盖过引擎上限、`_v0114_asr_stream_check.py` 的
  失败翻译路径按 0.1.18 契约（显示本地化提示、`failed=False`、原文保留在 `item.original`）更新、
  `_v017_startup_check.py` 显式恢复 INFO 日志级别（0.1.17 起默认静默会吞掉启动公告）。
- 0.1.10–0.1.16 套件的「冻结核心文件未被改动」检查以 mtime 与前序套件为基准；本轮合法改动了
  `src/ai/base_translator.py`、`src/ai/text_processor.py`、`src/audio/vad.py`，故按链式约定把冻结基准
  前移到本轮快照（该检查在每轮只证明「自上一轮以来未再改动」，历史轮次的结论不变）。
- `_v018_external_check.py`（真实公网端点）在离网环境下 2 项失败（MyMemory 超时、LibreTranslate DNS
  解析失败），属环境依赖，非本轮回归。

**产物**（本机实测）

- `dist\SubMirror-0.1.18-portable.zip` = 283,432,211 B / 1035 entry /
  SHA-256 `B737542C89A8E7621E3716939A668BCAA6DEBADE600C1782CFB2D1CDEB02468F`
- `dist\SubMirror-portable\SubMirror.exe` = 10,376,654 B /
  SHA-256 `114856CC2B97FD0808E015C538355B7BFB744759DAE2D5801CE1110CE2245CFB`
- `dist\SubMirror-0.1.18-setup.exe` = 96,192,124 B /
  SHA-256 `B7E78E7B34340528BACC498F129C367FCE04FAC024F989948AE15A417E9ABC6D`

同样的尺寸与哈希已由 `_v0118_fill_log.py` 填入 `submirror_verify_log.md` 的
「当前开发版产物 (0.1.18 …)」段（该段不留任何占位符）。

**已知限制**

- **翻译上下文只对支持指令的后端生效**：内置三个免密端点（Google gtx / MyMemory / LibreTranslate）均未声明
  `supports_prompt`，因此默认**不会**多发送任何文本；上下文带来的提示效果只对自建 LLM（OpenAI 兼容等）可见。
  把 `translate.context_sentences` 设为 `0` 可彻底关闭。
- **400 ms 是内容刷新地板**：字幕**内容**最快 400 ms 变化一次（动画仍逐帧），这是「字幕最少 400 ms 刷新」的
  落地方式，避免高频重绘掉帧。
- **静音切片依赖能量/Silero 判定**：默认 energy 路径下安静但非零的音频仍可能进入识别，`silent` 标记是防御性兜底。
- **安装包仍不含模型**：`dist\SubMirror` 无 `models\` 目录，模型只随便携包分发。

<!-- 0.1.17-artifacts -->
## [0.1.17] - 2026-10-20 — FPS 俚语词典 + 增量预览先翻译 + 悬浮窗样式面板 + 性能四项

**交付范围**：本轮合并两批同发布需求——**翻译与字幕流**（内置 FPS 俚语词典、翻译前置提示配置、ASR 增量预览
先翻译再上屏、ASR 乱码过滤）、**悬浮窗样式可调**（预设 / 宽度 / 高度 / 字号 / 文字随面板缩放 / 圆角 / 背景透明度）、
**三项悬浮窗增强**（动画时长、文字颜色 + 描边粗细、窗口锁定）与**性能四项**（队列上限与丢弃最旧、energy VAD
分片加长、字幕最大刷新帧率与全屏降速、线程锁分离、日志分级）。
**OCR 业务逻辑零改动**；ASR/VAD 的识别算法本身未改（只改分片长度与队列策略）；ASR 页模型规格 / VAD 引擎的
悬浮提示（0.1.16 成果）原样保留。

**新增**

- **内置 FPS 俚语词典**（`assets/slang_dict.json`，46 条词条，随包分发）：`gg`/`ggwp`/`wp`/`gj`/`clutch`/`ace`/
  `headshot`/`nt`/`nice`/`op`/`ez`/`noob`/`smurf`/`camper`/`rush`/`push`/`save`/`eco`/`ff`/`afk`/`brb`/`lol`/`nah`/
  `ty`/`thx`/`u`/`r`/`on me`/`behind you`/`cover me`/`enemy`/`1v1`/`pug` 等。替换在翻译**之前**做（`SlangProcessor`），
  用词边界正则（`(?<![0-9A-Za-z])…(?![0-9A-Za-z])`）按词条长度降序匹配，避免 `u` 命中 `you`、`op` 命中 `open`；
  纯俚语短句（`gg`、`wp`、`u`）不再被最小长度过滤拦掉。配置键 `text.slang_enabled`（默认开）、`text.slang_path`。
- **翻译前置提示（能力门控）**：新增 `translate.prompt` / `translate.prompt_enabled` 配置键与
  `BaseTranslator` 的样式提示契约（`supports_prompt` / `default_prompt` / `style_prompt` / `stats()` 里的
  `prompt_supported`、`prompt_enabled`、`prompt_chars`）。**诚实边界**：本版内置的三个免密端点（Google gtx /
  MyMemory / LibreTranslate）都**没有**自由文本指令通道，因此**没有**任何后端声明 `supports_prompt`，
  预置提示词**不会**进入请求——真正改变输出的是上面的俚语词典与短句过滤。契约已就位，接入支持指令的
  翻译端点（OpenAI 兼容 / 自建 LLM）即可生效。
- **ASR 乱码与幻觉过滤**：`text.asr_noise_filter`（默认开）——过滤 Whisper 常见幻觉（`thank you`、
  `thanks for watching`、`please subscribe`、`subtitles by` 等）、`[Music]`/`（掌声）` 类标签、符号占比过高
  （如 `♪♪♪`）与单字符/单词重复（`yeah yeah yeah yeah`、`aaaaaaaa`）。被过滤的文本**不送翻译、不上屏**；
  `ok`/`no`/`nice` 一类真实短回应与中文重复语气词**不会**被误杀。
- **增量预览先翻译再上屏**：新增 `asr.preview_translate`（默认开）。ASR 增量假设不再直接把**英文原文**推给
  悬浮字幕，而是先经翻译，**拿到译文才渲染**；翻译失败/超时才回退原文，保证字幕不为空、不闪烁。最终成片段
  复用同一行并以译文收尾，避免"先闪原文再补译文"。
- **悬浮窗样式面板**：`overlay.size_preset` 预设下拉（默认圆角长方形 900×220/圆角 14、紧凑窄长矩形
  660×120/圆角 10、正方形 600×600/圆角 12，一键套用后仍可逐项微调）；宽度/高度由数字框改为**滑块 + 精确数字框**
  双向同步（200–8000 / 60–4000）；**字幕文字大小**独立滑块（8–96 px，与既有字号框同步）；
  **文字随面板缩放**开关（`subtitle.font_scale_with_panel`，按面板宽度相对 900 px 等比缩放并夹在
  最小字号–96 px）；**圆角半径**滑块 0–48 px（`overlay.corner_radius`，0 = 直角）；**面板背景透明度**滑块 0–100
  （沿用 `subtitle.mask_opacity`，0 = 完全透明、100 = 完全不透明，上限由 0.95 放宽到 1.0）。
- **三项悬浮窗增强**：**字幕动画时长**滑块 0–800 ms（0 = 关动画，同时写入 `subtitle.fade_in_ms`/`fade_out_ms`，
  载入时取两者较大值以免非零动画被显示成关闭）；**文字颜色选择器**（沿用 `overlay.text_color`，
  `#RRGGBB` 输入 + 取色按钮）+ **描边粗细**滑块 0–8 px（`overlay.outline_width`，0 = 无描边；描边由固定 4 向
  1 px 改为按粗细逐圈外扩，多圈偏移量模块化为 `outline_offsets()`）；**窗口锁定**开关
  （`overlay.lock_position`，开启后鼠标拖拽不再移动悬浮窗）。
- **性能：队列上限与丢弃最旧**（可配置）：`perf.audio_queue_max` / `vad_queue_max` / `asr_queue_max` /
  `ocr_queue_max` / `translate_queue_max`（默认 1/4/1/1/8），`TaskScheduler` 由 `build_lanes(config)` 读取；
  队列满时**丢弃最旧的未处理项**并通过 `task_dropped` 上报，`submit()` 永不阻塞调用方——防止任务堆积与时间戳跳变。
- **性能：energy VAD 分片加长**：新增 `audio.vad_energy_max_segment_seconds`（默认 9.0 s）。能量门控（无语音模型）
  用更长的分片，`VoiceActivityDetector.segment_limit_seconds` 按**实际生效引擎**取值（Silero 仍为 6.0 s），
  强制切分点统一走该属性——13 秒连续语音的识别任务数从 2 降到 1。
- **性能：字幕最大刷新帧率 + 全屏自动降速**：`perf.overlay_max_fps`（默认 30，下限 4 ms 间隔）、
  `perf.overlay_fullscreen_slowdown`（默认开）、`perf.overlay_fullscreen_scale`（默认 0.5）——全屏游戏在前台时
  淡入淡出定时器间隔加倍（30 fps → 15 fps）；动画内容与不透明度未变化的 tick **不重绘**（计为 skip）；
  worker 线程的 `refresh()` 在动画进行中按帧间隔合并，同一帧只画一次。检测用 `foreground_is_fullscreen()`
  （user32 `GetForegroundWindow`/`GetWindowRect` 与显示器矩形比对，>= 98% 视为全屏，结果缓存 0.5 s，
  失败即视为非全屏）。
- **性能：日志分级**：新增 `logging.level`（默认 `warning`，可选 `debug`/`info`/`warning`/`error`），
  设置面板**高级**页新增「日志」分组下拉；`resolve_log_level()` / `set_log_level()` 让改动**保存即生效**、
  无需重启。默认只输出警告/错误，避免调试日志持续写盘造成卡顿；**全项目仍不安装任何 FileHandler**
  （唯一写盘的是启动器 `silent_launch.py` 自己的 LibreTranslate 日志，与本分级无关）。

**变更**

- 版本号：`src/core/config.py` 的 `APP_VERSION` 与 `build_installer.nsi` 的 `APP_VERSION` 同步为 **0.1.17**。
- 设置面板键集合 **51 → 58 → 59**：0.1.17 新增 `subtitle.font_scale_with_panel`、`subtitle.fade_in_ms`、
  `subtitle.fade_out_ms`、`overlay.size_preset`、`overlay.corner_radius`、`overlay.outline_width`、
  `overlay.outline`、`logging.level`（`overlay.corner_radius` 与既有 `subtitle.*` 面板项此前未在面板出现）。
- `overlay.outline_width` 的 schema 由 `(0,12)` 收窄为 `(0,8)`，与绘制上限一致；`subtitle.mask_opacity`
  上界由 `0.95` 放宽到 `1.0`（与需求"100 = 完全不透明"一致）。
- 词典路径解析加固：相对路径不再依赖进程 cwd（此前从仓库外运行会静默加载失败）。
- 双语新增 21 条界面文案键（样式/日志相关标签与提示），随 `ui_language` 实时切换。

**依赖**：无新增、无升级（仍为 3 个出站域名的静态审计结果；未引入新第三方模块）。

**验证**（本机无头运行）

- 新增套件 `_v0117_style_check.py`（翻译/字幕流/样式 5 项与冻结哈希）与 `_v0117_perf_check.py`（性能 4 项），
  两套合计 **76/76**；其中关键断言：俚语替换逐例（`gg wp`→`打得好 干得漂亮`、`clutch it`→`残局翻盘`、
  `u r so good`→`你 是吗 so good`、`you are good` 不变）、幻觉/标签/重复字符被拒且真实短句保留、
  预览路径在预览翻译开启时不再直接推送原文、面板 59 键且提示键不落盘、队列满丢最旧且 `submit()` 不阻塞
  （200 次提交耗时 < 1 ms）、energy 分片 9 s / Silero 6 s、帧间隔 33 ms（全屏 67 ms）与无变化 tick 不重绘、
  lane 任务确实运行在非 GUI 线程、`stats()` 的全局锁内不再读 lane 计数、日志默认 `warning` 且无 FileHandler。
- 既有套件无回归（完整日志见 `submirror_verify_log.md`；该日志每次交付重新生成，尺寸与 SHA-256 以当轮交付说明为准）。
- 0.1.16 的悬浮提示断言按新面板键数（58→59）与新版号下限（>= 0.1.16 → >= 0.1.17）同步更新。

**产物**（本机实测）

- `dist\SubMirror-0.1.17-portable.zip`：283,411,527 B / 1035 entry / 全部条目可读（0 损坏），SHA-256 `6A525CC3C91A16D0538319E21BC3E11AFCFB698AB41F611B9AE3748F93E82928`。
- 包内 `SubMirror.exe`：10,360,842 B，SHA-256 `BD334CD86A9D8F70F0D581569A742E1C037A4DD48DE112ED39756405E16C960E`（与磁盘上的便携目录副本逐字节一致）；安装包 `SubMirror-0.1.17-setup.exe`：96,167,760 B，SHA-256 `34D41FCD8FA3D9A16051936C37D873B307EC6974DAACDBB4B3AD610B25267A81`。
- 包内 `assets\slang_dict.json` 为 0.1.17 词典（46 条）；`models\silero_vad.onnx` 与 `models\faster-whisper-base`
  与 0.1.13~0.1.16 相同。
- 包内分发副本已随本轮文档更新（`README.md`、`PRIVACY.md`、`requirements.txt`、`LICENSE` 与仓库逐字节一致）。

**已知限制**

- **前置提示词对内置免密端点无效**（见上"诚实边界"）：三个后端都无指令通道，`prompt_supported` 恒为 `False`，
  配置键与契约只为后续接入支持指令的翻译端点预留。本版真正的翻译改善来自俚语词典与短句过滤。
- **全屏降速依赖前台窗口探测**：非 Windows 或探测失败时一律视为非全屏（即不降速），不会报错；无边框窗口化
  全屏以 98% 显示器面积判定，个别带黑边的全屏窗口可能不被识别。
- **词典为静态词表**：只做整词替换，不做上下文消歧（`op` 在 `operation` 语境下不替换，但独立出现的 `op`
  一律译作"太强了"）；46 条为起始规模，可持续扩充。
- 沿用既有延期项：安装包 `makensis` 编译、WASAPI 环回实采、打包后 `SubMirror.exe` 冒烟启动仍需在策略宽松的
  目标机复验；真实游戏音频下的端到端滞后改善幅度同样待真机确认。

<!-- 0.1.16-artifacts -->
## [0.1.16] - 2026-10-19 — ASR 设置页悬浮提示（模型规格逐项 + VAD 引擎）

**交付范围**：只新增**界面悬浮提示文字**，**不改任何 ASR / OCR 业务逻辑**：`src/audio/*`（VAD 与 Silero 封装）、
`src/screen/*`（OCR 引擎与选区拾取）、`src/ai/*`（翻译/文本处理）**零改动**；设置面板的键集合仍为 **51 键**
（提示文案不落盘、不参与 `collect()` / `load_from_config()`）；无新增配置键、无 schema 变更。

**新增**

- **模型规格下拉逐项提示**：ASR 页的「模型规格」下拉为 5 个选项各带一条 Tooltip（`Qt.ToolTipRole`），展开列表时
  悬停任一项即显示该规格的适用场景；下拉收起时跟随当前选中项刷新（选中 `base` 显示 `base` 的说明）。文案按用户给的
  口径：跟随模型路径 = 手动指定模型文件夹（进阶自定义）；`tiny` = 体积最小最快、精度低（低配/安静短句，不适合枪声与
  背景音乐）；`base`【默认推荐】= 体积小速度快、精度均衡（适配绝大多数 FPS 与语音通话，便携包内置开箱即用）；
  `small` = 抗噪与精度更好、占用更高（嘈杂环境、带背景音乐人声、英文歌词，需自备模型放入 models 文件夹）；
  `medium` = 识别能力强抗干扰、资源消耗大（低配易延迟，不推荐游戏实时翻译）。
- **VAD 引擎提示**：ASR 页只读的「人声检测」状态行（展示配置键 `audio.vad_engine` 的当前值）带一条 Tooltip，
  首行写出当前生效引擎（`audio.vad_engine = auto|silero|energy`），其后逐条解释三种引擎：`auto` = 优先 Silero、
  失败自动回退 Energy；`silero VAD` = AI 人声检测（只识别干净人声，过滤枪声与背景音乐，会过滤演唱）；
  `energy VAD` = 音量阈值（响度达标即送识别，适合背景音乐/歌词，纯噪音也会尝试）。
- 提示文案为**双语**（`settings.asr_model_size.tip.*`、`settings.asr_vad_engine.tip.*`，zh-CN / en 各 9 键），
  随 `ui_language` 实时切换（`retranslate()` 重放），加载配置时同步刷新 VAD 提示里的引擎名。

**变更**

- 版本号：`src/core/config.py` 的 `APP_VERSION` 与 `build_installer.nsi` 的 `APP_VERSION` 同步为 **0.1.16**。
- 提示文字只是新增 UI 文案，不改变任何控件的可编辑性、取值范围或写盘行为。

**依赖**：无新增、无升级（未引入新模块与出站域名，静态审计仍为 3 个域名）。

**验证**（本机无头运行）

- 新增套件 `_v0116_tooltip_check.py`：**21/21**。覆盖「9 条提示键中英双语存在且不是键名回显」「提示键不在
  `DEFAULT_CONFIG` 中」「模型规格表与提示表一一对齐（5 项同序）」「`Qt.ToolTipRole` 逐项写入 + 收起时跟随选中项 +
  语言切换后仍正确」「VAD 提示首行跟随 `audio.vad_engine`（auto/silero）」「面板键集合仍为 51 且提示键不进入
  `collect()`」「提示方法不写配置」「`src/audio` + `src/screen` + `src/ai` 共 8 个核心文件未被本轮触碰」。
- 既有套件无回归（完整日志 `submirror_verify_log.md`，**79,848 B**，SHA-256 `486AB80E01606F2EB0FFAC4BA0BA7456174963A0EAAD212B6DBF6DB2C6142537`）。
- 本轮无既有断言需要改写：0.1.16 只加 UI 文案，不改任何默认值、热键或面板键集合。

**产物**（本机实测）

- `dist\SubMirror-0.1.16-portable.zip`：283,387,691 B / 1035 entry / 全部条目可读（0 损坏），SHA-256 `B057B8F1E4D81B0EC6023DE6688B949307CA4D502E2CF0A6C2D9123C874192D2`。
- 包内 `SubMirror.exe`：10,339,877 B，SHA-256 `40327A19D7F76820C3C569E60B4CCC87BB014D3E7E16B4879754865C5ED56CDD`（与磁盘上的便携目录副本逐字节一致）。
- 包内 `models\silero_vad.onnx`：2,327,524 B；包内 `models\faster-whisper-base`：5 文件 147,884,932 B（与 0.1.13~0.1.15 相同）。
- 包内分发副本已随本轮文档更新（`README.md`、`PRIVACY.md`、`requirements.txt`、`LICENSE` 与仓库逐字节一致）。

**已知限制**

- 「VAD 引擎」在本版是**只读状态行**（引擎由 `audio.vad_engine` 配置键决定，面板不提供下拉），因此提示挂在状态行上；
  若希望面板里直接切换引擎，需要一个可写的下拉与 `collect()` 键（会把面板键集合从 51 改为 52），属于功能变更，
  未在本轮范围内实施。
- 沿用既有延期项：安装包 `makensis` 编译、WASAPI 环回实采、打包后 `SubMirror.exe` 冒烟启动仍需在策略宽松的
  目标机复验；真实游戏音频下的 Silero 命中率与端到端滞后改善幅度同样待真机确认。

<!-- 0.1.15-artifacts -->
## [0.1.15] - 2026-10-19 — 保存校验按 ASR 开关短路 + 固定选区「松开左键即保存、Esc 才结束」+ Alt+C 默认热键

**交付范围**：只做用户要求的两处行为修复，**其余功能一字未改**：`src/ai/*`（翻译/文本处理/OCR 引擎）零改动；
`src/audio/*`、`src/core/subtitle_queue.py`、`src/ui/overlay.py` 零改动；设置面板的键集合仍为 **51 键**（无新增配置键）。

**修复**

- **保存校验不再在 ASR 关闭时误报 `not-a-directory`**：此前「保存」无条件校验手填的 Whisper 模型目录，
  哪怕识别根本不开。现在 `SettingsDialog` 记住主程序的 ASR 开关（`set_asr_enabled()`，由 `open_settings()` 在
  弹窗前按 `not self._asr_off` 注入），`validate()` 仅在**识别已启用**且路径非空时才做目录校验；
  **ASR 关闭时完全跳过**——不论 `asr.model_path` 里写了什么（空、不存在、指向文件）都不再弹框、不再拦截保存。
  原有的错误码与提示文案（`model-path-empty` / `model-path-missing`）一字未改，仍只作用于「ASR 开着且路径有问题」。
- **固定选区拾取改为「松开左键即保存、Esc 才结束」**：旧的「按住热键 → 松开热键 → 立刻清空选区」流程会让选区
  在保存前被抹掉。现在：**Alt+C**（`hotkeys.toggle_region_ocr` 默认值，可在设置面板改）唤起遮罩；**松开左键立即**
  提交坐标并把选区写入面板（`区域已保存` 提示），遮罩**继续留在屏幕上**让人看到框选结果；按 **Esc**（或右键/
  Backspace）才结束本次拾取并收起遮罩。热键松开**不再**清空选区，也不影响已保存的坐标。
  `AreaPicker` 的既有语义（拖拽提交、过小取消、非 GUI 线程拒绝）不变。

**变更**

- `hotkeys.toggle_region_ocr` 默认值由 `""`（未绑定）改为 **`Alt+C`**，与用户的要求一致；用户仍可在设置面板
  快捷键页改绑或清空（清空即代表不注册，与 0.1.13 的「默认不占用热键」行为兼容）。快捷键冲突检测逻辑未改。
- 提示文案对齐新语义：固定区域提示与选区拾取窗口提示改为「松开左键确认，Esc 结束」/`release the mouse to confirm,
  Esc to finish`，并明确 Alt+C 唤起的含义（`settings.ocr_fixed_region_hint`、`settings.hotkey.region_hint`、遮罩窗口内置文案）。
- 版本号：`src/core/config.py` 的 `APP_VERSION` 与 `build_installer.nsi` 的 `APP_VERSION` 同步为 **0.1.15**。

**依赖**：无新增、无升级。纯行为修复，不引入新模块、不新增出站域名（静态审计仍为 3 个）。

**验证**（本机无头运行，全部通过）

- 新增套件 `_v0115_pick_save_check.py`：**13/13**。覆盖「ASR 关 → 任何 model_path 都不拦保存」「ASR 开 → 空/缺失路径
  仍按原错误码拦截」「主程序注入 ASR 开关」「Alt+C 是默认热键且可被清空/改绑」「固定选区松开左键即保存且遮罩不销毁」
  「Esc 取消并收起遮罩」「热键松开不清空已保存坐标」「提示文案含 Esc 语义」「`src/ai` 冻结与 `src/screen` 仅
  `area_picker.py` 例外」「面板键集合仍为 51」。
- 既有套件无回归（完整日志 `submirror_verify_log.md`，**76,076 B**，SHA-256 `D908D3A4025E773E2347D2C7DD9A99D54BBD30E2EA1A1B91DB3F2CBFF2AE0BB4`）：
  静态审计 files 33 / py 21 / 0 语法错 / 31 内部导入边 / 0 环 / 0 禁用词 / 3 出站域名 + BL-1 配置模板 0 差异、
  双语键集合 0 缺失、运行时接线 20/20、死控件 0、BL-3 行过滤 23/23、BL-2 热键冲突 34/34、BL-5 九页签与 51 键 40/40、
  第四轮 7/7、入口回归 8/8、accept 28/28、accept2 12/12、accept3 15/15、托盘 18/18、0.1.9 便携包 14/14、
  0.1.10 提示文案 15/15、0.1.11 静默启动 18/18、0.1.12 内置模型 17/17、0.1.13 固定区域 23/23、0.1.14 流式 35/35。
- 一处既有套件断言按 0.1.15 的新契约更新：`_v0113_region_ocr_check.py` 的热键默认值断言由空串改为 **`Alt+C`**
  （该项本来就只在验证「默认热键」这一事实，改的是期望值，不是覆盖面）；`_v0111_silent_start_check.py` 的
  「核心未触碰」核对新增与 0.1.14 相同的 `src/screen/area_picker.py` 合法例外（该文件本轮只改了提示文案）。

**产物**（本机实测）

- `dist\SubMirror-0.1.15-portable.zip`：283,384,784 B / 1035 entry / 全部条目可读（0 损坏），SHA-256 `8CAAC223E4A6AB21FDBB1C58F410787E56C709CD68250F6611FDC8C3D25E1C9B`。
- 包内 `SubMirror.exe`：10,337,009 B，SHA-256 `B62C78CAF3228530B94EF42D680B30F8A636C07E3F3C8166DE0283ABFD542B72`（与磁盘上的便携目录副本逐字节一致）。
- 包内 `models\silero_vad.onnx`：2,327,524 B（与 0.1.14 相同）；包内 `models\faster-whisper-base`：5 文件 147,884,932 B（与 0.1.13/0.1.14 相同）。
- 包内分发副本已随本轮文档更新（`README.md`、`PRIVACY.md`、`requirements.txt`、`LICENSE` 与仓库逐字节一致）。

**已知限制**

- 遮罩窗口在选区提交后会一直留在屏幕上直到 Esc：这是本轮要求的「不自动销毁」。若长时间不收，遮罩会持续置顶
  拦截鼠标点击（这是拾取窗口的固有行为），按 Esc 即可退出。
- 沿用既有延期项：安装包 `makensis` 编译、WASAPI 环回实采、打包后 `SubMirror.exe` 冒烟启动仍需在策略宽松的
  目标机复验（本机被 Application Control 策略拦截）；真实游戏音频下的 Silero 命中率与端到端滞后改善幅度同样待真机确认。

<!-- 0.1.14-artifacts -->
## [0.1.14] - 2026-10-18 — ASR 流式优化（Silero VAD）+ 模型下拉 + 字幕淡入淡出 + 区域选择拖拽修复

**交付范围**：四项要求加一处缺陷修复，**不改 OCR 引擎与翻译核心**：`src/ai/*`（翻译/文本处理）零改动；
`src/screen/*` 仅 `area_picker.py` 的**拖拽状态修复**（第 1 条要求本身就在该文件）。OCR 引擎行为、翻译器多端点
故障转移、字幕队列的既有语义（除新增的「就地更新一行」能力外）一字未改。

**修复**

- **区域选择「松开左键不退出绘制、选区继续跟随鼠标」**：遮罩窗口 `_PickerWindow.mouseMoveEvent` 此前不判断
  左键是否仍按住，而拖拽锚点在 release 之后继续保留（热键流程要等 KEYUP 才关掉遮罩），于是松手后移动鼠标
  仍会改矩形，看起来像「没退出绘制状态」。现在 `_PickerWindow` 维护显式的 `_dragging` 状态：
  `mousePressEvent`（左键）置位、`mouseMoveEvent` / `mouseReleaseEvent` 开头先判 `_dragging`（没有按下就整段忽略）、
  release **必先复位再提交** `committed`（有效选区）或 `cancelled("too-small")`；Esc / Backspace / 右键 / 关闭窗口
  一并复位；新增只读属性 `is_dragging` 供断言。画框保存坐标与一键启停链路不变。

**新增**

- `src/audio/silero_vad.py`（新模块）+ `models\silero_vad.onnx`（2,327,524 B，MIT 许可，随源码树与便携包分发）：
  Silero VAD 的 **ONNX 推理封装**，不引入 torch。与官方 `OnnxWrapper` 完全一致：16 kHz 下 **512 样本/帧**，
  并把上一帧尾部 **64 样本**作为上下文拼在前面（实际喂入 576）；`state` 形状 `(2,1,128)` 零初始化；
  `onnxruntime` CPU、intra/inter 线程各 1。`find_model_path` 依次查 配置项 → 环境变量 `SUBMIRROR_SILERO_VAD`
  → exe 同级 / 仓库根 / `src\audio` 下的 `models\silero_vad.onnx`。**模型缺失、加载失败或单帧推理异常都不会抛异常**，
  记下原因后透明退回既有能量门限（`engine` 暴露 `silero` / `energy`，`engine_error` 暴露 `model-missing` 等）。
- **ASR 流式增量输出**：`asr.streaming`（默认开）。讲话进行中按 `asr.partial_interval_ms`（1100 ms）把「本段已累积音频」
  送去识别，结果作为**预览行就地更新**（同一 `item id`，不新起字幕行）；区间下限 `asr.partial_min_seconds`（0.6 s）、
  上限 `asr.partial_max_seconds`（8.0 s）。分段结束后的最终识别结果**替换**该行并提交译文，避免预览行与最终行并排重复。
- **翻译时间对齐**：`asr.translate_in_lane`（默认开）。翻译从 ASR 泳道搬到独立的 `LANE_TRANSLATE` 泳道执行，
  ASR 泳道不再被翻译阻塞；迟到的译文通过新增的 `SubtitleQueue.update()` **回填到原字幕行**（就地刷新文案与过期时间），
  而不是新推一行。翻译失败仍按既有语义把该行转为「仅显示原文」，不抛异常。
- **ASR 设置页模型下拉**：`asr.model_size`（跟随模型路径 / `tiny` / `base` / `small` / `medium`）。选中型号后
  `AsrEngine._resolve_model_path` 先按型号在**内置/同机目录**里找对应模型，找不到再回落到手填的 `asr.model_path`，
  原有的「配置路径失效 → 自动回退」语义不变。旁附只读的「VAD 引擎」状态行，仅展示当前生效引擎与降级原因，**不新增配置键**。
- **悬浮窗字幕淡入淡出（QQ 音乐式）**：`subtitle.fade_in_ms`（180）、`subtitle.fade_out_ms`（220）、
  `subtitle.fade_slide_px`（6）。每条字幕按通道（ASR / TTS / OCR）独立做 alpha 渐显 + 轻微垂直位移，
  行被移除前先淡出；`fade_in_ms` 与 `fade_out_ms` 同时为 0 时**退化为原先的即时显隐**（零动画路径）。

**变更**

- **强制英文**：`asr.force_english`（默认 `True`）——识别固定 `language="en"`，不再逐段依赖 `asr.language="auto"` 的
  自动语种判定（改回 `false` 即恢复原有 `auto`/指定语种行为）。理由：目标场景是英文游戏语音，锁定语种可省掉一次
  语种判定并减少解码抖动。
- **解码参数调优（faster-whisper 透传）**：`asr.beam_size` 默认 `5` → **`1`**；新增 `asr.best_of`（1）、
  `asr.temperature`（0.0）、`asr.condition_on_previous_text`（False）、`asr.without_timestamps`（True）、
  `asr.no_speech_threshold`（0.5）、`asr.compression_ratio_threshold`（2.2）、`asr.log_prob_threshold`（-1.0）、
  `asr.initial_prompt`（`"Video game dialogue."`）。默认走贪心解码并关闭时间戳/上文条件，换取更低的端到端延迟。
- **VAD 参数与滑窗**：新增 `audio.vad_engine`（`auto`/`silero`/`energy`）、`audio.vad_silero_path`、
  `audio.vad_silero_threshold`（0.5）、`audio.vad_silero_negative_margin`（0.15）、`audio.vad_silero_min_speech_ms`（250）、
  `audio.vad_overlap_ms`（200，强制切段时保留的尾部重叠，避免把词切断）；默认值 `audio.vad_hangover_ms` `400` → **`320`**、
  `audio.vad_max_segment_seconds` `15.0` → **`6.0`**。更小的分片 + 更短的拖尾判定，是缓解 5–6 秒输出滞后的主要手段。
- `src/core/config.py` 的 `_SCHEMA` 与 zh/en 文案同步补齐（新增 `settings.asr_model_size`、`settings.asr_model_size.hint`、
  `settings.asr_vad_engine`、`settings.subtitle_fade_in` / `_fade_out` / `_fade_slide`）。
- 设置面板 `collect()` 由 50 键增至 **51 键**（新增 `asr.model_size`）；`_bl5_tabs_check.py` 基线与
  `submirror_runtime_accept.py` 的期望值同步为 51。
- `.gitignore` 增加例外：`!models/silero_vad.onnx`（2.3 MB 的 MIT 权重随源码分发，其余 `*.onnx` 与 `models/` 仍忽略）。

**依赖**：无新增、无升级。Silero 推理复用已随 faster-whisper 安装的 `onnxruntime`；模型权重随仓库/便携包分发，
**运行时零下载**。requirements.txt 已把该段的说明从「可选/后续」改写为「0.1.14 起实际使用，缺失即降级」。

**验证**（本机无头运行，全部通过）

- 新增套件 `_v0114_asr_stream_check.py`：**35/35**。覆盖区域选择 `_dragging` 状态机（按下前/按下/移动/释放/取消）、
  Silero 加载与路径解析（含环境变量覆盖、坏路径回退、模型缺失降级、`stats` 属性契约）、滑动窗口部分识别的时序与
  就地更新、`LANE_TRANSLATE` 时间对齐与迟到译文回填、模型下拉的解析优先级、字幕淡入淡出数学与零动画退化、
  设置面板 51 键接线、`src/ai` 冻结与 `src/screen` 仅 `area_picker.py` 例外、README/PRIVACY 文档一致性。
- 既有套件无回归（完整日志 `submirror_verify_log.md`，**70,751 B**，SHA-256 `C8136B41216C7D5F62F0BA1038C3BB6C02F627022854C7913B07BB6228BAF764`）：
  静态审计 files 33 / py 21 / 0 语法错 / 31 内部导入边 / 0 环 / 0 禁用词 / 3 出站域名 + py_compile 21 文件全过 +
  BL-1 配置模板 0 差异、双语键集合 0 缺失、运行时接线 20/20、死控件 0、BL-3 行过滤 23/23、BL-2 热键冲突 34/34、
  BL-5 九页签与 51 键 40/40、入口回归 8/8、accept 28/28、accept2 12/12、accept3 15/15、第四轮 11/11、托盘 18/18、
  0.1.9 便携包 14/14、0.1.10 提示文案 15/15、0.1.11 静默启动 18/18、0.1.12 内置模型 17/17、0.1.13 固定区域 23/23。
- 两处既有套件断言按 0.1.14 的新契约更新：`_v019_asr_bundle_check.py` 的中文识别改为显式关掉
  `asr.force_english`（并新增一条「force_english 会覆盖 `asr.language`」的断言，13 → **14** 项）；
  `_v0112_model_bundle_check.py` 的 main.py 接线断言改为新的 `_resolve_model_path(configured, self._model_size)`
  签名，并把 `src\screen\area_picker.py` 列为本轮冻结核对中唯一允许改动的文件（其余 `src/ai`+`src/screen` 一字未动）。
- 静态审计：files 33（含新增的 `src/audio/silero_vad.py` 与 `models/silero_vad.onnx`）/ py 21 / 语法错 0 / 内部导入边 31 / 环 0 / 禁用词 0 / 出站域名 3（未新增任何出站域名）。

**产物**（本机实测）

- `dist\SubMirror-0.1.14-portable.zip`：283,383,234 B / 1035 entry / 全部条目可读（0 损坏），SHA-256 `3AE3B42AA1B2F6518A382971630001A8CD643E8D367B751537DE548D960DF0C7`。
- 包内 `SubMirror.exe`：10,336,158 B，SHA-256 `10D823EB7D73A6AD63D3CB4E67D4B6BE9F6F2825BAF8634CC0F88868B463B9C7`（与磁盘上的便携目录副本逐字节一致）。
- 包内 `models\silero_vad.onnx`：2,327,524 B（新增，便携包离线自带 VAD）；包内 `models\faster-whisper-base`：5 文件 147,884,932 B 与 0.1.13 相同。

**已知限制**

- **Silero VAD 的阈值与官方 16 kHz 参考实现一致，未针对游戏音频重调**：`audio.vad_silero_threshold`（0.5）
  与 `negative_margin`（0.15）是保守默认值，音乐/战斗音效密集的素材需要在目标机上按实际听感微调；
  调不稳时把 `audio.vad_engine` 设为 `energy` 可立即退回旧行为。
- **流式预览会重复消耗一次识别**：为了低延迟，同一段音频可能被识别多次（预览 + 最终）。CPU 占用与 0.1.13 相比
  略有上升，`asr.streaming=false` 可关闭该路径。
- 沿用既有延期项：安装包 `makensis` 编译、WASAPI 环回实采、打包后 `SubMirror.exe` 冒烟启动仍需在策略宽松的目标机复验
  （本机被 Application Control 策略拦截）；本机无法构造真人语音，Silero 的实际命中率与 5–6 秒滞后的改善幅度
  需用真实游戏音频在目标机上确认。

<!-- 0.1.13-artifacts -->
## [0.1.13] - 2026-10-17 — 固定区域持续 OCR（画框保存 + 一键启停）

**交付范围**：只加「固定区域持续 OCR」这一条新链路，**不改 ASR 与翻译核心**：
`src/ai/*`、`src/audio/*` 零改动（时间戳核对含该项）。OCR 引擎、翻译器、字幕队列的既有行为与
错误码一字未改，新链路复用它们。

**新增**

- `src/core/region_watch.py`（新模块）：`parse_region` / `format_region` 解析与格式化 `"x,y,w,h"`；
  16×16 亮度指纹 `fingerprint`、`is_changed`（容差 + 最少变化格数）；`normalize_text` / `text_changed`
  文本级去重。纯函数、无 Qt、无副作用，可独立测试。
- **画框保存坐标**：设置面板 OCR 页新增「固定区域」行——只读坐标显示 + **画框选取** 按钮 + 启用开关 +
  变化灵敏度。点「画框选取」调用既有 `AreaPicker` 全屏遮罩，拖拽确认后把全局坐标回填面板并写入
  `config.json`（`ocr.fixed_region`）；取消则不改动原值。拾取期间主窗口与悬浮层自动让位，拾取结束恢复。
- **一键启停**：新快捷键 `hotkeys.toggle_region_ocr`（默认建议 `Ctrl+Alt+R`，可在快捷键页修改），
  与既有三路热键共用同一套冲突检测（`check_hotkey_conflict` 由三参扩展为四参，空值/全空白跳过）。
- 运行时不新开线程：固定区域与既有「拖拽选区」共用同一个 60 ms 定时器与 `ScreenCapturer`，
  未落地选区或未启用时该路径不工作。

**两道变化门（避免静止画面重复 OCR / 重复翻译）**

1. **图片门**：先对截图做 16×16 灰度指纹，与上一次比对；容差 `ocr.region_change_tolerance`（默认 `8`），
   且必须有 ≥4 格越界才判定「有变化」，抑制压缩噪声与光标抖动。
2. **文本门**：OCR 结果经 `normalize_text`（去空白、统一大小写、去标点）后与上次文本比对，
   相同则不提交字幕/不触发翻译。

**验证**（本机无头运行，全部通过）

- 新增套件 `_v0113_region_ocr_check.py`：**23/23**。覆盖坐标解析与格式化往返、非法/边界输入、
  指纹与 `is_changed` 容差语义、文本规范化去重、`OcrService` 固定区域启停与统计字段、
  启停热键注册与冲突检测、`AreaPicker` 回调接线、设置面板 50 键 collect/load 往返、
  `src/ai`+`src/audio` 冻结时间戳核对。
- 既有套件无回归（详见 `submirror_verify_log.md`，本轮重生成的完整日志 63,477 B）：BL-1 配置模板/双语键集合/运行时接线 20/20、BL-3 行过滤 23/23、BL-2 热键冲突 34/34、BL-5 九页签与 50 键 40/40、0.1.6 翻译守门 7/7、第一轮 28/28、第二轮 12/12、入口回归 8/8、第三轮 15/15、第四轮（真实 TTS + 真实公网端点）11/11、托盘 18/18、0.1.9 内置模型 13/13、0.1.10 提示文案 15/15、0.1.11 静默启动 18/18、0.1.12 模型识别 17/17；`py_compile` 20 文件 0 失败；控件属性静态核查 20 文件仅 1 项非 OK（`src/audio/capture_win.py` 的 `self._config` / `self._meter`，由基类 `src/audio/loopback.py` 赋值，属继承而非缺陷）。
- 静态审计：files 31 / py 20 / 语法错 0 / 导入边 30 / 环 0 / 禁用词 0 / 出站域名 3（未新增任何出站域名）。
- 校验日志：`submirror_verify_log.md` = 63,477 B（本轮重生成，含第十轮 0.1.13 套件；头部的版本/产物哈希/文件数已同步到 0.1.13）。

**依赖**：无新增、无升级、无移除（与 0.1.12 完全相同，见 requirements.txt）。

**产物**（本机实测）

- `dist\SubMirror-0.1.13-portable.zip`：281,405,048 B / 1035 entry / `testzip` 无损坏，SHA-256 `746B94F16F2A61CF2F0CF800FD588873B0484A16234AB6A61803123B437DA85B`。
- 包内 `SubMirror.exe`：10,307,913 B，SHA-256 `97414A760081E50C0B16F0F594ED60FE54B3C0A91D71AD057A8F06048E3DEA96`（zip 内同名文件大小 10,307,913 B，逐字节一致）。
- 包内 `models\faster-whisper-base`：5 文件 147,884,932 B（`model.bin` 145,217,532 B），平铺无嵌套；分发副本 `README.md` / `LICENSE` / `PRIVACY.md` / `requirements.txt` / `start_submirror.bat` 齐备。

**已知限制**：与 0.1.12 相同——安装包 `makensis` 编译、WASAPI 环回实采与打包 `SubMirror.exe`
冒烟启动需在装有完整依赖的目标机复验（本机被 Application Control 策略拦截）；固定区域 OCR 的真机
效果（坐标框选的手感、不同游戏分辨率的灵敏度默认值）建议在目标机各试一次。

<!-- 0.1.12-artifacts -->
## [0.1.12] - 2026-10-17 — 便携包内置 Whisper 模型自动识别

**交付范围**：只改「模型目录怎么解析」，不改识别、翻译与原有校验。`src/ai/*`、`src/screen/*`（OCR / 翻译核心）**零改动**（文件时间戳未变，验收套件含该项核对）。

**修复**

- **便携包内置模型不被识别**：`SubMirror.exe` 同级的 `models\faster-whisper-base` 之前只有
  `start_submirror.bat` 把路径写进配置后才生效——直接双击 exe、把文件夹换个位置、
  或者配置里留着指向**已被删除的旧目录**的 `asr.model_path`（本次实测到的真实成因）时，
  程序仍报「未配置 Whisper 模型」。

**变更**

- `main.py` 新增 `BUNDLED_MODEL_RELATIVE` 与只读查找 `bundled_model_dir()`：按 `SubMirror.exe`
  所在目录（再退到包根）查找 `models\faster-whisper-base`，不创建、不复制、不写配置。
- `AsrEngine` 新增 `_resolve_model_path()`：**有效的手选目录优先** → 否则使用内置模型 →
  两者都没有时**原样返回配置值**；`apply_config()` 记录来源，新增 `model_source` 属性。
- **原有校验逻辑一字未改**：自动找到的目录仍要满足与手选目录完全相同的检查
  （`model.bin` + `config.json` 平铺等，`validate_model_dir` 未改动），不合法时给出的仍是原来的
  `model-path-empty`（未配置）与 `model-path-missing`（路径不存在）错误码。

**验证**（本机无头运行，全部通过）

- 新增套件 `_v0112_model_bundle_check.py`：**17/17**。在**真实便携版目录**上验证：5 个模型文件
  平铺无嵌套、zip 载荷完整且 `testzip` 干净；四种解析分支（有效手选 / 空值回退 / 失效路径回退 /
  无内置模型）；两种错误码回归；`validate_model_dir` 逐项比对不变；解析过程不写配置；
  `src/ai`+`src/screen` 冻结时间戳核对。
- 既有套件无回归：BL-1 双语 20/20、BL-3 23/23、BL-5 40/40、0.1.6 守门、四轮验收、托盘 18/18、
  0.1.9 内置模型 13/13、0.1.10 提示文案 15/15、0.1.11 静默启动 18/18。
- 静态审计：files 30 / py 19 / 语法错 0 / 导入边 29 / 环 0 / 禁用词 0 / 出站域名 3。

**依赖**：无新增、无升级、无移除（与 0.1.11 完全相同，见 requirements.txt）。

**产物**（本机实测）

- `dist\SubMirror-0.1.12-portable.zip`：281,388,788 B / 1035 entry / `testzip` 无损坏，SHA-256 `02C0EF59F6EE62339E8C88C4AF182053E7303613A295D3E7B0F23A599D938539`。
- 包内 `SubMirror.exe`：10,293,861 B，SHA-256 `4386F354DCE9457335E099BB98CFA37983E5F0B048C9F03A7DB7903B72D721A6`（zip 内同名文件大小 10,293,861 B，逐字节一致）。
- 包内 `models\faster-whisper-base`：5 文件 147,884,932 B（`model.bin` 145,217,532 B），平铺无嵌套。

**已知限制**：与 0.1.11 相同——安装包 `makensis` 编译、WASAPI 环回实采与打包 `SubMirror.exe` 冒烟启动
需在装有完整依赖的目标机复验（本机被 Application Control 策略拦截）。

**延期项**：无新增。

<!-- 0.1.11-artifacts -->
## [0.1.11] - 2026-10-17 — 登录时静默启动 LibreTranslate

**交付范围**：只加「怎么启动」这一层，不改识别与翻译本身。`src/ai/*`、`src/screen/*`（OCR / 翻译核心）**零改动**（文件时间戳未变，验收套件含该项核对）。

**新增**

- 设置 → 翻译页新增两项（`translate` 组）：
  - **「LibreTranslate 目录」**（`translate.libre_dir`）：指向含 `start_libretranslate.bat` 的目录；
    留空时自动探测常见位置，带浏览按钮与「目录可用 / 不可用」即时校验，非 ASCII 路径给出提示。
  - **「登录时静默启动 LibreTranslate（无窗口）」**（`translate.libre_autostart`）：勾选即注册登录启动项。
- 新模块 `src/core/silent_launch.py`（Qt-free、无网络调用）：向当前用户的「启动」文件夹写入
  `SubMirror-SilentStart.vbs` —— 登录时以隐藏窗口拉起 LibreTranslate，轮询翻译端点直到**真正就绪**，
  再启动 SubMirror 主程序；超时未就绪则**不启动主程序**，原因写入 `%APPDATA%\SubMirror\silent_start.log`。
  取消勾选删除该启动项，并一次性清理同名旧计划任务（`SubMirror-SilentStart` / `SubMirror-LibreTranslate`）。
- 端口取自翻译页的「服务地址」（默认 `http://127.0.0.1:5000`）。

**设计取舍**

- **不用真·计划任务**：实测未提权进程执行 `schtasks /Create /SC ONLOGON` 返回 `Access is denied`（本机为分裂令牌），需 UAC 提权；改用**当前用户「启动」文件夹 + 隐藏 VBS**，零提权、零弹窗。
- 启动脚本由程序生成，内含实际路径与端口；升级或改目录后重新注册即随之更新。

**验证**（本机无头运行，全部通过）

- 新增套件 `_v0111_silent_start_check.py`：**18/18**。覆盖 VBS 生成内容与编码、注册 / 取消 / 幂等、
  就绪与未就绪两条路径（含超时不启主程序、日志落盘）、目录校验、真实 `SettingsDialog.collect()` 落盘与回读、
  `src/ai`+`src/screen` 冻结时间戳核对。
- 新增端到端联调 `_v0111_vbs_e2e.py`：在本机真实 Windows Script Host（`cscript`）里执行生成的脚本，
  验证无黑窗启动、就绪轮询与失败分支。
- 既有套件无回归：BL-1 双语 20/20、BL-3 23/23、BL-5 40/40（`BASELINE_KEYS` 44 → 46）、0.1.6 守门、
  四轮验收、托盘 18/18、0.1.9 内置模型 13/13、0.1.10 提示文案 15/15。
- 静态审计：files 30 / py 19 / 语法错 0 / 导入边 29 / 环 0 / 禁用词 0 / 出站域名 3。

**依赖**：无新增、无升级、无移除（与 0.1.10 完全相同，见 requirements.txt）。

**产物**（本机实测）

- `dist\SubMirror-0.1.11-portable.zip`：281,387,978 B / 1035 entry / `testzip` 无损坏，
  SHA-256 `755B5E607848379833007A8648EF44B24FEC5B79F80B4059050FDC14D7230EAA`。
- 包内 `SubMirror.exe`：10,292,455 B，SHA-256 `0C434D2D7726E075EC942C468B7DD87864ED167CC3173EA26DFE5E16DC484397`（zip 内同名文件大小 10,292,455 B，逐字节一致）。

**已知限制**：与 0.1.10 相同——安装包 `makensis` 编译、WASAPI 环回实采与打包 `SubMirror.exe` 冒烟启动
需在装有完整依赖的目标机复验（本机被 Application Control 策略拦截）。

**延期项**：无新增。

## [0.1.10] - 2026-10-17 — 用户可见提示收敛：ASR 关闭隐藏 Whisper 提示 + 翻译失败提示条简化

**交付范围**：只改「提示怎么显示」，不改识别与翻译本身。
`src/ai/*`、`src/screen/*`（OCR / 翻译核心）**零改动**（文件时间戳未变，验收套件含该项核对）。

**修复**

- **ASR 关闭后仍弹 Whisper 提示**：用 `hotkeys.toggle_asr` 暂停语音识别后，悬浮窗不再出现
  「语音识别不可用：请在设置中选择 Whisper 模型目录。」。`main.py` 新增 `_asr_off` 状态：
  `_toggle_asr()` 暂停时置位并清除屏幕上残留的该条提示；`_start_backends()`（启动路径）与
  `_on_asr_failed("model-path-empty")`（信号路径）两处设置提示的入口都加了 `not self._asr_off`
  守门，恢复 ASR 时清零。

**变更**

- 翻译失败提示条文案由「翻译服务不可用，暂时显示原文」简化为 **「翻译服务不可用」**
  （英文 `Translation service unavailable`），见 `src/ui/overlay.py`。
- `translator.status_changed` 携带的原始端点报错（如 `libretranslate: …`）在任何配置下都不再
  进入提示条；错误详情仍走 `logger.warning`，需要细节时看控制台。

**验证**（本机无头运行，全部通过）

- 新增套件 `_v0110_ui_hint_check.py`：**15/15**。覆盖双语文案取值、翻译失败四种状态 +
  恢复路径、ASR 启停两向、启动路径守门、OCR 提示回归、`src/ai`+`src/screen` 冻结时间戳核对。
- 既有套件无回归：BL-1 双语 20/20、BL-3 23/23、BL-5 40/40、0.1.6 守门、四轮验收、托盘 18/18、
  0.1.9 内置模型 13/13。
- 静态审计：files 29 / py 18 / 语法错 0 / 导入边 27 / 环 0 / 禁用词 0 /
  出站域名 3（api.mymemory.translated.net、translate.astian.org、translate.googleapis.com）。

**依赖**：无新增、无升级、无移除（与 0.1.9 完全相同，见 requirements.txt）。

**产物**（本机实测）

- `dist\SubMirror-0.1.10-portable.zip`：281,368,886 B / 1035 entry / `testzip` 无损坏，
  SHA-256 `4FFB9FAD406C987CF9E7D7375113D14B702B4165920AADFDE63BD12E75214235`；
- 包内 `SubMirror.exe`：10,274,235 B，SHA-256 `5397DE8E6442CBB444817EC15BCBA6FC02EF5E77D1FB64CBC16E5738A0B52B14`；
- 包内 Whisper 模型位于 `models\faster-whisper-base`（5 文件 / 147,884,932 B），
  包根目录附 `start_submirror.bat`（首次启动写入 `asr.model_path` 并启动程序）。

**已知限制**：与 0.1.9 相同——安装包未做代码签名；WASAPI 环回实采与 `SubMirror.exe` 冒烟启动
需在装有完整依赖的目标机复验（本机被 WDAC / Application Control 拦截）。

**延期项**：无新增。

---

## [0.1.9] - 2026-10-16 — 便携包内置 Whisper base 模型 + 首次启动配置脚本

**交付范围**：便携版不再要求用户自行准备语音模型。

- **内置模型**：`Systran/faster-whisper-base`（CTranslate2 格式，5 个文件，
  `model.bin` 145,217,532 B，整目录 147,884,932 B）随便携包一起分发，落在
  `models\faster-whisper-base\`。
- **首次启动脚本**：`start_submirror.bat` 把内置模型路径写进
  `%APPDATA%\SubMirror\config.json` 的 `asr.model_path`，然后启动 `SubMirror.exe`。
  **消除 ASR 提示** —— 不再出现「语音识别不可用：请在设置中选择 Whisper 模型目录。」
- **打包脚本**：`build_portable.bat` 新增两个环境变量
  `SUBMIRROR_PORTABLE_MODEL` / `SUBMIRROR_PORTABLE_EXTRAS`，按需把模型与脚本复制进便携包；
  两者都默认不设置，仓库文件清单与静态审计文件数保持 29 不变。

**未改动的部分**：OCR 链路（`src/screen/winrt_ocr.py`）、翻译链路（`src/ai/*`）、
ASR 核心（`src/audio/*`、`main.py::AsrEngine`）**一行未改**；`main.py` 也未修改。
本次只新增/修改打包脚本与便携包内容。

**已验证**（无头，本机离线，`_v019_asr_bundle_check.py` 13 项全过）

| 项目 | 证据 |
|---|---|
| 内置模型是合法 CTranslate2 模型 | 应用自带 `validate_model_dir()` 返回 `(True, "ok")` |
| `AsrEngine.load()` 用内置目录成功加载 | `load_error == ""`，加载耗时 < 1 s |
| 真实识别（英文语音样本） | `Hello, this is a test of the subtitle translation pipeline. The quick brown fox jumps over the lazy dog.`（逐字一致） |
| 真实识别（中文语音样本） | `大家好,这是一个字幕翻译馆线的测试 今天天气很好` |
| ASR 提示消失 | 有内置路径时 `AsrService` 不再发 `model-path-empty` |
| 反向对照 | 路径置空后仍会发 `model-path-empty`（证明该断言有效） |
| 启动脚本写配置 | exit 0；`asr.model_path` 正确，其余 5 个键原样保留；**不写 BOM** |
| 启动脚本首次运行 | 无配置文件时能从零创建 |
| 启动脚本防破坏 | 配置是坏 JSON 时**保留原文件**、打印警告、`--no-launch` 下 exit 1 |
| 启动脚本参数 | `--help` exit 0、未知参数 exit 2、缺 `SubMirror.exe` exit 1 |

**已知限制**

- 内置的是 **base** 模型（约 141 MB，识别质量低于 small/medium）。追求质量仍可在设置面板
  指向更大的模型目录。
- 便携包体积从 148,025,258 B 增至 281,367,909 B（模型本身占 147,884,932 B）。
- 本机 Application Control 策略仍拦截未签名 exe 与原生负载，`SubMirror.exe` 冒烟启动
  与 WASAPI 环回实采仍需在目标机复验。

### 打包（本机实测）

| 产物 | 体积 | SHA-256 |
|---|---|---|
| `dist\SubMirror-0.1.9-portable.zip` | 281,367,909 B（1035 entry，`testzip` 无错误） | `0F7CF70B7C12691BD3F1892294FBF910BF11E127CFE8F8FE2A503EC0475F0B6B` |
| `dist\SubMirror-portable\SubMirror.exe` | 10,273,562 B | `384A6144088471B1FBDF7190CA645C0F1E439564775D5578E478B31D87F8F3FC` |
| `dist\SubMirror-portable\models\faster-whisper-base\`（5 文件） | 147,884,932 B | 随包分发 |

包内 `README.md` 与仓库工作副本逐字节一致；`start_submirror.bat` 与 5 个模型文件都已确认在归档内。

校验日志：`submirror_verify_log.md` = 48,157 B，八套无头套件全绿 —— BL-3 23/23、BL-2 34/34、BL-5 40/40、0.1.6 守门 7/7、第一轮 28/28、第二轮 12/12、入口回归 8/8、第三轮 15/15、第四轮 11/11、托盘 18/18、**内置模型 13/13**；静态审计 29 文件 / 18 py / 0 语法错 / 27 导入边 / 0 环 / 0 禁用词 / 3 出站域名。

## [0.1.8] - 2026-10-16 — 系统托盘完善（显示/隐藏悬浮窗、重置、启停 OCR、状态点）

> 承接 [0.1.7]：0.1.7 已冻结为正式开源发布基线，本版在其之上补齐**系统托盘**，是当前开发版本。
> 0.1.7 的发布产物与 SHA-256 记录仍然有效，未被本版覆盖。

### 交付范围

| 项 | 说明 |
|---|---|
| 右键菜单 | 显示/隐藏悬浮窗、打开设置、重置悬浮窗、暂停/启用 OCR、关于、退出 |
| 左键单击 | 切换悬浮窗显示 / 隐藏（右键不再触发） |
| 图标状态点 | **绿点 = OCR 运行**，**灰点 = OCR 已暂停**；图标运行时绘制，仓库无需图标资源 |
| 托盘提示 | 悬浮提示随状态变化，并跟随界面语言（中/英双语文案） |
| 无障碍降级 | 未安装 Windows OCR 语言包时，OCR 菜单项**置灰禁用**，不会误导用户 |

### 修复的缺陷

- **托盘图标从未被创建**：`TrayController` 提供 `start()`，但 v0.1.7 全程序**没有任何调用点**，
  因此 README 交付范围表里声明的「托盘」实际上只存在于代码中——菜单、气泡、退出入口全部不可达。
  0.1.8 在 `SubMirrorApp._start_backends()` 中显式启动托盘，并只在成功后同步状态与提示。
- **README 与实现不一致**：文档写「托盘图标 → 重置字幕位置」，但当时既没有托盘图标、也没有该菜单项。
  0.1.8 补齐「重置悬浮窗」菜单项后，文档同步改为单击/右键的真实行为。

### 实现要点

- OCR 启停是**应用层闸门**（`OcrService.set_enabled()`）：暂停时不截图、不投递 OCR 任务、不推字幕，
  并忽略区域/全屏两个热键；**Windows OCR 引擎与识别代码完全未改动**，因此恢复是瞬时的、无需重新加载。
- 暂停会主动取消进行中的框选，避免留下一个全屏遮罩窗口。
- `src/ocr`、`src/ai`、`src/audio` 三个核心目录**零改动**；本版改动集中在 `main.py`（装配与托盘）。

### 已验证

| 套件 | 结果 |
|---|---|
| 托盘无头验收 `_tray_check.py`（新增） | **18 / 18** |
| 运行时验收第一轮 `submirror_runtime_accept.py` | 28 / 28 |
| 运行时验收第二轮 `submirror_runtime_accept2.py` | 12 / 12 |
| 运行时验收第三轮 `submirror_runtime_accept3.py` | 15 / 15 |
| 入口回归 `_v017_startup_check.py` | 8 / 8 |
| 翻译生命周期守门 `_v016_translator_check.py` | 7 / 7 |

托盘套件在离屏 Qt 下把 `QSystemTrayIcon.isSystemTrayAvailable()` 打桩为 True（离屏平台没有托盘宿主），
并用**独立互斥体名**运行，以免与本机正在运行的 SubMirror 实例互相干扰。

### 打包

`build_portable.bat` 已用 0.1.8 源码重建便携版（退出码 0）：

| 产物 | 大小 | 说明 |
|---|---|---|
| `dist\SubMirror-0.1.8-portable.zip` | 148,025,258 B | 1028 个 entry，`testzip` 无错误 |
| `dist\SubMirror-portable\SubMirror.exe` | 10,273,562 B | 需连同 `_internal\` 整目录拷贝 |
| zip SHA-256 | `5F7CB2BFE4EDC7009E427136ABFE0276A1686619B3A6938126976D3C68CF64F6` | |
| exe SHA-256 | `92FB4BD33645EE1334071702D2F368DF7B56F72AB746F67013C7876EB045EB4C` | |

包内根目录为 `SubMirror.exe` + `README.md` / `PRIVACY.md` / `LICENSE` / `requirements.txt`，
其中 `README.md` 与仓库逐字节一致；`assets\slang_dict.json` 等资源位于 `_internal\` 下。
安装包仍需在无 Application Control 策略的目标机用 NSIS 编译。

### 测试基建修正（非产品缺陷）

本机有一个**用户自己启动的 SubMirror 实例**长期持有 `Local\SubMirror.SingleInstance.v1`
与三路全局热键，导致启动真实程序的验收套件首次重跑时出现 8 项假失败（`already-running`、
`RegisterHotKey ... winerror=1409`）。这些**不是产品缺陷**：在把单实例互斥体换成私有名的
`_iso_run.py` 包装下，同样的套件全部通过（入口回归 8/8、第三轮 15/15）。校验日志生成脚本
已改为经该包装器运行这四套启动真实程序的套件，避免把「本机已有实例」误判为回归。

## [0.1.7] - 2026-10-16 — 启动链路 P0 修复（首次启动即自己退出）

> **发布基线（已冻结）**：本版本为**正式开源发布基线**，不再接受功能性变更。验收 ✅ 62 / ⏳ 0 / ⏸ 2（共 64），
> 四轮无头运行时验收全部通过。BL-6（关于页可点击外链、环回设备下拉）**不纳入本次发布范围**，
> 作为后续版本（v0.2）的迭代功能。
>
> 便携版产物（已完成结构与内容核验）：
>
> | 产物 | 大小 | SHA-256 |
> |---|---|---|
> | `dist\SubMirror-0.1.7-portable.zip`（1014 entry，testzip 通过，内嵌 README 与仓库逐字节一致） | 147,384,151 B | `AF578461BF201FE3FE83E6CD7F9287C822116ACDBACD1E606DABA011944FE44D` |
> | `dist\SubMirror\SubMirror.exe`（安装包打包源） | 9,823,451 B | `870D5DA7C587253914ED76F371BBDCA23D07BAEF70D464CBD1CC3889A093E576` |
>
> 安装版（`makensis` 编译）与三项人工复验（WASAPI 环回实采、Whisper 本地模型加载、`SubMirror.exe`
> 冒烟启动）需在无 Application Control 策略的 Windows 目标机执行，命令与回填要求见 README「v0.1.7 发布清单」。
> 安装包编译命令为 `makensis /DDIST_DIR="dist\SubMirror" /DAPP_VERSION="0.1.7" build_installer.nsi`，
> `DIST_DIR` 必须指向 PyInstaller 原始输出目录 `dist\SubMirror`（不含便携版附加的四个分发文档副本）；
> 本机 `makensis.exe` 已下载但被 Application Control 策略阻止执行，故未能编译。

0.1.6 之后的第三轮运行时验收换了打法：不再只测组件，而是**跑真正的入口** —— 在离屏模式下把
`main.main()` 完整走一遍（`init_app_context → SubMirrorApp.start → 事件循环 → shutdown`）。
第一次执行就抓到「整个程序根本起不来」的 P0。

### 交付范围

| 文件 | 变化 |
|---|---|
| `src/core/context.py` | `AppContext.initialize()` 改为**幂等**：已初始化时直接返回缓存的成功结果 |
| `D:\Downloads\_v017_startup_check.py` | 新增：入口回归（真实 `main()` 返回 0 且打印 started / 幂等性 / 真实第二实例仍被拒绝） |
| `D:\Downloads\submirror_runtime_accept3.py` | 新增：第三轮无头验收 15 项（真实启动、悬浮窗、热键、OCR、缺音频降级、原文+译文、退出线程回收） |

### 本次修复的缺陷

**[P0] 首次启动就被自己判成「已有实例在运行」，程序直接退出**

- 现象：`python main.py`（以及打包后的 `SubMirror.exe`）启动后立即结束，日志依次是
  `application context initialized` → `another SubMirror instance is already running`
  → `another SubMirror instance is already running; activating it`。
- 根因：`init_app_context()`（`src/core/context.py:473`）**内部已经调用过**
  `context.initialize()`；`main.py:1681-1682` 拿到同一个对象后又调用了一次。第二次执行
  `SingleInstanceGuard.acquire()` 时 `CreateMutexW` 返回 `ERROR_ALREADY_EXISTS`，
  而这个已存在的互斥体**正是本进程第一次调用时创建的**，于是首启被误判为“第二个实例”，
  直接走 `EXIT_ALREADY_RUNNING` 分支返回。
- 影响：字幕链路、OCR、悬浮窗、全局热键一律不会启动；打包产物表现为「双击没反应」。
- 修法：`AppContext.initialize()` 变为幂等（`self._initialized` 为真时返回 `(True, INIT_OK)`），
  并在注释里写明原因，避免以后又被“优化”回去。
- 复验：修复前实测入口直接退出并打印 phantom 行；修复后 `main()` 返回 0，日志出现
  `SubMirror 0.1.6 started (language=zh-CN)`、三条管线启动、1.5 秒后 `SubMirror stopped after 1.5 s`。
  入口回归 8/8：幂等、真实第二互斥体仍被拒绝、shutdown 后可重新获取。

### 变更 / 依赖

- 依赖无变化。单实例语义不变：真正的第二个实例依旧被拒绝并唤起第一个。
- 版本号：`APP_VERSION` / NSIS / README / PRIVACY 同步 0.1.7。

### 已验证（本机运行时）

| 组 | 脚本 | 结果 |
|---|---|---|
| 入口回归 | `_v017_startup_check.py` | 8/8 |
| 第三轮无头验收（真实启动链） | `submirror_runtime_accept3.py` | 15/15 |
| 第四轮补测（真实 TTS 语音 + 真实公网） | `_v018_external_check.py` | 11/11 |
| 第一 / 二轮无头验收（回归） | `submirror_runtime_accept.py` / `submirror_runtime_accept2.py` | 28/28 · 12/12 |
| 翻译快速守门（回归） | `_v016_translator_check.py` | 7/7 |

静态回归维持全绿：`py_compile` 18/18、模板↔schema 94:94、双语 167:167、运行时断言 20/20、
BL-3 行过滤 23/23、BL-2 热键 34/34、BL-5 页签 40/40、死控件 0、静态审计 29 文件 / 18 py /
0 语法错 / 27 导入边 / 0 环 / 0 禁用词 / 3 个出站域名、`import main` OK。

打包产物：`build_portable.bat` 已用 0.1.7 源码重建 `dist\SubMirror-0.1.7-portable.zip`
（147,381,665 B，1014 个 entry，`testzip()` 无损坏；内含 `SubMirror.exe` 9,823,451 B +
`_internal` 1009 个 entry + 5 个根文件 LICENSE / PRIVACY.md / README.md / requirements.txt）。
0.1.7 补测后又重建过一次，使随包 `README.md` 与仓库一致（脚本比对两端 SHA-256 相同）；
原生负载（`_portaudiowpatch.cp312-win_amd64.pyd`、`ctranslate2.dll`、`onnxruntime`、`av`、
`soundcard`、657 个 `winsdk` 条目）均在包内。
需要说明的是：本机启用了 Application Control 策略，**新建的可执行文件被禁止启动** ——
`Start-Process` 直接返回 `An Application Control policy has blocked this file`，所以冻结后的
`SubMirror.exe` 未能在本机实跑；入口回归是在源码上、用与打包相同的虚拟环境完成的。
旧的 0.1.5 / 0.1.6 zip 已从 `dist` 清理。

### 补测（第四轮运行时验收：真实 TTS 语音 + 真实公网端点）

第四轮不再造模拟数据，而是**用真的东西**：本机 Windows SAPI 合成两段真人语音（英文 Zira、中文 Huihui）
作为音频源，并用**真实公网**调用翻译端点。11 项全部通过，**未发现产品缺陷**；剩余 5 项 ⏳ 全部转 ✅。

| 验收项 | 补测方式 | 结果 |
|---|---|---|
| 三.A.1 VAD 识别人声 / 静音不触发 | 真人语音样本按 100 ms 采集块送入 `VoiceActivityDetector.push()` | 英文 3 段 / 中文 3 段；数字静音与房间噪声 0 段 0 语音块 |
| 二.3.1 语种切换 | `en→zh-CN`、`zh-CN→en` 真实翻译 | 双向均得到目标语言译文 |
| 二.3.2 三端点故障转移 | 真实网络（libretranslate DNS 失败 → google-gtx 超时 → mymemory 成功） | 失败端点按设计轮转，成功端点上移粘性优选 |
| 三.A.3 联网后原文 + 译文同屏 | 真实译文经 `display_texts(show_original=True)` | 同时返回原文行 + 译文行 |
| 四.4 三域名不可达的彻底阻断 | 以本机确实不可达的域名等价替代 hosts 封禁 | 3 次失败均保留原文、屏幕不空白；第 4 次 `circuit-open` |

两处初始失败均定位为**验收脚本自身的用词错误**（语言词表应取面板枚举 `zh` 而非规范码 `zh-CN`；
按设计「连接超时」不计入 `endpoint_failures`，需用 DEBUG 日志证明端点确实被调用过），不是产品缺陷。

补测脚本 `D:\Downloads\_v018_external_check.py` 为**纯标准库**实现（不需要 Qt / 网络库 / numpy），
音频用 stdlib `wave` + `array` 读取后按采集块大小喂给 VAD。

### 已知限制 / 延期项

- **WASAPI 环回实采**（`pyaudiowpatch` / `soundcard` 原生负载）、**Whisper 本地识别**
  （`ctranslate2` / `onnxruntime`）与**安装版打包**仍需在无 WDAC 的机器上完成
  （本机 Application Control 策略同样拦截新建 exe 的启动）。
- 三.A.1 的音频来源为离线语音样本，已按生产块大小验证 VAD 判决；实时采集一路待目标机复验。
- 二.2.1 环回设备下拉、二.8.1 关于页外链（BL-6）。

---

## [0.1.6] - 2026-10-16 — 运行时验收第二轮（翻译生命周期两个真缺陷）

0.1.5 首次在无头环境跑通 OCR 全链路后，本轮把运行时验收扩展到**翻译生命周期与边界行为**
（新增 `submirror_runtime_accept2.py` 12 项 + `_v016_translator_check.py` 7 项快速守门），
又抓到两个只有在运行时才暴露的真缺陷。

### 交付范围

| 文件 | 变化 |
|---|---|
| `src/ai/base_translator.py` | 修复失败分支的未绑定变量；`close()` 现在清空翻译缓存 |
| `D:\Downloads\submirror_runtime_accept2.py` | 新增：第二轮无头验收（免责弹窗、重置默认、模型目录预检、采样上限、选区释放、区域抓取、失败回退、缓存清理、熔断、拖动保存、热键 25 轮启停、退出线程回收） |
| `D:\Downloads\_v016_translator_check.py` | 新增：无需 Qt/网络的 7 项快速守门（失败永不抛异常、超时、尝试计数、熔断、close 清空缓存、close 幂等、同语言直通） |

### 本次修复的缺陷

**[P0] 翻译失败会抛 `UnboundLocalError`，打穿字幕通道**

- 现象：后端一旦失败（断网、超时、HTTP 错误），`BaseTranslator.translate()` 抛
  `UnboundLocalError: cannot access local variable 'used'`，而该方法契约明确写「永不抛异常，失败装在结果里」。
- 根因：分块循环里 `translated_chunk, used = self._translate_with_retry(...)` 只在**成功路径**绑定 `used`，
  而 `except TranslationTimeoutError / TranslationError / Exception` 三个处理分支都要读 `attempts + used`。
- 修法：每个分块进入 `try` 之前先 `used = 0`。
- 复验：修复前 4 连败即抛异常；修复后第 4 次正确返回 `circuit-open`（`retry in 10.0s`），失败结果保留原文。

**[P1] `close()` 从不清空翻译缓存**

- 现象：`close()` 之后 `stats()["cache"]["entries"]` 仍为 1，缓存中的用户文本留在内存里，
  与验收项二.7.3「退出清空缓存」不符。
- 根因：`close()` 只置 `_closed` 并调用空的 `_on_close()` 钩子，从未触碰 `self._cache`。
- 修法：`close()` 在 `_on_close()` 之后调用 `self._cache.clear()`（带异常兜底）。
- 复验：缓存条目 `1 → 0`，`close()` 幂等，之后调用返回 `translator closed`。

### 变更

- `close()` 之后 `translate()` 仍返回失败结果，不会重新建立缓存。
- 版本号：`APP_VERSION` / NSIS / README / PRIVACY 同步 0.1.6。

### 依赖

无变化。

### 已验证（本机运行时，非静态推断）

| 组 | 脚本 | 结果 |
|---|---|---|
| 快速守门 | `_v016_translator_check.py` | 7/7 |
| 第一轮无头验收（回归） | `submirror_runtime_accept.py` | 28/28 |
| 第二轮无头验收 | `submirror_runtime_accept2.py` | 12/12 |

静态回归维持全绿：`py_compile` 18/18、模板↔schema 94:94、双语 167:167、运行时断言 20/20、
BL-3 行过滤 23/23、BL-2 热键 34/34、BL-5 页签 40/40、死控件 0、静态审计 29 文件 / 18 py /
0 语法错 / 27 导入边 / 0 环 / 0 禁用词 / 3 个出站域名、`import main` OK。

### 打包产物（本机实测）

用修复后的源码重建便携版（`cmd /c "build_portable.bat /PYTHON ""D:\Downloads\smbuild\venv\Scripts\python.exe"""`，退出码 0）：

- 目录：`dist\SubMirror-portable\`（PyInstaller onedir，约 384 MB，含 `assets\slang_dict.json`）
- 压缩包：`dist\SubMirror-0.1.6-portable.zip`（**143,794 KB / 约 140.6 MB**，22:09 产出）
- 安装版仍未产出：本机 makensis 被 WDAC 阻止，须在正常策略机器上执行 `build_installer.nsi`。

### 已知限制 / 延期项

- 本机 WDAC/Application Control 仍阻止未签名原生负载（`pyaudiowpatch` / `ctranslate2` / `onnxruntime`）
  与打包产物执行，因此真实音频、真实模型、真实网络与 exe 冒烟仍需在正常策略的机器上完成。
- 二.2.1 环回设备下拉、二.8.1 关于页外链（BL-6）仍未做。

---

## [0.1.5] - 2026-10-16 — 运行时验收修复（OCR 链路与配置单例）

BL-5 之后进入**目标机打包与运行时验收**环节。本机在无头模式下跑通 28 项运行时检查
（`D:\Downloads\submirror_runtime_accept.py`）时，暴露出三个只有真正执行代码才会显现的缺陷。
本版**不新增功能**，只修缺陷。

### 交付范围

| 文件 | 变更 |
| --- | --- |
| `src/core/config.py` | 配置单例锁改为可重入（修复首次调用自死锁） |
| `src/screen/winrt_ocr.py` | 修正 WinRT 枚举成员解析、修正成功结果构造参数 |
| `src/core/config.py` | `APP_VERSION` → `0.1.5` |

### 本次修复的缺陷

**[P0] OCR 永远拿不到任何文本**
- 现象：`OcrService` 每帧都返回空结果，字幕里的 OCR 通道完全不出字幕。
- 根因：`WinrtOcrEngine._make_bitmap()` 里写死了 `BitmapPixelFormat.Bgra8` 与
  `BitmapAlphaMode.Premultiplied`；实际投影（winsdk）的成员名是全大写
  `BGRA8` / `PREMULTIPLIED`，于是抛 `AttributeError`，被同一处的宽 `except` 静默吞掉，
  退化到 `_bitmap_from_stream()` 兜底也失败，`_make_bitmap()` 返回 `None`，
  `recognize()` 直接返回“无法转换为 SoftwareBitmap”的空失败结果。
- 修法：新增 `_enum_member(enum_class, *names)` 按名解析（同时接受两种拼写），
  取不到时记 `warning` 而不是静默；兜底路径的日志级别从 `debug` 提到 `warning`。
- 复验：同一张 GDI 渲染图，修复前 `lines=0`，修复后 `lines=1 text='SUBMIRROR TEST 123'`。

**[P0] OCR 成功结果被构造参数打回失败**
- 现象：即使位图修好，`recognize()` 仍返回 `ok=False`。
- 根因：成功分支用 `WinrtOcrResult(..., origin_ok=True, ...)` 构造，而
  `WinrtOcrResult` 没有 `origin_ok` 字段 → `TypeError` 被 `recognize()` 的宽 `except`
  捕获，把成功转成失败（`WinrtOcrResult.failure("TypeError: ...")`）。
- 修法：去掉该参数（`ok` 默认即为 `True`）。
- 复验：修复前 `C engine.recognize() -> ok=False lines=0`，修复后
  `ok=True lines=1 text='SUBMIRROR TEST 123'`。

**[P0] 首次调用 `get_config()` 自死锁**
- 现象：任何以 `get_config()` 作为首次配置访问的代码路径都会永久挂起（实测 >90s 无返回）。
- 根因：`src/core/config.py:1255` 的 `_INSTANCE_LOCK = threading.Lock()` 不可重入，
  而 `get_config()` 持锁后调用同样要取这把锁的 `init_config()`。
- 修法：改为 `threading.RLock()` 并加注释说明为什么必须可重入。
- 复验：`get_config()` 幂等、`reset_singleton()` 后再次调用均即时返回。
- 说明：正常启动路径被 `src/core/context.py:404` 自建 `ConfigManager` 掩盖，因此日常运行
  不会复现；但 `main.py:1692`（初始化失败要弹致命对话框的分支）与任何测试/工具都会中招。

### 验证（本机无头运行时验收）

- `submirror_runtime_accept.py`：**28 项检查 0 失败**（1 启动 / 2 设置 / 3 链路 / 6 边界）。
- OCR 真实链路：GDI 渲染图 → 引擎识别，`text='SUBMIRROR TEST 123'`（31ms，语言 zh-Hans-CN），
  中文图识别为 `字 幕 测 试 3`（中文模型不认拉丁数字，属模型行为）。
- 界面：设置面板 9 页签、运行时中英切换、`collect()` 44 键（42 点号键）全部落盘。
- 边界：空配置加载、损坏配置修复、非法值降级为 partial、悬浮窗开合、热键注册、
  ASR 启停无服务时安全 no-op、屏幕抓取 mss 可用。
- 静态复核：29 文件 / 18 py / 语法错 0 / 导入环 0 / 禁用词 0 / 出站域名仍 3 个。

### 已修复后仍未消除的环境限制

- 本机启用 WDAC/Application Control，**新装的未签名原生二进制被系统阻止**，
  因此打包出的 `SubMirror.exe`、NSIS `makensis.exe` 在本机都无法执行；
  便携版 zip 可以产出，安装版必须在策略宽松的机器上打包。
- 音频采集/ASR 真链路（winsdk 之外的 `pyaudiowpatch`、`ctranslate2`、`onnxruntime`）
  在本机被策略阻止，只能做到导入级与逻辑级验收。

### 打包产物（本机实测）

- 便携版**已在本机产出**：`dist\SubMirror-portable\`（PyInstaller onedir，约 384 MB）与
  `dist\SubMirror-0.1.5-portable.zip`（约 140 MB）。构建命令：
  `build_portable.bat /PYTHON "<venv>\Scripts\python.exe"`（脚本从 `src/core/config.py` 读取
  `APP_VERSION`，产物名自动跟随版本号）。
- 构建过程出现两类可复现的 WARNING，均由本机 WDAC 策略阻止未签名原生 DLL 引起
  （`numpy._mt19937`、`ctranslate2`、`pyaudiowpatch`、`soundcard`、`av`）。这些负载实际已随包复制，
  但**完整性未获保证**，应在无 WDAC 的机器上重建一次再对外发布。

### 延期项

- BL-4 已完成；BL-6（环回设备下拉 / 关于与隐私页跳转链接）仍待排期。
- 安装版产物、以及需要真实音频设备的 17 项 ⏳ 运行时验收，需在有正常策略与声卡的机器上完成
  （0.1.7 补测后其中网络与语音类 5 项已转 ✅，⏳ 归零，见 [0.1.7] 段）。

---

## [0.1.4] - 2026-10-16 — BL-5 设置面板九页签拆分

BL-2 之后按迭代排期执行 **BL-5**：把设置面板从 8 页拆为验收清单要求的 **9 页**，
并新增「关于与隐私」页。按用户指定口径，本批**只做页面分组重组**，
不改动业务逻辑、配置键与过滤/热键代码。

### 交付范围

| 文件 | 改动 |
|---|---|
| `src/core/config.py` | 新增 15 个双语键：9 个 `settings.tab.*` 页签标题 + `settings.group.queue` / `cache` / `resilience` / `about` / `privacy` + `settings.privacy_note`；既有 `settings.group.*` 文案保持不变 |
| `src/ui/settings_ui.py` | `_build_ui` 改为 9 个 `addTab`（顺序 = 通用 / 全局热键 / 音频·ASR / 翻译 / OCR / 悬浮窗样式 / 队列与限流 / 高级选项 / 关于与隐私）；新增 `_build_queue_tab` / `_build_advanced_tab` / `_build_about_tab`；删除 `_build_v02_tab` / `_build_maintenance_tab`（内容并入关于页）；`retranslate()` 末尾新增 `_refresh_about_version()` |
| `src/core/config.py` | `APP_VERSION` → `0.1.4` |

### 行为说明

* **控件只移动，不新增/不删除**：所有控件的属性名与 `collect()` 键集合保持不变
  （44 键 = 42 个点号键 + `ui_language` / `fps_safe_mode`），拆页前后逐一对照一致。
* 新页归属：「队列与限流」收纳 `subtitle.queue_max` / `subtitle.timeout_seconds` 与 `translate.cache_*`；
  「高级选项」收纳翻译容错三项（`translate.timeout_seconds` / `failure_threshold` / `circuit_break_seconds`）
  与音频三项（`audio.spike_filter_ms` / `reconnect_attempts` / `silence_log_seconds`）；
  「关于与隐私」收纳版本号、免责声明、隐私说明、v0.2 占位与维护入口。
* 隐私说明文本明确：设置与字幕只存本机 `config.json`；仅启用翻译时把待翻译文本发往
  已配置的公共翻译端点；不采集遥测、不上传截图、不访问麦克风。

### 依赖

无变化。

### 已验证（静态）

1. 新增断言脚本 `_bl5_tabs_check.py`：**40/40 通过** —— 9 个 `addTab` 顺序与数量、
   9 个构造器各定义一次、旧 `_build_v02_tab` / `_build_maintenance_tab` 已移除、
   15 个新双语键在中英两侧均存在、`collect()` 键集合 missing=none / extra=none / 计数 44==44。
2. `py_compile` 全项目 18/18；静态审计 29 文件 / 18 py / 语法 0 / 导入边 27 / 环 0 / 禁用残留 0 / 出站域名 3。
3. `_bl1_attr_check.py`：`settings_ui.py` 130 处 `self._` 赋值 / 121 处读取，**reads never assigned = 0**。
4. `_bl1_cfg_check.py` 94:94 `RESULT OK`；`_bl1_lang_check.py` 167:167 `RESULT OK`；
   `_bl1_runtime_check.py` 20/20；`_bl3_filter_check.py` 23/23；`_bl2_toggle_check.py` 34/34；死控件 0。
5. `import main` 冒烟通过。

### 已知限制 / 延期项

* 面板改动**未在目标机做运行时验证**（开发机未安装 PySide6），页签切换与语言重译待目标机确认。
* 「关于与隐私」页的**可点击外链入口**（打开仓库 / `PRIVACY.md` / 再次查看免责声明）
  与「音频·ASR」页的**环回设备下拉**、以及把已声明未暴露的键放进「高级选项」页，
  均属新增 UI 逻辑，已拆为 **BL-6**（对应验收项 二.8.1、二.2.1）。
* 打包产物仍未产出（开发机无 PyInstaller / makensis），需目标机执行。

---

## [0.1.3] - 2026-10-16 — BL-2 ASR 启停全局热键

0.1.2 之后按迭代排期执行 **BL-2**：新增一个**可选的**全局热键，用于暂停 / 恢复语音识别链路。
该功能为**新增功能**（不是接线），因此除接线外也改动了热键冲突判定。

### 交付范围

| 落点 | 改动 |
|---|---|
| `src/core/hotkey.py` | 新增热键槽位 `HOTKEY_TOGGLE_ASR`（`0xA003`）与 `asr_toggled` 信号；`check_hotkey_conflict()` 由两两扩展为**三路两两**比较（第三个参数可选，空值/无效键被忽略，旧的两参调用行为不变）；`_register_from_config()` 只在配置了按键时才注册第三路，留空不会被当成注册失败 |
| `src/core/config.py` | `DEFAULT_CONFIG` / `_SCHEMA` 93 → **94 键**（`hotkeys.toggle_asr`，默认空串 = 不绑定）；中英双语各补 2 条并改写热键冲突提示文案 |
| `main.py` | 新增 `_toggle_asr()` 与 `_notify_asr_state()`：运行时暂停 / 恢复 `AsrService`（暂停会释放环回设备），并在悬浮窗与托盘气泡给出本地化提示；新增 `name.toggle_asr`、`notice.asr_paused`、`notice.asr_resumed` 双语文案 |
| `src/ui/settings_ui.py` | 热键页新增「ASR 启停热键（可留空）」输入框与说明；`load_from_config` / `collect()` 接入（面板键 41 → **42**）；`validate()` 把该键纳入同一个唯一性校验，且**允许留空** |

### 行为说明

* 默认**不绑定**任何按键：升级安装不会抢占新的键位；留空时面板只显示说明，不报错、不阻止保存。
* 按下后：正在识别 → 暂停（释放音频设备，不再采集 / 识别 / 翻译）；已暂停 → 恢复。
* 恢复失败（例如音频设备被独占）**不弹提示**，只在日志中记录失败原因，避免失败提示刷屏。
* 保存设置时若三个热键中有任意两个相同，仍然**整个配置不落盘**并弹出冲突提示。
* 该热键与两个 OCR 热键同为 `MOD_NOREPEAT`，按住不会连续触发。

### 本次修复的缺陷

* `[P1] 空热键被 _coerce 判为非法 → 每次启动都会把默认配置判为「部分损坏」` —— 0.1.3 新增的
  `hotkeys.toggle_asr` 默认值为空串（= 不绑定），但 `src/core/config.py` 的 `_coerce()` 对
  `kind == "hotkey"` 一律走 `normalize_hotkey()`，空串返回 `None`，于是被当成「invalid hotkey format」
  计入坏值。若不管，用户每次启动都会看到「配置已重置」类提示（`LOAD_PARTIAL`），并让
  `submirror_verify_log.md` 的模板↔schema 断言长期报错。
  **修法**：`_coerce()` 的 hotkey 分支在规范化之前先处理空值 —— `None` 或纯空白一律返回
  `("", True, "")`，即「合法且表示不绑定」；非空值仍走 `normalize_hotkey()` 严格校验。
  **复验**：`_bl1_cfg_check.py` 由 `default_mismatch=1` 恢复为 `default_mismatch=0 / RESULT OK`
  （`default_keys=94 schema_keys=94`）。

### 版本号

`APP_VERSION`（`src/core/config.py`）与 `build_installer.nsi` 的默认 `APP_VERSION` 均升至 **`0.1.3`**；
打包产物名随之变为 `SubMirror-0.1.3-portable.zip` / `SubMirror-0.1.3-setup.exe`
（README 与 PRIVACY 中的版本引用已同步）。

### 已知限制

* 热键仍走**用户态 `RegisterHotKey`**（无键盘钩子、无注入）；若第三路按键已被其他程序独占，
  只会在启动时通过「热键注册失败」提示告知，需要用户改键。
* 本批改动仍未在装有 PySide6 的目标机做运行时验证（本机缺 GUI 依赖），静态与纯逻辑断言见校验日志。

### 延期项

BL-4（文档补充）与 BL-5（设置面板九页签拆分）仍未做，见 [ITERATION_BACKLOG.md](ITERATION_BACKLOG.md)。

---

## [0.1.2] - 2026-10-16 — BL-3 OCR 行过滤适配（方案 B）

0.1.1 之后按迭代排期执行 **BL-3** 的**方案 B**：保留置信度滑块，但把真正决定「这行要不要」
的判定改为**最小行高 + 最小字符数**的双过滤，并**兜底保底至少保留一行**识别文本。

### 交付范围

| 落点 | 改动 |
|---|---|
| `src/screen/winrt_ocr.py` | `filter_by_confidence()` 升级为 `filter_lines(min_height, min_chars, min_confidence, keep_best)`；新增 `_best_line()` 兜底；`recognize()` 在映射行之后调用引擎级 `_filter()` 并累计 `filtered` 统计；新增模块级 `_as_float` / `_as_int` 钳制helper |
| `src/core/config.py` | `DEFAULT_CONFIG` / `_SCHEMA` 90 → **93 键**（`ocr.confidence_min`、`ocr.min_line_height`、`ocr.min_line_chars`）；中英双语各补 4 条（150 : 150 对齐） |
| `src/ui/settings_ui.py` | OCR 页新增置信度滑块 + 最小行高 + 最小字符数三组控件（含「Windows OCR 不返回置信度」提示文案），接入 `load_from_config` / `collect()` |
| `main.py` | `OcrService.apply_config()` 现在会把配置转发给 OCR 引擎（**修复**：此前保存设置后引擎缓存的阈值不会更新） |

### 行为说明

* 任一阈值为 `0` 即关闭该项；三项全为 `0` 时**行为与 0.1.1 完全一致**（旧 `config.json` 无需迁移）。
* 行高未知（WinRT 未给出边框）的行**不因行高被丢弃**——未知不等于太小。
* `min_confidence` 只对**真的带分数**的行生效；Windows 内置 OCR 的行一律视为 unknown。
* 若三项过滤把全部行都判掉，则保留**信息量最大的一行**（先比行高、再比字符数）。
  `KEEP_BEST_LINE` 故意不做成配置项：可关闭的保证不是保证。

### 版本号

`APP_VERSION`（`src/core/config.py`）与 `build_installer.nsi` 的默认 `APP_VERSION` 均升至 **`0.1.2`**；
打包产物名随之变为 `SubMirror-0.1.2-portable.zip` / `SubMirror-0.1.2-setup.exe`
（README 与 PRIVACY 中的版本引用已同步）。

### 已知限制

置信度阈值对 Windows 内置 OCR 仍不生效（该引擎不返回置信度），这是 API 的硬限制而非实现缺陷；
实际过滤由同行的高/字符数完成。面板写出的配置键由 **38** 增至 **41**，且**全部**能被运行时读到。

---

## [0.1.1] - 2026-10-16 — BL-1 配置接线（设置面板可用性补齐）

在 0.1.0 基线上做**纯接线**：把「底层已在读、但配置模板与面板没有暴露」的参数接到位。
不改变核心链路行为，`asr.vad_min_speech_ms` 的默认值更正为运行时真实默认（行为等价）。

### 交付范围

| 落点 | 改动 |
|---|---|
| `src/core/config.py` | `DEFAULT_CONFIG` 45 → **90 键**；`_SCHEMA` 同步 90 键；中英双语 `lang_data` 各补 14 条（146 : 146 对齐） |
| `src/audio/vad.py` | 阈值/最小语音时长改读公开键 `asr.*`，旧 `audio.*` 名降为兼容别名；新增 `asr.vad_mode == "fps"` 预设 |
| `src/audio/capture_win.py` | 尖峰过滤 / 静音提示 / 重连次数改读公开键，旧内部名降为别名 |
| `src/ui/overlay.py` | 新增 `overlay.lock_position`，锁定后禁止拖动 |
| `src/ui/settings_ui.py` | 新增 7 组控件；模型路径非 ASCII 告警（不拒绝） |

### 版本号

`APP_VERSION`（`src/core/config.py`）与 `build_installer.nsi` 的默认 `APP_VERSION` 均升至 **`0.1.1`**；
`build_portable.bat` 从源码自动读取版本号，因此打包产物名随之变为 `SubMirror-0.1.1-portable.zip` /
`SubMirror-0.1.1-setup.exe`（README 与 PRIVACY 中的版本引用已同步）。

### 新增的面板控件

`asr.vad_threshold_db`、`translate.libre_base_url`、`translate.mymemory_email`、
`translate.cache_ttl_seconds`、`translate.cache_max_entries`、`subtitle.queue_max`、
`subtitle.timeout_seconds`、`overlay.text_color`（含取色按钮）、`overlay.lock_position`。

面板写出的配置键由 **31** 增至 **38**，且**全部**能被运行时代码读到（死控件数 0）。

### 行为变更 / 更正

- **`asr.vad_min_speech_ms` 默认值 `200` → `300`。** 原模板值与 `vad.py` 的运行时默认不一致，
  面板此前显示的是假默认；更正后模板、schema、面板、运行时四者一致。
- **`asr.vad_mode` 由「无任何运行时读取」变为 FPS 预设。** `fps` 时抬高起音时长与最小语音时长
  下限（`FPS_ONSET_MS_FLOOR=150`、`FPS_MIN_SPEECH_MS_FLOOR=300`），以过滤枪声类短促噪声。
  —— 该解释待用户复核是否即清单原意。
- **一个值只保留一个已声明键。** 公开名（`asr.*`、`audio.reconnect_attempts` 等）为准，
  运行时历史内部名仅在公开键缺失时兜底，手工改过 `config.json` 的用户不受影响。

### 依赖

无变化（未引入新第三方依赖；取色使用 PySide6 自带 `QColorDialog`）。

### 已验证（静态）

- 全项目 `py_compile`：**18/18 通过，0 失败**。
- 配置模板 ↔ schema：90 : 90，无单侧多余键，**每个声明默认值都能通过自己的 schema 回灌**（`default_mismatch=0`）。
- 双语键集合：`zh-CN` 146 : `en` 146，无单侧缺失、无空值。
- 运行时接线：**20/20** 断言通过（公开键生效、旧别名兜底、公开键优先、FPS 下限、锁定位置默认值）。
- 死控件扫描：面板写出的 38 个键**全部**被其他文件读取，死控件 **0**。
- 控件属性静态核查：18 个文件逐个检查 `self._x` 读取但从未赋值（拼写错误类 `AttributeError` 的静态替代），
  唯一非 OK 的 `src/audio/capture_win.py` 命中 `self._config` / `self._meter`，
  二者由基类 `src/audio/loopback.py:462` 与 `:468` 赋值，**属继承而非缺陷**。
- `import main`、静态审计（导入环 0 / 禁用残留 0 / 出站域名仍 3 个 / 无日志文件）全部通过。

### 已知限制 / 延期项

- **【二】页签结构仍为 8 页**，清单要求 9 页 → 转 **BL-5**（本次只把控件挂进现有页，未拆页）。
- **`toggle_asr` 热键**（BL-2）与 **WinRT 置信度滑块**（BL-3）仍未做，理由见 ITERATION_BACKLOG.md。
- 面板改动**尚未在目标机做运行时验证**（本机缺 PySide6）：保存往返、取色按钮、告警弹窗、
  9 页拆分的可视确认都要在装有 PySide6 的机器上完成。

---

## [0.1.0] - 2026-10-16 — 基础稳定版（Baseline）

首个交付版本。**核心识别、翻译与隐私合规的 P0 主流程已通过静态核验**；设置面板的次要 UI 配置项、
一个新增热键与一项 API 适配延期到后续迭代，不阻塞本基线交付。

### 交付范围

| 链路 | 内容 |
|---|---|
| 音频 | WASAPI 扬声器环回采集 → 环形缓冲 → 尖峰过滤 → VAD 语音端点检测 → Faster-Whisper 本地识别 |
| 屏幕 | 区域拖拽选框流式 OCR + 全屏单次快照 OCR（Windows 内置 WinRT OCR，`Windows.Media.Ocr`） |
| 翻译 | 三方免密钥公共端点故障转移；3 秒总预算、连续 3 次失败熔断 10 秒、内存缓存 |
| 文本闸门 | NFKC 规范化、零宽/控制字符清理、无效结果突发限流、200 字符截断 |
| 界面 | 三行悬浮字幕窗（ASR / TTS 占位 / OCR）、8 页设置面板、托盘菜单、单实例、免责门禁 |
| 打包 | 便携版脚本 `build_portable.bat` + NSIS 安装脚本 `build_installer.nsi`（按用户安装） |
| 合规 | 仅 3 个翻译域名出站、无遥测、无 Cookie/设备标识、无日志文件、不访问麦克风 |

### 本次修复的缺陷

相对首次整机静态核验（问题清单）修复如下，全部已复验：

- **[P0] 屏幕 OCR 链路整体失效。** `main.py` 引用了一个已被移除模块中的引擎解析函数，模块导入即抛
  `ImportError`。已改为直接实例化 WinRT OCR 引擎；图像为空时改从原始像素重建位图，不再丢帧。
- **[P0] `src/core/config.py` 缺 `import threading`。** 文件末尾的 `threading.Lock()` 会在**导入时**抛
  `NameError`，而 `py_compile` 只做语法检查、发现不了此类问题。已补上导入并复验。
- **[P0] OCR 引擎选择项已从整条链路移除。** 配置模板、设置面板、打包脚本与文档章节统一只描述
  Windows 内置 OCR；v0.1 为单引擎实现，不再有引擎切换概念。
- **[P1] 截图与选框模块对已删除模块的可选导入。** 统一收敛为模块内本地 `ImageRegion` dataclass，
  工程内不再存在对已删除包的引用。
- **[P1] `src/ai/base_translator.py` 与 `src/ui/settings_ui.py` 源码缺失。** 按真实调用契约重建（见下）。

### 源码来源说明（重要）

交付集共 **26 个文件** = 官方 24 项清单文件 + 2 个清单外文件（`src/audio/loopback.py`、
`src/screen/winrt_ocr.py`）。

- **22 个文件为逐字交付原文还原。**
- `src/ai/base_translator.py`、`src/ui/settings_ui.py`：会话记录中**没有完整源码**（只有设计蓝图与
  调用点摘录），因此按调用契约**反向重建**。已用真实调用方 `src/ai/online_translator.py` 与
  `main.py` 的导入/实例化/信号连接路径逐项验证接口一致，但**代码文本与首次交付原文不完全相同**。
- `src/screen/winrt_ocr.py` 为本次新增。

### 依赖变更

- **新增必需**：`PyAudioWPatch` —— WASAPI 环回的首选后端（缺失时才会退化到 `soundcard`）。
- **移除冗余**：`silero-vad`、`httpx` —— 代码中零引用（VAD 为能量 + 自适应噪声底实现；HTTP 使用标准库
  `urllib`，`requests` 仅为可选增强），不再列入前置条件。
- **澄清**：`winsdk` 通过 `importlib` 动态导入，普通静态检索搜不到，它是**必需依赖**而非遗漏。
- 可选后端：`soundcard`、`mss`、`requests` 缺失时自动降级，不影响启动。

### 打包状态

**本次未产出打包产物。** 开发环境缺少 PySide6、faster-whisper、winsdk、PyAudioWPatch 等全部运行依赖，
且 PATH 中没有 `pyinstaller` 与 `makensis`。打包脚本本身已通过语法检查与依赖清理，需在装有完整依赖的
Windows 主机上执行：

```bat
build_portable.bat            :: 生成 dist\SubMirror-0.1.0-portable.zip
makensis build_installer.nsi  :: 生成 SubMirror-0.1.0-setup.exe
```

### 已知限制（基线保留）

- 全屏 OCR 仅支持主显示器（多显示器在 v0.2）。
- TTS 中行仅为占位，v0.1 不做任何语音合成或播放。
- 俚语词典为空占位（`assets/slang_dict.json` 为 `{}`）。
- 日志仅输出到控制台，不落盘。
- 翻译依赖公共免密钥端点，可用性与速率不受本项目控制；离线环境无法翻译。
- 安装包未签名，可能触发 SmartScreen 提示。

### 延期项（本次基线不交付）

| 编号 | 内容 | 类型 |
|---|---|---|
| [BL-1](ITERATION_BACKLOG.md#bl-1-配置ui-接线任务) | 配置 + UI 接线（VAD 阈值、Libre 地址、MyMemory 邮箱、队列限流、翻译缓存、悬浮窗颜色/锁定位置、中文路径告警） | 次要 UI 配置 |
| [BL-2](ITERATION_BACKLOG.md#bl-2-新增功能asr-启停全局热键-toggle_asr) | 新增 ASR 启停全局热键 `toggle_asr`（含托盘联动） | 新增功能 |
| [BL-3](ITERATION_BACKLOG.md#bl-3-winrt-ocr-置信度滑块适配api-硬限制) | WinRT OCR 置信度滑块适配（Windows API 不提供行置信度） | API 适配 |
| [BL-4](ITERATION_BACKLOG.md#bl-4-文档补充) | 文档补充（本次已随基线交付完成） | 文档 |

---

## 后续记录模板

```markdown
## [0.1.1] - YYYY-MM-DD — 标题
### 交付范围
### 修复的缺陷            # 格式：[P0/P1/P2] 现象 → 根因 → 修法 → 复验方式
### 变更                  # 行为/接口/配置键的变动，含迁移说明
### 依赖变更
### 已知限制
### 延期项                # 指向 ITERATION_BACKLOG.md 条目
```
