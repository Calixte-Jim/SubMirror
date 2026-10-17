# 迭代待办工单（ITERATION BACKLOG）

> **当前版本**：v0.1.7 = v0.1 基础稳定版 + BL-1 配置接线 + BL-3 OCR 行过滤适配 + BL-2 ASR 启停热键 + BL-5 九页签拆分 + 0.1.5 运行时验收修复（OCR 链路两个 P0 缺陷 + 配置单例死锁）+ 0.1.6 运行时验收修复（翻译失败即抛异常 + 退出不清缓存）+ 0.1.7 运行时验收修复（**首次启动即退出的 P0**：`AppContext.initialize()` 非幂等）+ 0.1.7 补测（第四轮：真实 TTS 语音 + 真实公网端点，⏳ 归零）+ **0.1.8 系统托盘完善**（显示/隐藏悬浮窗、重置悬浮窗、启停 OCR、绿/灰状态点；含**托盘从未被创建**的缺陷修复） + **0.1.9 便携包内置 Whisper base 模型 + 首次启动配置脚本**（消除 ASR 提示；不改 OCR/翻译/ASR 核心） + **0.1.10 ASR 关闭时隐藏 Whisper 提示 + 翻译失败提示条简化**（不改 OCR/翻译核心）+ **0.1.11 登录静默启动 LibreTranslate**（设置项 + 启动文件夹；不改 OCR/翻译核心） + **0.1.12 内置模型自动识别**（exe 同级模型 / 失效路径回退；不改 OCR/翻译核心） + **0.1.13 固定区域持续 OCR**（画框保存坐标 + 一键启停热键 + 两道变化门；不改 ASR/翻译核心）+ **0.1.14 ASR 流式优化**（Silero VAD + 滑窗分片缩小 + 强制英文 + 解码参数 + 增量预览 + 翻译时间对齐）+ **模型型号下拉**（`asr.model_size`）+ **字幕淡入淡出过场**（`subtitle.fade_*`）+ **区域选择拖拽修复**（松开左键即退出绘制）+ **0.1.15 保存校验按 ASR 开关短路**（ASR 关闭时完全不校验 Whisper 模型目录，不再弹 `not-a-directory`）+ **固定选区拾取改为「松开左键即保存、遮罩不自动销毁、Esc 才结束」** + **固定区域 OCR 默认热键 `Alt+C`**（不改 OCR/翻译/ASR 核心）+ **0.1.16 ASR 设置页悬浮提示**（模型规格下拉逐项 Tooltip + VAD 引擎状态行 Tooltip，双语；纯 UI 文案，不改 ASR/OCR 逻辑、不新增配置键、面板仍 51 键）+ **0.1.17 FPS 俚语词典 + 增量预览先翻译 + ASR 乱码过滤 + 悬浮窗样式面板（预设/宽高/字号/随面板缩放/圆角/背景透明度）+ 三项增强（动画时长/文字颜色+描边/窗口锁定）+ 性能四项（有界队列丢最旧 / energy VAD 分片 9 s / 字幕帧率上限 + 全屏降速 / 调度器缩锁 + 日志分级）**（面板 51 → **59 键**，`collect()`/`load_from_config()` 同步；不改 OCR 与 ASR/VAD 识别算法本身；翻译前置提示因免密端点无指令通道而暂不生效，诚实声明）（见 [CHANGELOG.md](CHANGELOG.md)）
> **已交付范围**：核心 ASR 环回采集、WinRT OCR 识别、翻译链路、隐私合规 —— P0 主流程静态验证通过；
> 设置面板配置接线（BL-1，38 键全部被运行时读取，死控件 0）；OCR 行过滤适配（BL-3，面板 41 键）；
> ASR 启停热键（BL-2，面板 42 键，死控件 0）；九页签拆分（BL-5，`collect()` 44 键不变，死控件 0）
> **待办内容**：设置面板外链与设备下拉（BL-6，**不在 v0.1.7 正式发布范围，计划后续版本迭代**），
> **不阻塞交付**
> **发布口径**：**v0.1.7（2026-10-16）已冻结为正式开源发布基线**（验收 ✅ 62 / ⏳ 0 / ⏸ 2，共 64 项）；
> 安装包编译与三项人工复验在无 Application Control 策略的目标机执行，清单见 README「v0.1.7 发布清单」。
> **验收口径**：验收项与延期项标记见 [ACCEPTANCE.md](ACCEPTANCE.md)（✅ 62 / ⏳ 0 / ⏸ 2，共 64 项；其中 24 项由 0.1.5/0.1.6/0.1.7 三轮无头运行时验收与第四轮补测转正）

## 排期原则

1. **不阻塞主流程**：本工单内所有条目都属于设置面板的次要配置、一个新增热键与一项 API 适配，
   核心识别/翻译/合规链路不依赖它们。
2. **分批独立可验**：各批次互相独立，可各自单独交付；每批完成后追加 CHANGELOG 记录，
   并把 ACCEPTANCE.md 中对应项的 ⏸ 改为 ✅/⏳。
3. **不做静默改动**：涉及新配置键的批次必须同时更新 `DEFAULT_CONFIG`、`_SCHEMA` 与面板绑定，
   否则 `ConfigManager` 会拒绝写入该键。
4. **下一个可开工批次**：**BL-2**（ASR 启停热键；需把 `check_hotkey_conflict` 从两两扩展为三路
   两两比较，运行时需联动采集暂停/恢复）→ **BL-5**（纯 UI 页签组织，零运行时风险）。
   BL-1 与 BL-3 已于 2026-10-16 交付。

---

## BL-1 配置+UI 接线任务 —— ✅ 已完成（2026-10-16）

底层逻辑已在读取对应参数，缺口集中在配置模板/校验规则与界面绑定。按**改动性质**分三类，
原计划按 A → B → C 顺序做（A 最便宜且立刻可见）—— 三类已在同一批内全部完成。

### 完成情况

| 落点 | 改动 |
|---|---|
| `src/core/config.py` | `DEFAULT_CONFIG` 45 → **90 键**；`_SCHEMA` 同步 90 键（范围照运行时 clamp）；中英双语 `lang_data` 各补 14 条，两语种键集合 146 : 146 对齐 |
| `src/audio/vad.py` | `threshold_db` / `min_speech_ms` 改为优先读公开键 `asr.*`，旧 `audio.*` 名保留为兼容别名；新增 `asr.vad_mode == "fps"` 预设（抬高 onset / min_speech 下限），常量 `FPS_ONSET_MS_FLOOR` / `FPS_MIN_SPEECH_MS_FLOOR` |
| `src/audio/capture_win.py` | `CaptureSettings.from_config` 的 `read()` 支持「多键取首个存在值」；`audio.spike_filter_ms` / `audio.silence_log_seconds` / `audio.reconnect_attempts` 为公开键，旧内部名降级为别名 |
| `src/ui/overlay.py` | `OverlaySettings` 新增 `lock_position`；`mousePressEvent` 在锁定或穿透时忽略拖动 |
| `src/ui/settings_ui.py` | 新增 7 组控件（VAD 阈值、Libre 地址、MyMemory 邮箱、缓存 TTL/条数、队列上限/超时、文字颜色+取色按钮、锁定位置），接入 `load_from_config` / `collect`；`_on_browse_model` 增加非 ASCII 路径**告警**（不拒绝） |

> **命名口径**：一个值只保留一个已声明键。面板与工单使用的公开名（`asr.*`、`audio.reconnect_attempts`
> 等）为准，运行时历史内部名仅在公开键缺失时兜底，手工改过 `config.json` 的用户不受影响。
>
> **`asr.vad_min_speech_ms` 默认值由 `200` 更正为 `300`**：原模板值与 VAD 运行时真实默认不一致，
> 面板此前显示的是假默认。`asr.vad_mode` 此前无任何运行时读取，本次按清单语义实现为
> **FPS 预设**（求用户复核该解释是否即清单原意）。

### A 类：运行时代码已在读取该键，仅缺配置模板 + 面板控件（已完成）

| 配置键 | 运行时读取位置 | 改动 |
|---|---|---|
| `asr.vad_threshold_db` | `src/audio/vad.py` | ✅ 默认 `-45.0`、schema `("float", (-80.0, -10.0))`、面板加阈值输入 |
| `translate.libre_base_url` | `src/ai/online_translator.py` | ✅ 默认空串（用内置公共实例）、schema `("str", (0, 256))`、面板加地址输入 |
| `translate.mymemory_email` | `src/ai/online_translator.py` | ✅ 默认空串、schema `("str", (0, 128))`、面板加邮箱输入 |

> 可选补充 `audio.vad_margin_db` 也已接线（默认 `9.0`，schema `("float", (6.0, 40.0))`）。

### B 类：配置键已存在，仅缺面板控件（已完成）

| 配置键 | 现状 | 改动 |
|---|---|---|
| `subtitle.queue_max` | `DEFAULT_CONFIG` 已有，默认 `2`，`_SCHEMA` 已限 `(1, 2)` | ✅ 面板加输入项（范围取 `SINGLE_QUEUE_MAX`） |
| `subtitle.timeout_seconds` | `DEFAULT_CONFIG` 已有，默认 `8.0`，`_SCHEMA` 限 `(1.0, 60.0)` | ✅ 面板加输入项 |

### C 类：需要新增配置键 + 面板控件（已完成）

| 配置键 | 含义 | 改动 |
|---|---|---|
| `overlay.text_color` | 字幕文字颜色 | ✅ 默认 `#FFFFFF`、schema `("color", None)`、面板加输入框 + 取色按钮（`QColorDialog`） |
| `overlay.lock_position` | 锁定悬浮窗位置（锁定后禁止拖动） | ✅ 新增键 + 面板复选框 + `overlay.py` `mousePressEvent` 守卫 |
| `translate.cache_ttl_seconds` | 翻译内存缓存条目的存活时长 | ✅ 默认 `300.0`、schema `("float", (0.0, 3600.0))` + 面板输入项 |
| `translate.cache_max_entries` | 翻译内存缓存条目上限 | ✅ 默认 `256`、schema `("int", (0, 4096))` + 面板输入项 |

> 颜色键的公开名是 `overlay.text_color`（清单写的是 `overlay.font_color`）；`overlay.py` 的
> `_qcolor()` 本就接受 `#RGB` / `#RRGGBB` / `#AARRGGBB`，无需改动绘制代码。

### 模型路径校验增强（已完成）

- [x] `src/ui/settings_ui.py` 的模型目录选择增加**非 ASCII / 中文路径检测**：命中时弹出告警
      （`settings.model_non_ascii_title` / `_body`）并把状态行改写为提示文案，**不拒绝、不阻止保存**。
      实现方式为面板侧告警而非改 `validate_model_dir()` 的返回码 —— 后者被 `main.py` 与
      契约测试共同依赖，改返回码会扩大接口影响面。

### 验收标准（BL-1 完成判据）

1. 上述每一个配置键都能在面板上修改 → 保存 → 重启后值仍在 `%APPDATA%\SubMirror\config.json` 中。
   —— 静态已验证：模板/schema/面板三处键集合一致（90 : 90）、**面板写出的 38 个键全部能被运行时读到**
   （死控件 0）、`self._x` 属性核查无拼写级未赋值读取；**待目标机做一次真实保存往返**。
2. 每个键填入越界值时被 `_SCHEMA` 拒绝并回退到默认值，不写坏配置文件。
   —— 静态已验证（`default_mismatch=0`，即每个声明默认值都能通过自己的 schema 回灌）。
3. 中文/含空格模型路径触发告警弹窗，且**不阻止保存**。—— 已实现（`_warn_non_ascii_path`），
   **待目标机点一次浏览按钮**确认。
4. 设置面板页签补齐为 9 页：通用 / 热键 / 音频ASR / 翻译 / OCR / 悬浮窗样式 / 队列与限流 /
   高级选项 / 关于与隐私。—— ❌ **未完成，转 BL-5**（本次只把控件挂进现有 8 页，未拆页签）。

---

## BL-2 新增功能：ASR 启停全局热键 `toggle_asr` —— ✅ 已完成（2026-10-16，v0.1.3）

**这是新增功能，不是接线**：此前只有两个 OCR 热键槽位，ASR 管线只能在启动时决定是否运行。

### 完成情况

| 落点 | 实现 |
|---|---|
| `src/core/config.py` | `hotkeys.toggle_asr` 默认 `""`（不绑定）+ `_SCHEMA ("hotkey", None)`；中英双语各补 2 条并改写 `dialog.hotkey_conflict.body` |
| `src/core/hotkey.py` | `HOTKEY_TOGGLE_ASR = 0xA003`、`asr_toggled` 信号、`check_hotkey_conflict(a, b, toggle=None)` 三路两两、`_register_from_config()` 跳过空值、`_handle_hotkey()` 新增分派 |
| `main.py` | `_toggle_asr()`（运行中→`stop()` 释放音频设备；空闲→`start()`）、`_notify_asr_state()`（悬浮窗 + 托盘气泡）；`name.toggle_asr` / `notice.asr_paused` / `notice.asr_resumed` 双语文案 |
| `src/ui/settings_ui.py` | 热键页第三个 `HotkeyEdit` + 说明；load/collect 接入（面板 42 键）；`validate()` 三路校验且允许留空 |

### 原工单条目

- [x] `src/core/config.py`：`DEFAULT_CONFIG.hotkeys` 增加 `toggle_asr`（默认留空 = 不注册，
      避免与用户已有热键冲突），`_SCHEMA.hotkeys` 增加 `("hotkey", None)`。
- [x] `src/core/hotkey.py`：注册第三路槽位。原先只注册
      `area_ocr` / `fullscreen_ocr`，现新增「按键触发」型热键（与区域热键的「按住/松开」语义不同）。
- [x] `main.py`：实现音频采集管线启停（暂停 = 释放环回设备并停止喂帧；恢复 = 重新开始采集），
      并联动托盘状态与悬浮窗提示。
- [x] `src/ui/settings_ui.py`：热键页增加 `toggle_asr` 输入框；**热键冲突检测由「两两比较」
      扩展为「三路两两比较」**。
- [x] 冲突校验失败时**不写盘**（沿用现有 `apply()` 先校验后写入的行为）。

### 验收标准（BL-2 完成判据）

1. 设置并按下 `toggle_asr`，ASR 字幕链路停止产生新字幕；再次按下恢复，无需重启程序。
   —— 静态已验证（`_bl2_toggle_check.py` 覆盖 stop/start 分派、暂停/恢复提示、启停失败静默）；
   **待目标机实按一次**确认。
2. 三路热键任意两路重复时，保存被拦截并弹出热键冲突提示，`config.json` 不被修改。
   —— 静态已验证（三路两两 9 种组合 + 留空/无效键忽略）；**待目标机点一次保存**确认。
3. ASR 关闭期间 OCR 字幕链路不受影响（两条管线独立）。—— 已由结构保证（`_toggle_asr` 只触碰
   `AsrService`）；**待目标机同时运行两条管线**确认。

---

## BL-3 WinRT OCR 置信度滑块适配（API 硬限制）—— ✅ 已完成（2026-10-16，方案 B）

> **结论**：采用**选项 B**，随 0.1.2 交付。Windows 原生 WinRT 接口**不输出每行置信度**，
> `WinrtOcrLine.confidence` 恒为 `0.0`，因此置信度滑块**保留但仅对会打分的引擎生效**；
> 真正决定去留的是**最小行高 + 最小字符数**双过滤，且**无论如何至少保留一行**。

原二选一（已裁定）：

- [ ] **选项 A（删除）**：验收清单移除该项，设置面板删除对应控件。
- [x] **选项 B（换判据，已采用）**：保留滑块，过滤判据改为**行高 + 字符数**双过滤，
      并保留「至少一行」下限保护。

### 实现要点

* `WinrtOcrResult.filter_lines(min_height, min_chars, min_confidence, keep_best)`：任一阈值为 0 即关闭；
  行高未知（无 bounding box）的行**不因行高被丢弃**；`min_confidence` 只测带分数的行。
* `KEEP_BEST_LINE = True` 常量兜底：过滤后为空时返回 `_best_line()`（先比行高、再比字符数）。
  故意不做成配置项——可关闭的保证不是保证。
* 引擎级 `WinrtOcrEngine._filter()` 在 `recognize()` 内生效（覆盖全部调用方），并累计 `filtered` 统计。
* 新增配置键 `ocr.confidence_min` / `ocr.min_line_height` / `ocr.min_line_chars`（默认全为「关闭」，
  旧 `config.json` 行为不变）；面板 38 → **41 键**。
* **附带修复**：`main.py` 的 `OcrService.apply_config()` 此前不把配置转发给 OCR 引擎，
  导致保存设置后引擎缓存的阈值不更新——已修。

### 验收标准（已达成）

1. ✅ 设置面板与验收清单一致（B：控件生效且行为可观察）。
2. ✅ 把过滤拉到最严仍至少保留一行识别结果，不出现「整屏清空」（`_bl3_filter_check.py` 21/21 通过）。

---

## BL-4 文档补充 —— ✅ 已完成（2026-10-16，随 v0.1.4 收尾）

- [x] 更新验收清单，标记本次基线版本交付范围与待迭代项 → [ACCEPTANCE.md](ACCEPTANCE.md)
- [x] README 补充分阶段交付说明 → README「分阶段交付说明」章节
- [x] 留存书面变更记录 → [CHANGELOG.md](CHANGELOG.md)
- [x] 每完成一批 BL-1~BL-5，在本文件勾选对应条目并追加 CHANGELOG 记录 → BL-1 / BL-3 / BL-2 / BL-5
      均已完成并各自追加了 CHANGELOG 段落（`[0.1.1]` / `[0.1.2]` / `[0.1.3]` / `[0.1.4]`）
- [x] README 补齐「9 页签结构」与配置键计数口径（面板 `collect()` 44 键：42 个点号键 + 2 个非点号键）
- [x] 新增 BL-6 工单，承接本批从 BL-5 拆出的新增 UI 逻辑（外链入口 / 设备下拉 / 未暴露键）

---

## BL-5 设置面板页签结构补齐 —— ✅ 已完成（2026-10-16，v0.1.4）

BL-1 只把控件挂进现有 8 页，未拆页签。验收清单要求 9 页：
通用 / 全局热键 / 音频ASR / 翻译 / OCR / 悬浮窗样式 / 队列与限流 / 高级选项 / 关于与隐私。

> **实施口径**（用户 m02500 指定）：控件功能本身已全部可用，本批**只做页面分组重组**，
> 不改动业务逻辑、配置键与过滤/热键代码，以最小化引入 bug 的风险。
> 因此下面的「高级选项页暴露未暴露键」与「隐私政策/免责声明入口」两条**不在本批范围内**，
> 已移入新建的 **BL-6**。

- [x] 把队列与限流（`subtitle.queue_max` / `subtitle.timeout_seconds` / `translate.cache_*`）
      从「字幕+悬浮窗」页拆到独立页。
- [ ] ~~新增「高级选项」页：安置已声明但未暴露的键~~ → 移入 BL-6（会改变面板键集合）。
- [ ] ~~新增「关于与隐私」页：隐私政策入口 / 免责声明再次查看入口~~ → 页面已建，可点击入口移入 BL-6。
- [x] 拆页后重跑回归验证清单第 4 条（用 `main.py` 真实构造签名实例化 `SettingsDialog`）。

### 完成情况

| 位置 | 改动 |
|---|---|
| `src/core/config.py` | 新增 15 个双语键：`settings.tab.*`（9 个页签标题）+ `settings.group.queue` / `cache` / `resilience` / `about` / `privacy` + `settings.privacy_note`；既有 `settings.group.*` 文案未动 |
| `src/ui/settings_ui.py` | `_build_ui` 改为 9 个 `addTab`（顺序 = 清单顺序）；新增 `_build_queue_tab` / `_build_advanced_tab` / `_build_about_tab`；删除旧的 `_build_v02_tab` / `_build_maintenance_tab`（其内容并入关于页）；控件仅移动位置，属性名与数量不变；`retranslate()` 末尾新增 `_refresh_about_version()` |

### 验收标准（BL-5 完成判据）

1. `addTab` 调用数为 9，顺序与清单一致。**✅ 静态断言通过**（`_bl5_tabs_check.py`）。
2. 拆页不丢控件：拆页前后 `collect()` 返回的键集合不变（当前 **44** 个 = 42 个点号键 + `ui_language` / `fps_safe_mode`）。
   **✅ 断言 missing=none / extra=none / 计数 44 == 44**。
3. 每页文案仍走 `_bind_text` + `retranslate()`，切换语言后无残留旧语言文案。**✅ 每页均经 `_form` 建页并入绑定表**（真机切换语言待运行时验证）。

---

## 打包与运行时验收现状（0.1.5，2026-10-16）—— 部分完成

> 本批次目标是「完成 BL-5 后同步更新 BL-4 文档，然后进入目标机打包和运行时验收环节」。

**已完成**：
- 无头运行时验收脚本 `D:\Downloads\submirror_runtime_accept.py`，**28 项检查 0 失败**，
  已纳入 `submirror_make_verify_log.ps1` 常规回归；据此修复 3 个缺陷（2 个 OCR P0 + 1 个配置单例死锁），
  并把 7 项验收由 ⏳ 转 ✅。
- 便携版产物已产出：`dist\SubMirror-0.1.5-portable.zip`（约 140 MB）。

**未完成（受本机 WDAC / Application Control 策略阻塞，需目标机执行）**：
- 安装版产物（`makensis build_installer.nsi`）；
- 打包出的 `SubMirror.exe` 冒烟启动；
- 需要真实音频设备 / Whisper 模型 / 真实网络的 17 项 ⏳ 验收
  （环回采集与 ASR 推理、三端点故障转移、断网熔断、VAD 真人语音、hosts 阻断等）。
  → 后续进展见下节：**网络与语音类已于 0.1.7 补测转 ✅，⏳ 归零**。

**建议**：在一台未启用 WDAC 的 Windows 机上执行 `build_portable.bat` 与 `makensis build_installer.nsi`，
再按 ACCEPTANCE.md 逐项跑完后回填状态。

---

## 便携包内置语音模型 + 首次启动脚本（0.1.9，2026-10-16）

**问题**：便携版用户双击 `SubMirror.exe` 后必须自己下载 CTranslate2 模型并在设置面板里
选中目录，否则永远看到「语音识别不可用：请在设置中选择 Whisper 模型目录。」。

**方案**（用户 m05477 指令）：

1. 便携包内置 `Systran/faster-whisper-base`，放在 `models\faster-whisper-base\`；
2. 包内新增 `start_submirror.bat`：首次运行把该目录写进
   `%APPDATA%\SubMirror\config.json` 的 `asr.model_path`，再启动程序；
3. `build_portable.bat` 增加 `SUBMIRROR_PORTABLE_MODEL` / `SUBMIRROR_PORTABLE_EXTRAS`
   两个可选环境变量完成打包（模型与脚本**不进仓库**，保持 24 项文件清单与审计 29 文件不变）。

**边界**：不改 OCR / 翻译 / ASR 核心；`main.py` 不动；脚本只写用户配置，不动程序与模型文件。

**验收标准**（`D:\Downloads\_v019_asr_bundle_check.py`，13 项）

| # | 断言 |
|---|---|
| 1 | 包内模型 4 个必需文件齐全，`model.bin` > 100 MB |
| 2 | `validate_model_dir()` 返回 `(True, "ok")` |
| 3 | `AsrEngine.load()` 成功且 `load_error == ""` |
| 4 | 英文语音样本逐字识别正确（含 "quick brown fox"） |
| 5 | 中文语音样本识别含「大家好」 |
| 6 | 有内置路径时 `AsrService.start()` 不发 `model-path-empty` |
| 7 | 反向对照：路径为空时**必须**发 `model-path-empty` |
| 8 | 脚本 `--no-launch` 退出 0，写入 `asr.model_path`，无 BOM |
| 9 | 其余配置键（5 个）原样保留 |
| 10 | 无配置文件时脚本能从零创建 |
| 11 | 坏 JSON 时保留原文件 + 警告 + `--no-launch` exit 1 |
| 12 | zip 内含 `start_submirror.bat` 与 5 个模型文件，`testzip` 无错 |

## 系统托盘完善（0.1.8，2026-10-16）

**状态：已交付**（`main.py` 装配层改动；`src/ocr`、`src/ai`、`src/audio` 核心零改动）

| 项 | 改动 |
|---|---|
| `main.py` `TrayController` | 新增 `reset_overlay_requested` / `ocr_toggle_requested` 两个信号；菜单扩为 6 项（显示/隐藏悬浮窗、打开设置、重置悬浮窗、启停 OCR、关于、退出）；`_state_icon()` 运行时绘制绿/灰状态点；`set_overlay_visible()` / `set_ocr_active(active, available)` / `_refresh_labels()` 让文案与提示跟随状态 |
| `main.py` `OcrService` | 新增 `is_enabled` 与 `set_enabled()`（应用层闸门），并在 `on_region_ready` / `on_fullscreen_trigger` / `_tick_area` / `_submit` / `_ocr_task` 入口设闸；**未触碰 OCR 引擎与识别逻辑** |
| `main.py` `SubMirrorApp` | `_start_backends()` 中显式 `tray.start()`（修复托盘从未创建的缺陷）；新增 `_toggle_ocr()` / `_reset_overlay()` / `_tray_sync_ocr()` / `_on_overlay_visibility()` / `_on_region_pressed()`；`_notify_asr_state()` 更名 `_notify_state()` 供托盘与 ASR 复用 |
| 文案 | 新增 `tray.reset` / `tray.ocr_stop` / `tray.ocr_start` / `tray.state_on|off|na` 与 `notice.ocr_paused|ocr_resumed` / `notice.overlay_reset` 双语键 |

**验收标准（已满足）**：托盘图标在真实装配流程中出现；六个菜单项各自只发一个信号；左键切换悬浮窗而右键不切换；
菜单文案跟随悬浮窗与 OCR 状态；图标在 OCR 运行时为绿点、暂停时为灰点；暂停后不截图、不投递 OCR、不推字幕、
区域与全屏热键均被忽略，恢复后热键立即生效；重置悬浮窗回到默认位置并持久化；退出后线程归零。

**回归**：`_tray_check.py` 18/18，既有五套验收 28/28、12/12、15/15、8/8、7/7 全绿。

## 打包与运行时验收现状（0.1.7，2026-10-16）—— 无头范围全部达成

> 承接上节：四轮无头运行时验收（0.1.5 / 0.1.6 / 0.1.7 / 0.1.7 补测）把可离机验证的项全部跑完，
> ACCEPTANCE.md 的 ⏳ 计数由 25 降到 **0**（✅ 62 / ⏸ 2 / 共 64）。

**已完成**：
- 入口回归 8/8（`_v017_startup_check.py`，真实 `main.main()` 启动 → 事件循环 → 退出）；
- 第三轮真实启动链 15/15（`submirror_runtime_accept3.py`）；
- 第四轮补测 11/11（`_v018_external_check.py`，真实 Windows SAPI 语音 + 真实公网端点）；
- 翻译快速守门 7/7（`_v016_translator_check.py`）；第一 / 二轮回归 28/28 · 12/12；
- 四轮共修复三类真缺陷：2 个 OCR P0 + 配置单例死锁（0.1.5）、翻译失败即抛异常 + 退出不清缓存（0.1.6）、
  **首次启动即退出 P0**（0.1.7）；第四轮**未发现产品缺陷**（两处初始失败均为验收脚本用词错误）。
- 便携版产物：`dist\SubMirror-0.1.7-portable.zip`（重建时同步刷新随包 README）。

**仍受本机 WDAC / Application Control 阻塞（需目标机执行）**：
- 安装版产物（`makensis build_installer.nsi`）与打包 `SubMirror.exe` 的冒烟启动；
- 三个**子步骤**的真机复验：WASAPI 环回实采（`pyaudiowpatch` / `soundcard`）、Whisper 本地识别
  （`ctranslate2` / `onnxruntime`）、以及三.A.1 的实时音频源（当前用离线语音样本按生产块大小验证判决）。

---

## BL-6 设置面板外链与设备下拉（BL-5 拆出的新增 UI 逻辑）

> **状态：不属于 v0.1.7 正式发布范围**（基线已冻结），作为后续版本（v0.2）的迭代功能。
> 设计定位与验收标准已就绪，见下。

- [ ] 「关于与隐私」页补可点击入口：打开项目仓库 / `PRIVACY.md` / 再次查看 `DisclaimerDialog`。
- [ ] 「音频 / ASR」页补环回设备下拉（枚举 WASAPI loopback，只列扬声器环回、不列麦克风），
      写入已声明的 `audio.device`。
- [ ] 「高级选项」页暴露已声明但未暴露的键（`translate.max_retries` / `retry_delay_seconds` /
      `total_budget_seconds` / `min_interval_ms`、`asr.device` / `asr.compute_type`、
      `audio.device` / `audio.block_ms`），并在 CHANGELOG 中说明面板键集合从 44 增至 N。
- 对应验收项：ACCEPTANCE 二.2.1、二.8.1。

---

## 回归验证清单（每批交付后重跑）

> 一键重跑：`powershell -NoProfile -ExecutionPolicy Bypass -File D:\Downloads\submirror_make_verify_log.ps1`
> （会重新生成 `D:\Downloads\submirror_verify_log.md`，下面 1~3、6~7 项都在其中留证。）

1. 全项目语法校验：所有 `.py` 文件 `py_compile` 零错误（18 个 python 文件）。
2. 静态审计：重跑 `submirror_static_audit.py`，确认
   **内部导入环 0 / 禁用依赖残留 0 / 出站域名仍为 3 个 / 无日志文件写入**。
3. 配置一致性：`_bl1_cfg_check.py`（模板 ↔ schema 键集合与默认值回灌，应为 `RESULT OK`）、
   `_bl1_lang_check.py`（中英键集合对齐，当前 167 : 167）、
   `_bl1_runtime_check.py`（运行时接线断言 20/20）、
   `_bl3_filter_check.py`（OCR 行过滤断言 23/23）、
   `_bl2_toggle_check.py`（ASR 热键断言 34/34）、
   `_bl5_tabs_check.py`（九页签与 `collect()` 键集合断言 40/40）、
   `_bl1_dead_controls.py`（面板写出的键全部被运行时读取，死控件 0）。
4. 配置往返：新增键「写入 → 重启 → 读回」一致，越界值被 schema 拒绝。（需目标机）
5. 契约验证：设置面板改动后，用 `main.py` 的真实构造签名实例化一次 `SettingsDialog`，
   确认没有 `TypeError`（基线中 `main.py:1419` 的关键字参数为
   `context` / `overlay` / `hotkey_manager` / `scheduler` / `audio_capture`）。（需目标机）
6. 属性静态核查：`_bl1_attr_check.py` 逐个文件检查 `self._x` 读取但从未赋值
   （拼写错误类 AttributeError 的静态替代）。当前唯一非 OK 的
   `src/audio/capture_win.py` 命中 `self._config` / `self._meter`，
   二者由基类 `src/audio/loopback.py:462` 与 `:468` 赋值，属继承而非缺陷。
7. 冒烟：`import main`、`create_translator()`、`WinrtOcrEngine()` 在缺依赖环境下不抛异常。

---

## 三种工作模式 + 采集来源与混音 + 降噪/爆破音过滤 + 队列背压（0.1.19，2026-10-21）

- **用户诉求**：修复卡顿卡死与字幕断流；NSIS 四项与「模型不打包」；ASR&翻译六项（2 s 切片 / 0.4 s 重叠、
  快语速降 beam、静音跳过；语气词与按句切分；**队列上限 3 + 满载暂停采集**、**600 ms 单任务超时**、单并发、
  及时释放音频内存；最近 2 句上下文并限 token；**字幕最小 500 ms 刷新**、独立渲染线程；超时显示「翻译超时」、
  过载拉长切片）；强制后台运行不阻塞 UI、管控 CPU/内存；**新增三模式下拉自动加载参数**
  （全能 / 游戏 / 对话）。
- **实现**：`src/core/modes.py`（三档预设 + `apply_mode()`）、`src/core/config.py`（`app.mode` /
  `app.mode_auto_params`、`audio.source` 等 12 个采集键、`perf.pause_capture_on_full`，并把
  `perf.asr_queue_max` / `perf.translate_queue_max` 收紧到 3、`translate.task_timeout_ms` 到 600 ms、
  `perf.overlay_min_interval_ms` 到 500 ms）、`src/audio/loopback.py`（`kind` 与输入设备枚举）、
  `src/audio/capture_win.py`（来源/混音/旁路/背压暂停）、`src/audio/dsp.py`（降噪与爆破音过滤）、
  `main.py`（模式自动套用、队列探测接线）、`src/ui/settings_ui.py`（模式下拉 + 采集页，面板 60 → 72 键）。
- **交付（对应实现）**：见 CHANGELOG `[0.1.19]` 段与 ACCEPTANCE「0.1.19 增补」；验收套件
  `_v0119_flow_check.py`（45 项）。

## 切片按句翻译 + 翻译上下文 + 过载自适应 + 开机自启（0.1.18，2026-10-21）

- **需求（用户原话摘要）**：①音频按 **2 s** 切片、重叠 **0.4 s**，语速快时自动降束宽，静音跳过；
  ②清理 `um`/`ah` 等填充语气词，按 `.!?` 切短句，空白/杂音直接丢弃；③任务队列上限 **4**，过载丢旧任务，
  单任务 **700 ms** 超时，翻译**单并发**，任务结束释放音频内存；④翻译只附带最近 **2** 句上下文并限制 token，
  禁止长文本一次性提交；⑤字幕最少 **400 ms** 刷新，渲染独立线程；⑥超时字幕显示「翻译超时」，
  持续过载自动拉长切片，内存及时释放。**强制**：ASR/网络翻译不阻塞 UI 主线程、控制 CPU、避免游戏掉帧。
- **交付（对应实现）**：见 CHANGELOG `[0.1.18]` 段与 ACCEPTANCE「0.1.18 增补」。
  `audio.vad_chunk_seconds`/`vad_chunk_max_seconds`/`vad_overlap_ms`(400)、`asr.sentence_split`/
  `max_sentences`/`adaptive_beam`/`fast_speech_wps`/`fast_beam_size`、`text.filler_filter`/
  `filler_words`/`max_sentence_chars`、`translate.task_timeout_ms`(700)/`context_sentences`(2)/
  `context_max_chars`(240)、`perf.asr_queue_max`(4)/`translate_queue_max`(4)/`translate_workers`(1)/
  `overlay_min_interval_ms`(400)、`app.autostart`(false)。
- **NSIS 四项核对结论**：自选安装目录（`MUI_PAGE_DIRECTORY`）、桌面快捷方式默认勾选（`SEC_DESKTOP`
  未标 `RO`）、卸载可选保留配置（`SM_REMOVE_CONFIG` + MessageBox 默认「否」）、**模型不打包进安装包**
  （`dist\SubMirror` 无 `models\` 目录，模型只随便携包分发）—— 0.1.17 安装脚本已全部具备，本轮只需随
  `APP_VERSION` 一起改版本号；「开机自启默认关闭（高级设置可开关）」由 `app.autostart` 实现。
- **验证**：新增无头套件 `_v0118_flow_check.py`（切片/切句/语气词/上下文/超时/队列/字幕地板/自启脚本），
  既有 0.1.17 与 0.1.16 套件按新版号下限与面板键数（59 → 60）同步更新；另有开发期探针
  `_v0118_probe.py`（20 项）用于逐模块回归。

---

## 翻译与字幕流优化 + 悬浮窗样式面板 + 性能四项（0.1.17，2026-10-20）

- **需求（用户原话摘要，两批同发布）**：
  1. **翻译优化**：内置简易 FPS 俚语词典（`gg` `clutch` `wp` `u` `r` `nah` 等）优先替换游戏口语缩写，
     减少离谱翻译；翻译模块增加前置提示（输入为 FPS 玩家实时口语，短句/缩写多，译成自然通顺中文、不硬直译）。
  2. **字幕流式逻辑**：ASR 增量预览优先送翻译，**禁止未翻译英文原文直接展示**，等译文返回再渲染；
     过滤 ASR 输出的无意义乱码，不送翻译、不展示。
  3. **悬浮窗样式页**：宽度滑块、高度滑块（自由拉伸）、预设下拉（默认圆角长方形 / 紧凑窄长矩形 / 正方形）、
     **字幕文字大小独立滑块**、**文字随面板缩放**开关、圆角半径滑块（0 直角到大圆角）、
     面板背景透明度滑块 0–100（0 全透明、100 全不透明）。
  4. **三项高优先级增强**：字幕动画时长滑块（0 = 关动画）、文字颜色选择器 + 文字描边粗细滑块、字幕窗口锁定开关。
  5. **性能优化四条**：ASR 任务队列最大数量限制 + 队列满丢最旧，防止堆积与时间戳跳变；energy VAD 模式
     适当增大音频分片长度；字幕限制最大刷新帧率 + 淡入淡出动画在游戏全屏时自动降速；分离 UI 主线程与
     ASR/OCR 工作线程、减少锁竞争、禁止后台任务阻塞 UI；日志分级（默认只输出关键错误）并提供日志等级选项。
  **要求：保留原有音频 ASR 页的模型规格 / VAD 引擎悬浮提示；OCR、ASR、VAD 其余业务逻辑保持不变。**
- **① 翻译与文本处理**（`src/core/config.py`、`assets/slang_dict.json`、`src/ai/text_processor.py`、
  `src/ai/base_translator.py`）：
  - `assets/slang_dict.json` 由空占位 `{}` 填充为 **46 条** FPS 词条（含 `_comment`/`_version` 元数据）；
    `SlangProcessor` 由占位实现改为真实实现：编译为 `(?<![0-9A-Za-z])词条(?![0-9A-Za-z])` 的词边界正则、
    按词条长度**降序**替换（避免短词吃掉长词），中文替换值不会级联命中；新增 `matches()`（不写统计）
    与 `resolve_path()`（相对路径按 exe 同级 → cwd → 仓库根解析，**修掉旧代码从仓库外运行打不开词典**的隐患）。
  - `TextProcessor.process()`：ASR 来源的**纯俚语短句**（`gg`/`wp`/`u`）绕过最小长度过滤；
    新增 `_is_asr_noise()`（幻觉词表 / 方括号与全角括号标签 / 符号占比 / 重复字符或重复词），
    命中即返回 `REASON_ASR_NOISE`，**不送翻译、不上屏**；保守设计，`ok`/`no`/`nice` 与中文重复语气词不误杀。
  - 新增配置键：`text.slang_enabled`（默认 `true`）、`text.slang_path`（默认 `assets/slang_dict.json`）、
    `text.asr_noise_filter`（默认 `true`）；`text` 组进入 `DEFAULT_CONFIG` 与 `_SCHEMA`。
  - `BaseTranslator` 新增**样式提示契约**：`supports_prompt` / `default_prompt` 类属性，
    `style_prompt`（getter/setter）/ `prompt_enabled` / `supports_style_prompt` 属性，
    `apply_config()` 读 `translate.prompt` / `translate.prompt_enabled`，`stats()` 增
    `prompt_supported` / `prompt_enabled` / `prompt_chars`。
  - **诚实边界（必须在文档与汇报中声明）**：`_GoogleGtxEndpoint` / `_MyMemoryEndpoint` /
    `_LibreTranslateEndpoint` 三个内置免密端点**都没有自由文本指令通道**，因此**没有任何后端**声明
    `supports_prompt`，`prompt_supported` 恒为 `False`、提示词**不会**进入请求；
    本版实际改善翻译的是俚语词典与短句过滤。契约与配置键已就位，接入支持指令的端点（OpenAI 兼容 / 自建 LLM）
    即可生效——**不为此新增"提示词是否生效"之类的死控件**。
- **② 增量预览先翻译再上屏**（`main.py`）：新增配置键 `asr.preview_translate`（默认 `true`）；
  `AsrService.__init__` 增 `_preview_translate`（默认 `False`，`apply_config()` 按配置打开）；
  `_partial_task()` 在文本处理通过后，若开关打开且翻译器可用，则把增量文本
  `submit(LANE_TRANSLATE, self._preview_translate_task, ...)` 并 **return（不再直接推原文）**；
  新任务 `_preview_translate_task()` 调翻译，成功用译文、失败/异常回退原文，再 `_push_preview()`；
  `_push_final()` 在开关打开且已有预览文本时，把 `translated=self._partial_text` 一起写入同一行，
  避免成品行先闪原文。翻译关闭或不可用时行为退回 0.1.14。
- **③ 悬浮窗样式面板**（`src/ui/overlay.py`、`src/ui/settings_ui.py`）：
  - 新增模块常量 `SIZE_PRESETS`（`default` 900×220/圆角 14、`compact` 660×120/圆角 10、
    `square` 600×600/圆角 12）、`DEFAULT_SIZE_PRESET`、`preset_geometry()`、`outline_offsets()`
    （描边由固定 4 向 1 px 改为按粗细逐圈外扩，`width=1` 时与旧行为逐项一致）。
  - `OverlaySettings` 新字段 `size_preset` / `font_scale_with_panel` / `outline_width`，新属性
    `effective_font_size`（未开缩放取原字号；开启后按面板宽度相对 900 px 等比缩放并夹在最小字号–96 px）；
    `from_config()` 读新键，`subtitle.mask_opacity` 上界 `0.95 → 1.0`，
    `data.outline = bool(read("overlay.outline", True)) and outline_width > 0`。
  - `settings_ui.py` 新增 `_slider_row()`（滑块 + 实时数值标签）、`_mirror()`（滑块 ↔ 数字框双向同步，
    值相同不写、防反弹）、`_spin_slider()`（滑块拖动 + **精确数字框**为权威值）、`_set_tooltip_text()`
    （空串 = 清空提示）；悬浮窗样式页新增预设下拉（`currentIndexChanged` → `_on_size_preset()` 用
    `blockSignals` 包住三个 setValue，避免预设覆盖用户刚读入的值）、描边粗细滑块 0–8 px、
    圆角滑块 0–48 px、字幕字号滑块 8–96 px（与既有字号框同步）；宽/高由数字框改为滑块 + 精确数字框；
    背景透明度沿用 `_opacity_row` 的 0–100 滑块（0 = 全透明、100 = 全不透明）并补提示文案。
  - 新增双语界面文案键（样式与日志相关标签/提示），随 `ui_language` 实时切换。
- **④ 三项增强**（同一批控件）：字幕动画时长滑块 0–800 ms（0 = 关动画，同时写入
  `subtitle.fade_in_ms` 与 `fade_out_ms`；载入时取两者**较大值**，避免非零动画被显示成关闭）；
  文字颜色沿用 `overlay.text_color` 选择器 + 新增描边粗细 0–8 px（0 = 无描边）；
  窗口锁定 `overlay.lock_position`（开启后 `mousePressEvent` 直接 return，无法拖拽移动）。
- **⑤ 性能四项**：
  1. **有界队列 + 丢最旧**（`src/core/task_scheduler.py`）：新增 `build_lanes(config)`，按
     `perf.audio_queue_max` / `vad_queue_max` / `asr_queue_max` / `ocr_queue_max` / `translate_queue_max`
     （默认 1/4/1/1/8）建泳道；队列满时 `_offer()` 取最旧项并 `token.cancel("superseded")` +
     `task_dropped(lane, task_id)` 上报，`submit()` 永不阻塞调用方。
  2. **energy VAD 分片加长**（`src/audio/vad.py`）：新增 `VadConfig.energy_max_segment_seconds`
     （配置键 `audio.vad_energy_max_segment_seconds`，默认 9.0 s）与 `segment_limit_seconds` 属性
     （按**实际生效引擎**取值：engine 不是 `silero` 时取 `max(max_segment_seconds, energy_max_segment_seconds)`，
     下限 1.0 s）；`push()` 的强制切分点由固定 `max_segment_seconds` 改为该属性，`stats()` 暴露该值。
     实测 13 秒连续语音的识别任务数：6.0 s 上限 → 2 段，9.0 s 上限 → 1 段。
  3. **字幕刷新预算**（`src/ui/overlay.py`）：`perf.overlay_max_fps`（默认 30，间隔下限 4 ms）、
     `perf.overlay_fullscreen_slowdown`（默认开）、`perf.overlay_fullscreen_scale`（默认 0.5）→
     `OverlaySettings.frame_interval_ms(fullscreen)`；模块级 `foreground_is_fullscreen()` 探测
     （user32 `GetForegroundWindow` + `GetWindowRect` 与 `MonitorFromWindow`/`GetMonitorInfoW` 的显示器矩形
     比对，>= 98% 视为全屏；结果缓存 0.5 s；非 Windows / 探测失败 / 缓存为空一律视为非全屏，**不抛异常**）；
     `_on_fade_tick()` 用 `_fade_signature()`（通道 + 两位小数 alpha + 可见行）比对，**未变化则不重绘**
     并累加 `_paint_skips`；`refresh()` 在动画进行中按帧间隔**合并** worker 线程的刷新，且只用纯 Python 状态
     判断（绝不在非 GUI 线程建/启 QTimer）；定时器间隔随全屏状态动态调整。
  4. **线程与锁 + 日志分级**（`src/core/task_scheduler.py`、`src/core/context.py`、`main.py`、
     `src/ui/settings_ui.py`）：`TaskScheduler.stats()` 改为**锁内只取泳道快照**、**锁外**读
     `running_count()`/`pending_count()`/线程数，避免 UI 诊断轮询与提交争抢全局锁；识别 / OCR / 翻译全部
     跑在调度器工作线程（套件断言每个入口都走 `submit()` 且 lane 任务运行在非 GUI 线程）；
     日志新增 `logging.level`（默认 `warning`，schema 枚举 `debug`/`info`/`warning`/`error`），
     `context.resolve_log_level()` / `set_log_level()` / `get_log_level()`，`main.py` 启动按配置设定、
     设置保存后立即生效；设置面板**高级**页新增「日志」分组下拉。**全项目仍不安装 FileHandler**。
- **验收**：新增 `_v0117_style_check.py` 与 `_v0117_perf_check.py` 两套，共 **76 项全过**——
  词典逐例替换（`gg wp`→`打得好 干得漂亮`、`clutch it`→`残局翻盘`、`u r so good`→`你 是吗 so good`、
  `you are good` 不变）、幻觉/标签/重复字符被拒且真实短句保留、预览路径在开关打开时不推原文、
  面板 **59 键**且提示键不落盘、队列满丢最旧且 200 次提交 < 1 ms、energy 9 s / Silero 6 s 的段数对比、
  帧间隔 33 ms（全屏 67 ms）与无变化 tick 不重绘、`refresh()` 帧间隔合并、lane 任务在非 GUI 线程、
  `stats()` 锁内不读计数、日志默认 `warning` 且无 FileHandler、10 个核心文件冻结哈希不变。
  既有套件无回归；0.1.16 套件按新键数（58→59）与版本下限（>= 0.1.17）同步更新。
- **已知限制**：前置提示词对内置免密端点无效（见上"诚实边界"）；全屏降速依赖前台窗口探测（非 Windows /
  失败即不降速）；俚语词典是静态词表（46 条，整词替换、不做上下文消歧，可自行扩充）。

---

## ASR 设置页悬浮提示（0.1.16，2026-10-19）

- **需求（用户原话摘要）**：①**模型规格下拉加鼠标悬浮提示**——跟随模型路径 / `tiny` / `base`【默认推荐】/
  `small` / `medium` 各给一段适用场景说明。②**VAD 引擎下拉加鼠标悬浮提示**——`auto` / `silero VAD` /
  `energy VAD` 各给一段行为说明。**要求：仅新增 UI 悬浮提示文字，不修改任何 ASR、OCR 原有业务代码逻辑。**
- **实现（只动 `src/ui/settings_ui.py` 与 `src/core/config.py` 的文案表）**：
  - `config.py` 新增 9 条双语提示键（zh-CN / en 各 9 键，两语言键集合等长）：
    `settings.asr_model_size.tip.none|tiny|base|small|medium`、
    `settings.asr_vad_engine.tip.header|auto|silero|energy`。提示键**不属于** `DEFAULT_CONFIG`，不进 schema。
  - `settings_ui.py` 新增模块级 `ASR_MODEL_SIZE_TIPS`（5 项，与 `ASR_MODEL_SIZES` 同序同值）与
    `VAD_ENGINE_TIPS`（3 键）；`_build_asr_tab` 里对「模型规格」下拉逐项 `setItemData(pos, text, Qt.ToolTipRole)`，
    并 `currentIndexChanged.connect(self._refresh_model_size_tip)`，让**收起状态**的下拉显示当前选中项的提示；
    「人声检测」只读状态行用 `_refresh_vad_engine_tip()` 生成「首行引擎名 + 三行说明」的多行 Tooltip。
  - 三个新方法（`_apply_model_size_tips` / `_refresh_model_size_tip` / `_refresh_vad_engine_tip`）**只读文案，
    不写配置**；`retranslate()` 与 `load_from_config()` 末尾各调用一次，保证切语言与载入配置后提示同步。
- **验收**：`_v0116_tooltip_check.py` **21 项全过**（双语键存在且非键名回显、提示键不是配置键、规格表与提示表同序、
  逐项 Tooltip 与收起态跟随、VAD 提示首行跟随 `audio.vad_engine`、面板 `collect()` 仍 51 键且无提示键、
  提示方法无写盘、`src/audio`+`src/screen`+`src/ai` 等 8 个核心文件未被触碰）；既有套件无回归。
- **已知限制**：「VAD 引擎」当前是**只读状态行**（引擎由 `audio.vad_engine` 决定，面板无下拉），提示挂在状态行上；
  若要面板内直接切换引擎，需要新增可写下拉并把面板键集合从 51 改为 52，属于功能变更，未在本轮范围内实施。

---

## 保存校验短路 + 固定选区拾取流程 + Alt+C 默认热键（0.1.15，2026-10-19）

- **需求（用户原话摘要）**：①**保存校验逻辑**——仅 ASR 开启时校验 Whisper 模型目录；ASR 关闭时无论
  `model_dir` 内容如何都**完全跳过**目录校验，保存不再弹 `not-a-directory`。②**修复固定选区 OCR**——
  `Alt+C` 唤起选区拾取窗口，**鼠标松开左键保存选区坐标**，拾取窗口**不自动销毁**；`ESC` 取消选区；
  **不要松开热键就清空选区**。**其余功能不动。**
- **① 保存校验短路**（`src/ui/settings_ui.py` + `main.py`）：`SettingsDialog` 新增 `_asr_enabled` 状态位
  （默认 `True`，保证脱离主程序单独构造时行为不变）、`set_asr_enabled(enabled)` 与只读属性 `asr_enabled`；
  主程序 `open_settings()` 弹窗前用 `getattr(self._settings, "set_asr_enabled", None)` 注入
  `not self._asr_off`（取不到就跳过，向后兼容）。`validate()` 的目录校验改为
  `if model and self._asr_enabled:`——**ASR 关闭时整段跳过**，不校验存在性、不弹框、不拦保存；
  `model-path-empty` / `model-path-missing` 两个错误码与文案一字未改。
- **② 固定选区拾取流程**：
  1. `hotkeys.toggle_region_ocr` 出厂默认 `""` → **`"Alt+C"`**（`src/core/config.py` 默认模板 + 注释更新），
     设置面板仍可改绑或清空（清空 = 不注册）。
  2. `main.py` `_toggle_region_ocr`：在「未处于拾取态」时，第一次按下热键进入**拾取模式**并开遮罩；
     移交坐标后进入启停切换；拾取期间热键的 KEYUP **不再**走 `end_fixed_pick` 清空选区
     （新增 `_on_region_released` 的拾取态守卫，热键松开直接 return）。
  3. `_on_fixed_region_committed` **不再调用** `self._picker.end("region-saved")`——提交即保存坐标、
     写入面板并提示「区域已保存」，但**遮罩留在屏幕上**；只有当用户按 **Esc** / 右键（`finished` 回调）
     时才收起遮罩并结束拾取。`AreaPicker` 自身的 `committed` / `cancelled` / `_dragging` 语义不变。
  4. 文案对齐：`settings.ocr_fixed_region_hint`、`settings.hotkey.region_hint`（zh/en）与
     `src/screen/area_picker.py` 的遮罩内置提示改为「松开左键确认，Esc 结束」/
     `release the mouse to confirm, Esc to finish`。
- **验收**：新增 `_v0115_pick_save_check.py` **13 项全过**（ASR 关时任何 `asr.model_path` 都不拦保存、
  ASR 开时仍按原错误码拦截、开关注入链路、`Alt+C` 默认值与可清空/改绑、固定选区松开左键即保存且遮罩
  不销毁、Esc 取消并收起、热键松开不清空已保存坐标、文案含 Esc 语义、`src/ai` 冻结与 `src/screen` 仅
  `area_picker.py` 例外、面板键集合仍为 51）；既有套件无回归（静态审计 files 33 / py 21 / 0 语法错 /
  0 环 / 3 出站域名；BL-1 运行时 20/20、死控件 0、BL-3 23/23、BL-2 34/34、BL-5 40/40、0.1.13 23/23、
  0.1.14 35/35、0.1.10 15/15）。基线同步：`_v0113_region_ocr_check.py` 的热键默认值断言由空串改为 `Alt+C`。
- **边界**：`src/ai/*`、`src/audio/*`、`src/core/subtitle_queue.py`、`src/ui/overlay.py` 零改动；
  面板键集合仍为 **51 键**（本轮**不新增配置键**）；`src/screen` 仅 `area_picker.py` 的提示文案与默认值相关改动。

## ASR 流式优化 + 模型型号 + 字幕淡入淡出 + 拖拽修复（0.1.14，2026-10-18）

- **需求（用户原话摘要）**：①修复固定持续选区 OCR 的拖拽 bug（松开左键要正确触发 release、
  保存坐标、退出绘制状态、框选不再跟随鼠标）；②ASR 优化——接入 Silero-VAD、缩小音频滑动缓冲窗口
  流式分片、强制英文、调优 faster-whisper 解码参数、增量输出降低端到端延迟、缓解 5–6 秒滞后、
  翻译输出做简单时间对齐；③ASR 设置页增加 base/small 模型下拉；④悬浮窗字幕增加 QQ 音乐式
  淡入淡出过场动画。**不改 OCR、翻译核心代码。**
- **① 拖拽修复**（`src/screen/area_picker.py`）：`_PickerWindow` 新增 `_dragging` 状态机，
  `mouseMoveEvent` / `mouseReleaseEvent` 开头即判 `_dragging`（未按下/重复 release 直接忽略），
  左键释放先 `_dragging = False` 再提交 `committed`，右键/Esc/关闭也复位；新增只读属性 `is_dragging`。
- **② ASR 流式优化**：
  1. 新增 `src/audio/silero_vad.py`——`onnxruntime` 直接跑 `models\silero_vad.onnx`（512 样本/帧 +
     64 样本上下文，与官方 `OnnxWrapper` 一致），**无 torch、运行时不下载**；`find_model_path` /
     `is_available` / `SileroVad.probability`，任何失败都只记录 `error` 不抛。
  2. `src/audio/vad.py`——`VadConfig` 新增 `engine`(auto/silero/energy)、`silero_path`、`silero_threshold`、
     `silero_negative_margin`、`silero_min_speech_ms`、`overlap_ms`；默认 `hangover_ms` 400→320、
     `max_segment_seconds` 15→6；`_load_silero()` 两级降级；`_silero_speech_locked()` 做概率判定 +
     滞回；`active_audio()` 供预览识别；切段保留 `overlap_ms` 尾部并给每段编号 `sequence`。
  3. `main.py` `AsrEngine`——新增 `model_size`（含 `bundled_model_dir_for` 解析）、`force_english`、
     `best_of`/`temperature`/`no_speech_threshold`/`compression_ratio_threshold`/`log_prob_threshold`/
     `initial_prompt` 等解码参数（`beam_size` 5→1）；新增 `transcribe_parts()` 返回带时间戳的片段。
  4. `main.py` `AsrService`——`asr.streaming` 走增量预览（按 `partial_interval_ms` 用
     `active_audio()` 识别，**预览行由最终结果就地替换**）；`asr.translate_in_lane` 把翻译移到
     `LANE_TRANSLATE`，**迟到译文回填原行**（`SubtitleQueue.update`）；`_aligned_timeout()` 按语音时长
     调整字幕存活时间（即「简单时间对齐」）。
  5. 修一个既有潜在缺陷：`VoiceActivityDetector.stats` 是 `@property`，而 `AudioPipeline.stats` 曾按
     方法调用（被 `except` 吞掉 → `vad` 统计恒为空），现改为属性读取。
- **③ 模型下拉**（`src/ui/settings_ui.py`）：ASR 页新增 `ASR_MODEL_SIZES` 下拉（follow / tiny / base /
  small / medium）写入 `asr.model_size`，并新增只读「VAD 引擎」状态行。
- **④ 字幕淡入淡出**（`src/ui/overlay.py`）：每通道独立 `_ChannelFade`（alpha + 轻微垂直位移），
  16 ms `QTimer` 驱动；`subtitle.fade_in_ms`(180) / `fade_out_ms`(220) / `fade_slide_px`(6)，
  两者为 0 即退化为原即时显隐（无 timer）。
- **验收**：新增 `_v0114_asr_stream_check.py` **35 项全过**（Silero 加载与两级降级、分片重叠与序号、
  时间戳与解码参数下发、强制英文、增量预览不新增行、独立泳道翻译与回填、对齐超时、失败回退、
  `subtitle_queue.update`、面板 51 键 collect/load 往返、文案与文档核查、`src/ai` 冻结时间戳核对）；
  基线同步：`_bl5_tabs_check.py` 与 `submirror_runtime_accept.py` 的 50 → **51 键**；
  `_v0113_region_ocr_check.py` 与 `_v0110_ui_hint_check.py` 的冻结戳新增本轮合法例外
  （`src/screen/area_picker.py` 等本轮改动文件）。
- **边界**：`src/ai`（翻译核心）零改动；`src/screen` 仅 `area_picker.py` 拖拽修复；
  OCR 识别链路零改动。新增依赖 `onnxruntime` + `models\silero_vad.onnx`（2.3 MB，MIT）。

## 固定区域持续 OCR（0.1.13，2026-10-17）

- **需求**：新增「固定持续选区 OCR」——画框保存坐标，开启后循环截取该区域，英文变化自动 OCR 翻译；
  增加快捷键一键启停，关闭即停止。**不改 ASR、翻译核心。**
- **实现**：
  1. 新增 `src/core/region_watch.py`（纯函数、无 Qt）：`parse_region` / `format_region`（`"x,y,w,h"`）、
     16×16 亮度指纹 `fingerprint` 与 `is_changed`（容差 + 最少变化格数）、
     `normalize_text` / `text_changed`。
  2. `src/core/config.py`：新增 4 个键——`hotkeys.toggle_region_ocr`（默认空 = 不绑定）、
     `ocr.fixed_region`（默认 `""`）、`ocr.fixed_region_enabled`（默认 `False`）、
     `ocr.region_change_tolerance`（默认 `8`，schema 钳制 1–64）；中英双语文案同步补齐（各 188 键）。
  3. `src/core/hotkey.py`：新增第四路热键 `HOTKEY_TOGGLE_REGION = 0xA004` 与 `region_toggled` 信号；
     `check_hotkey_conflict(area, fullscreen, toggle=None, region_toggle=None)` 由三参扩展为四参
     （空值/全空白仍跳过，不误报）。
  4. `settings_ui.py`：OCR 页新增「固定区域」行（只读坐标 + 画框选取按钮 + 启用开关 + 变化灵敏度），
     快捷键页新增固定区域启停录制；新增 `pick_requested` 信号把画框请求交给主程序。
  5. `main.py`：`OcrService` 新增固定区域状态机（`start_fixed_region` / `stop_fixed_region` /
     `set_fixed_enabled` / `sync_fixed_region`）；主程序接线画框回调与快捷键，落盘配置。
- **复用而非另起**：与既有「拖拽选区 OCR」共用同一个 60 ms 定时器与 `ScreenCapturer`，**不新开线程**；
  既有 OCR 引擎、翻译器、字幕队列一律未改。
- **两道变化门**：先图片指纹（压缩噪声/光标抖动被容差与「≥4 格越界」抑制）再 OCR；
  OCR 文本经规范化比对，相同则不提交字幕、不触发翻译。
- **验收**：`_v0113_region_ocr_check.py` **23 项全过**；面板 `collect()` 由 46 键增至 **50 键**
  （`_bl5_tabs_check.py` 基线同步、`submirror_runtime_accept.py` 46→50）；
  `_bl2_toggle_check.py` 的源文本断言同步为四参调用（34/34）。
- **边界**：`src/ai`、`src/audio` 零改动（时间戳核对含该项）。

## 内置模型自动识别（0.1.12，2026-10-17）

**问题**：便携包已经把 `models\faster-whisper-base` 放在 `SubMirror.exe` 旁边，但程序只认
`asr.model_path`——配置为空、或（实测）指向一个**已被删除的旧解压目录**时，`os.path.isdir`
失败，程序报「未配置 Whisper 模型」，明明旁边就躺着一个完整模型。

**方案**（用户指令：修复目录结构识别，原有校验逻辑不变）：

1. `main.py` 新增只读查找 `bundled_model_dir()`（`BUNDLED_MODEL_RELATIVE`），
   顺序：exe 同目录 → 包根 → 源码目录。
2. `AsrEngine._resolve_model_path()`：有效手选目录优先 → 内置模型 → 否则原样返回配置值，
   保持 `model-path-empty` / `model-path-missing` 两种错误码不变。
3. `validate_model_dir`、`model-path-*` 判定、设置面板与 `start_submirror.bat` **均未改动**。

**边界**：不改 OCR / 翻译核心（`src/ai`、`src/screen` 时间戳未变，验收套件含该项核对）；
解析是只读的，不写任何配置。

## 登录静默启动 LibreTranslate（0.1.11，2026-10-17）

- **需求**：设置项勾选后，登录时静默（无黑窗）启动 LibreTranslate，待服务就绪再启动 SubMirror；
  取消勾选删除该启动项。（不改 OCR / 翻译核心）
- **实现**：新增 `src/core/silent_launch.py`（Qt-free、无网络调用），读 `translate.libre_dir` /
  `translate.libre_autostart`，向当前用户「启动」文件夹写入 / 删除 `SubMirror-SilentStart.vbs`；
  `settings_ui.py` 翻译页新增目录行 + 浏览按钮 + 复选框 + 即时校验；`main.py` 设置应用后调用 `sync()`。
- **免提权原因**：`schtasks /Create /SC ONLOGON` 在未提权进程下返回 `Access is denied`（本机为分裂令牌），
  故不用真计划任务，改走启动文件夹；保留对同名旧计划任务的一次性清理。
- **验收**：`_v0111_silent_start_check.py` **18 项全过**；BL-1 `_bl1_key_scan.py` / `_bl1_dead_controls.py`
  与 `_bl5_tabs_check.py`（BASELINE_KEYS 44 → 46）全过。
- **回归**：静态审计 files 30 / py 19 / 语法错 0 / 导入边 29 / 环 0 / 禁用词 0 / 出站域名 3。

## 提示文案收敛（0.1.10，2026-10-17）

**问题**：（1）用户用 `hotkeys.toggle_asr` 关掉语音识别后，悬浮窗仍会弹出
「语音识别不可用：请在设置中选择 Whisper 模型目录。」；（2）翻译失败时提示条文案偏长
（「翻译服务不可用，暂时显示原文」）。

**方案**（用户指令；经确认，目标是悬浮窗提示条文案）：

1. `main.py::SubMirrorApp` 增加 `_asr_off` 状态：`_toggle_asr()` 暂停时置位并清除
   屏幕上残留的 ASR 提示；`_on_asr_failed("model-path-empty")` 与 `_start_backends()`
   两条设置提示的路径都加了 `not self._asr_off` 守门（恢复 ASR 时清零）。
2. `src/ui/overlay.py` 的 `notice_translate_unavailable` 简化为「翻译服务不可用」/
   「Translation service unavailable」。

**边界**：不改 OCR / 翻译核心（`src/ai`、`src/screen` 时间戳未变，验收套件含该项核对）；
原始端点报错仍走 `logger.warning`，只是不再进提示条。
