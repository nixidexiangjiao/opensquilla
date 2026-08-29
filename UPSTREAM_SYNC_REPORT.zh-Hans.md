# 社区上游同步与版本对比报告

**同步对象**：`opensquilla/opensquilla`（社区上游）→ 本仓库 `nixidexiangjiao/opensquilla`
**分支**：`claude/merge-community-code-comparison-hhj9cn`
**生成日期**：2026-08-29

---

## 一、合并结果

| 项目 | 内容 |
| --- | --- |
| 合并前 HEAD | `8d3a1b6e` — *Add the curated TokenRhythm router ladder…* (#557)，2026-07-09 |
| 合并后 HEAD | `e015bdee` — *test(gateway): lock chat history wire behavior* (#1476)，2026-08-29 |
| 合并方式 | **fast-forward**（本仓库无本地分叉提交，`HEAD` 是上游 `main` 的祖先） |
| 冲突 | **0**（无需人工解决） |
| 新增提交 | **499 个**（约 452 个已合并 PR） |
| 文件变更 | 3,440 个文件，+1,107,424 / −119,281 行 |
| 版本号 | `0.5.0rc2` → **`0.5.4`**（正式版） |
| 时间跨度 | 2026-07-09 → 2026-08-29（约 7 周） |

跨越的上游发布：`0.5.0rc3` → `0.5.0rc4` → **`0.5.0`（首个 0.5 稳定版）** → `0.5.1` → `0.5.2` → `0.5.3` → `0.5.4`。

**验证**：全部 Python 源码在 Python 3.12 下 `compileall` 通过。完整测试套件未在本环境执行（依赖未安装）。

### 规模变化

| 指标 | 合并前 | 合并后 | 变化 |
| --- | ---: | ---: | ---: |
| `src/` Python 模块 | 847 | 1,068 | +221 |
| 测试文件 | 1,019 | 1,444 | +425 |
| Web UI 源文件 | 401 | 921 | +520 |
| 数据库迁移 | 20 | 41 | +21 |
| 内置 Agent 工具 | 89 | 91 | +18 / −16 |
| 内置 Skills | 68 | 77 | +9 / −1 |

---

## 二、新增功能与特性

### 1. Goal 模式（持久化目标编排）— 全新

- 一个会话可以拥有一个**跨多轮持续存在的目标**，带 `set / status / edit / pause / resume / clear` 控制与 Plan 模式延后。
- 不是第二套 agent runtime：Goal 轮次仍走标准 AgentTask / TaskRuntime / TurnRunner / 沙箱 / 审批 / provider fallback / 用量记账链路；无固定阶段序列、无隐藏 evaluator。
- 排队的后续输入、附件、项目交接、会话 fork 现在能跨重连存活并保持顺序。
- 新增：工具 `update_goal`、`update_goal_progress`；网关 `rpc_goals.py` / `goal_service.py`；迁移 `V033__goal_runs`、`V034__goal_message_anchor`；文档 `docs/goal-mode.md`。

### 2. HTML 文档编辑（Electron 优先）— 全新，最大单笔改动

- Desktop 端可编辑单文件 HTML 附件与交付物：预览 / 源码 / 版本 / 变更四视图，Agent 生成候选修改、用户审阅后提交。
- 自治的 HTML 文档编辑循环（#1359）与 prompt 标注编辑。
- 新增工具族：`document_read` / `document_inspect` / `document_locate` / `document_patch` / `document_apply` / `document_finish`，以及浏览器级 `document_browser_inspect` / `_act` / `_reload` / `_screenshot`。
- 新增子系统 `src/opensquilla/artifact_session/`（生命周期、修改尝试、HTML 锚点、修订仓储）；网关 `rpc_artifact_editing.py`、`desktop_artifact_bridge.py`、`artifact_mutation_recovery.py`。
- 新增迁移 `V037__artifact_sessions`、`V038__artifact_prompt_annotations`、`V039__artifact_mutation_attempts`、`V040__document_resources`。
- 安全：隔离预览面、不透明 mutation grant、原子修订、能力门控的文档工具；旧版 Desktop bridge **fail closed**。

### 3. Runtime Packs（可选运行时下载）— 全新

- 从沙箱设置中按需下载 Python / Node.js / Windows Git Bash 运行时包，走不可变目录（catalog）。
- 支持断点续传、取消、源回退、完整性校验、移除与缓存丢弃，**不触碰系统已安装的运行时**。
- 下载源固定、按精确大小 + SHA-256 校验后再安全解压激活；失败只影响该组件，不阻塞网关启动。
- 收益：Desktop 安装包**显著瘦身**（不再捆绑可选开发运行时）。
- 新增子系统 `src/opensquilla/runtime_packs/`（catalog / manager / models / resolver）。

### 4. 项目工作区 + 沙箱强化

- **项目工作区**（#831）：迁移 `V028__project_workspaces`、网关 `project_workspace_runtime.py` / `rpc_workspaces.py`；macOS 原生项目文件夹创建、Desktop 深链。
- **沙箱设置模块化**（#1032）：Safe/Full 默认值、版本化的文件/命令/网络策略、有界递归删除备份、固定的内置运行时版本、LAN 监听与 CIDR 控制、命名 token（迁移 `V029__sandbox_policy_tokens`）。
- Safe 可用性改为由**实时探针**（进程、文件系统 worker、deny-write、authority-deny-read canary）判定，而非仅看 setup 状态。
- 一次 turn 内固定一个策略版本；高危命令要求精确用户批准；递归删除走不可逆操作确认 + 默认 3 GiB 最旧优先备份库。
- 结构性拒绝会杀掉网关自身进程的 shell 命令（`kill` / `taskkill /PID` / `Stop-Process -Id <gateway pid>` 及按名变体），在任何可配置策略层之前生效。
- 新增文档 `docs/sandbox-security.md`、`docs/sandbox-deep-dive.zh-Hans.md`。

### 5. 路由 / 模型能力

- **每个会话独立策略**：每个 chat 可以自己选 Direct / Router / Ensemble，全局策略只作为新 chat 的默认值（迁移 `V036__session_model_routing`，#1312）。
- **C3 多模型融合**（#1199）：共享 fusion 计划 + 固定模型的弹性回退 + 独立图像路由。
- **TokenRhythm 梯队更新**：C0 = DeepSeek V4 Flash 0731，直连与 C1 默认 = DeepSeek V4 Pro 0813，C2 = Kimi K2.7 Code，C3 = GLM 5.2 B5 fusion（已有自定义 inline tier 不迁移）。
- **TokenRhythm 模型发现**：官方发布目录 + 当前凭据声明的模型权限合并，暴露 `metadata.published` / `metadata.declared`，目录过期时报告状态但不阻塞。有界惰性缓存、last-good 快照、authority 隔离、凭据安全持久化。
- **计费精确化**：TokenRhythm 用量按原生计费凭证记账（迁移 `V024__usage_native_billing_receipts`、`V021__usage_ledger`、`V022__telemetry_daily_usage`、`V023__router_deployment_telemetry`）。
- **新 Provider 能力**：Qwen Token Plan、自定义 Anthropic 兼容 provider、自定义兼容 provider 的 Base URL 恢复；图像生成与模型 provider 共享凭据（#969）；provider 文本工具规范化、模型身份、FX 汇率、应用归属头、错误脱敏等新模块。
- **上下文压缩**改为 deployment-aware 且请求安全（#921）；prompt cache keepalive 机制（网关 `prompt_cache_keepalive.py`）。
- **同轮 steering** 跨 Web UI / CLI / 网关统一版本化，幂等且可跨重连与 provider fallback 存活（#905）。

### 6. Skills / MetaSkills

- **MetaSkills 生产化**（#770）：`/meta` 内联请求、Cron 工作区管理、更丰富的 Skills 生命周期诊断；迁移 `V030__meta_control_intents`、`V031__meta_launch_drafts`、`V032__meta_launch_discard_tombstones`。
- **社区 Skills**（ClawHub + GitHub，#1118）：不可变源解析 + 事务化管理，网关 RPC 与 CLI 增加只读 Doctor 诊断；兼容性明确限定在单根、instruction-first 的 Skill，不支持的执行方言保持惰性且显式；GitHub 批量串行、上限 10 个引用、遇到限流暂停。
- 安全：不安全归档路径/链接、歧义包根、源漂移、畸形 manifest、YAML alias/深度膨胀、超限工件、发布中断恢复，全部 **fail closed**。
- 新增内置 Skills：`paper-artifact-runtime`、`paper-citation-integrity-gate`、`paper-delivery-summary`、`paper-latex-sanitizer`、`paper-length-gate`、`paper-quality-gate`、`paper-source-readiness-gate`、`short-drama-delivery-audit`、`short-drama-review-normalizer`。
- 新增工具 `skill_install_community` / `skill_search_community`（工具族已在基线，能力大幅扩展）。

### 7. 客户端与交互

- **OpenTUI 全屏终端聊天**（源码安装可选，#703）：共享网关会话、turn/tool/reasoning 呈现、Router/Ensemble 控制、受保护的终端恢复；新增 `packages/opensquilla-tui-host` 与大量 `.mjs` 组件（历史渲染、稳定滚动、welcome/context 视图、ensemble block）。
- **Web UI 重构**：删除旧版 Control UI 资产（#752），Vue 控制台不再入库、由 CI 构建；新增 `modules/`、`workbench/`、`architecture/`、`contracts/generated/v4/`、`adapters/gateway/` 等分层，并加入架构门禁（#1468）。
- 新增视图：`OverviewHubView`、`SkillsChannelsHubView`、`NotFoundView`；可调宽侧边栏与会话导航器（#675）；浏览器级 artifact 预览（#869）、可扩展 artifact workbench（#803）。
- **设置信息架构**：收敛为十个稳定目的地，含合并后的「Security & Privacy」与一级「Memory」页；旧深链通过兼容别名继续可用。
- Web UI 可选**背景音乐播放器**（默认关闭，用户自备曲库）。
- Desktop：关闭主窗口时 macOS 保留 Control UI、Windows 保留托盘图标；启动进度改为单调的里程碑式进度；系统语言解析修复（`zh-Hans-HK/TW` 正确落到简体中文）。

### 8. 数据安全与恢复

- **全新 `src/opensquilla/recovery/` 子系统**：原子写、清理、配置修补/恢复、合并、锁、会话合并、设置事务。
- Desktop 配置恢复与整档 profile 导入（#619、#866）、遗留恢复 profile 归并（#800）、profile 迁移改为静默且仅在设置中提供（#762）。
- **遗留 home 迁移**：`opensquilla migrate opensquilla` 支持 CLI `~/.opensquilla`、退役的 Windows 便携目录、显式 `--source`；默认 dry-run + 机器可读报告，apply 走 WAL 安全的整档拷贝并事务提交。
- 每条已发布产品线（0.1–0.5）的默认配置 dump 作为 golden fixture 固定，机械化验证旧配置可加载。
- 遗留配置不再硬失败严格校验（剥离过期键、未注册 channel 类型降级为告警等）。
- 非 UTF-8（如 GBK 损坏）配置文件走「备份后重写」恢复路径，含 CJK 往返与写入中断的回归覆盖。
- SQLite turn 接受在竞争下原子化（#688）；会话恢复有界且非阻塞（#862）。

### 9. 通道（Channels）

- 统一通道平台工作流（#763）：带认证的准入、持久化投递、provider 生命周期契约、配对与实时认证控制、统一状态词汇与 restart-pending 呈现。
- LAN WebSocket 对端限制在 loopback / RFC 1918 / IPv6 ULA，可用 `auth.allowed_client_cidrs` 进一步收紧，公网对端在认证前即被拒。
- Telegram 编辑消息不再触发新的 agent 轮次（避免重复计费）。
- 迁移 `V020__turn_ingress_receipts`、`V025__session_collaboration_state`、`V035__pending_chat_inputs`。

### 10. 其他新增工具

`audio_config`（TTS provider 配置，限定注册端点与凭据环境变量）、`request_user_input`（运行中向用户提问）、`submit` / `submit_plan` / `plan_run_checkpoint`（Plan 模式与提交评审）、`style`。

### 11. 工程与发布

- 新增 CI 工作流：`desktop-fault-injection`、`live-skill-hub-canary`、`managed-toolchain-artifacts`、`mirror-release-to-oss`；CI 通过可信队列证据复用提速（#1334）；新增 `.github/CODEOWNERS`、`.github/ci/`、`.github/scripts/`。
- 新增 `contracts/gateway/v4/` 契约目录与生成脚本（`scripts/contracts`、`generate_router_tier_contract.py`）。
- 大量 live/端到端脚本：`live_harness_security.py`、`live_multi_provider_matrix.py`、`live_tokenrhythm_billing_audit.py`、`live_long_task_release_gate.py`、`long_task_fault_proxy.py` 等。
- 依赖新增：`watchfiles`、`certifi`、`pytest-xdist`。
- 发布镜像到阿里云 OSS（含 Mainland China 直链与稳定 latest 别名）。
- 技术报告上线 arXiv / aiXiv / ChinaXiv，仓库内提供中英文 PDF（`docs/report/`）。

---

## 三、移除与破坏性变更（需注意）

| 变更 | 影响 |
| --- | --- |
| **删除 `opensquilla swebench`** CLI、可选依赖、Python 命名空间与内置 skill | 基准评测配方与证据迁至外部实验账本；`opensquilla agent` 与 Coding Mode 不变，本地已有产物保留但不再由 OpenSquilla 管理 |
| **删除 16 个 `feishu_*` Agent 工具** | 已被统一的通道平台工作流取代（Feishu **通道**本身保留） |
| **删除旧版 Control UI 静态资产**（`gateway/static/{css,js,dist,fonts,vendor}`） | 控制台改由 Vue 构建产物提供 |
| **Vue 控制台产物不再入库** | **源码安装现在需要 Node.js 22.12+ 与 npm**；发布 wheel / Desktop 安装包 / 容器镜像仍自带已构建控制台，用户无需 Node |
| **Desktop 安装包不再捆绑可选开发运行时** | 按需安装 Runtime Packs |
| `onboarding.models.discover` / `models.list` **新增可选字段** | 使用 `additionalProperties: false` 的外部解码器需先放行新增字段再升级 |
| **TokenRhythm 默认梯队模型更换** | 已有自定义 inline tier **不会自动迁移** |
| **每轮 `[Current user request reminder]` 默认关闭** | 内部评测显示长工具循环吞吐提升约 2.5×；可用 `OPENSQUILLA_TURN_OBJECTIVE_REMINDER=on` 逐字节还原旧行为 |
| **Desktop profile 位置语义变化** | Desktop 使用平台应用数据目录；终端安装的 `~/.opensquilla` 是独立 profile，需在设置中显式迁移 |
| **Windows RC3 → RC4+ 升级** | 直接覆盖安装，**不要**先卸载 RC3（其卸载器可能删除用户数据） |
| `bypass` / Full host 行为修正 | 本地 owner 的 `bypass` 在 Web / CLI / TaskRuntime / Cron 一致解析为 Full host，不再初始化沙箱 worker |

---

## 四、变更热度分布

按目录统计的变更行数（前十）：

| 区域 | +行 | −行 | 文件数 |
| --- | ---: | ---: | ---: |
| `src/opensquilla` | 315,254 | 65,903 | 1,106 |
| `opensquilla-webui/src` | 271,175 | 18,021 | 833 |
| `tests/test_gateway` | 90,071 | 10,397 | 187 |
| `tests/test_engine` | 52,495 | 2,053 | 144 |
| `desktop/electron` | 47,666 | 3,927 | 101 |
| `tests/test_sandbox` | 29,064 | 2,366 | 73 |
| `tests/test_skills` | 25,213 | 327 | 49 |
| `tests/test_session` | 19,168 | 266 | 36 |
| `opensquilla-webui/e2e` | 18,173 | 499 | 64 |
| `tests/test_recovery` | 16,640 | 0 | 28 |

改动量最大的 10 个 PR：

1. `#1222` Add Electron-first HTML artifact editing（89k 行）
2. `#770` Make core MetaSkills production-ready（68k）
3. `#752` Delete retired legacy Control UI assets（53k）
4. `#831` Add project workspaces and harden sandbox execution（42k）
5. `#619` Integrate safe RC4 profile recovery and profile imports（36k）
6. `#1118` Make Community Skill delivery transactional and diagnosable（35k）
7. `#1161` Stabilize long-running chat tasks（35k）
8. `#703` Add shared-session OpenTUI chat and routing controls（34k）
9. `#763` Unify channel platform workflows and harden heartbeat delivery（33k）
10. `#1032` Make sandbox settings reliable across desktop and web（32k）

提交作者分布（前五）：`Open-Squilla` 337、`lihongguang-0014` 54、`JiaoQingRui` 17、`Runco` 12、`RickyYii` 11。

---

## 五、升级建议

1. **源码安装环境需补装 Node.js 22.12+ 与 npm**，否则控制台构建会失败（发布 wheel 用户不受影响）。
2. **升级前备份 profile**；Windows Desktop 从 RC3 升级务必覆盖安装而非先卸载。
3. 若曾自定义 TokenRhythm inline tier，请核对新的默认梯队是否需要同步调整。
4. 若有外部客户端解码 `models.list` / `onboarding.status`，先放行新增的可选字段。
5. 依赖 `opensquilla swebench` 或 `feishu_*` 工具的自动化需要改造。
6. 建议在完整依赖环境下跑一次 `pytest`（新增 425 个测试文件）与 Web UI e2e，再推进到生产。
