# SquillaRouter 中央路由服务：设计文档

本文档是 **`services/squilla_central`** 的设计说明：它做什么、怎么做、架构与流程、
中央算法（**真实 V4 Phase 3 管线**）、**运营管控面**、自学习语料，以及与 OpenClaw
客户端插件、中控（`tokenhub`）之间的边界。

**核心边界：所有路由判定都在中央。** OpenClaw 侧的 `squilla-router` 插件是纯透传
客户端——不做分类、不做 KV-cache 粘滞、不做图片处理、不做档位就近，只有中央调不通时
才服务 profile 的默认档。

配套图（drawio 源文件，用 <https://app.diagrams.net> 打开）：

- 系统架构图：[`squilla-central-routing/architecture.drawio`](squilla-central-routing/architecture.drawio)
- 单轮请求流程图：[`squilla-central-routing/request-flow.drawio`](squilla-central-routing/request-flow.drawio)
- 配置下发（中控）流程图：[`squilla-central-routing/config-distribution.drawio`](squilla-central-routing/config-distribution.drawio)

代码：`services/squilla_central/server.py`（服务与决策链）、
`services/squilla_central/policy.py`（运营管控面）。

> 配置字段用英文名（与代码/JSON 一致）。**中文对照见 §0 术语表**；只做运营配置的话，
> 看 §0.2（规则字段）和 §0.3（排查字段）两张表就够。

> 命名澄清：本文的 **tokenhub** 指公司内部的**配置下发中控系统**，与 OpenClaw 仓库里的
> `extensions/tencent`（Tencent **TokenHub**，混元 hy3 的模型 provider 网关）**不是**
> 同一个东西。

---

## 0. 术语表

配置字段和轨迹字段用的都是英文名（和代码、JSON 保持一致），这里给出中文含义。
运营只需要看 §0.2 和 §0.3。

### 0.1 基础概念

| 术语 | 中文 | 在本项目里的意思 |
|---|---|---|
| **tier** | 档位 | `c0`/`c1`/`c2`/`c3` 四个**抽象能力档**，从便宜到强。它不是模型名——客户端各自把档位映射到自己配的真实模型。中央只谈档位，不谈模型。 |
| **profile** | 档位方案 | 客户端的一套「虚拟路由 id + 四档模型表」。例如 `squilla/auto` 是省钱型、`squilla/auto-max` 是高质量型。同一个中央同时服务多个 profile。 |
| **tenant** | 租户 | 调用方身份（团队/业务线/环境），请求里的 `tenantId`。数据隔离、统计、管控都按它切分。 |
| **classifier** | 分类器 | V4 模型本身。输入一句话，输出「这轮需要哪个档」。 |
| **baseline** | 基准档 | **没有任何管控规则时**，这一轮会服务的档位。是判断"规则到底改没改东西"的参照物。 |
| **snap** | 就近取档 | 客户端 profile 可能只配了部分档（比如只有 c0/c2）。模型选了 c1 时要落到实际能服务的档上：**优先向上**（宁可贵一点），都没有才向下。 |
| **sticky** | 粘滞 | 短续轮（"继续"、"然后呢"）不允许降档。换模型会丢掉 provider 的 KV-cache，整个上下文要重新付费，所以切到"更便宜"的档反而更贵。 |
| **clamp** | 收窄 | 把「可选档位集合」按管控规则的上下界砍掉一部分。砍的是**集合**不是某个值——见 §4B.3。 |
| **argmax** | 最大值所在项 | 四个档的概率里最高的那个。 |

### 0.2 管控规则字段（运营配置时要填的）

| 字段 | 中文 | 说明 |
|---|---|---|
| `name` | 规则名 | 唯一标识。会出现在决策轨迹和统计里，出问题时按它排查。 |
| `note` | 备注 | 写清楚这条规则为什么存在（工单号、负责人、复评时间）。三个月后没人记得。 |
| `weights` | 权重 | **软干预**：给某档的概率乘一个系数。`{"c3": 3.0}` = 让 c3 更容易被选中。模型仍在做判断，只是倾向被推了一把。 |
| `floor` | 下界（托底） | **硬干预**：服务档位不得低于此档。用于保质量（大客户不许走最便宜档）。 |
| `ceiling` | 上界（封顶） | **硬干预**：服务档位不得高于此档。用于控成本（夜间别用最贵的）。 |
| `pin` | 钉死 | **最硬**：直接写死到某一档，模型不参与。内部等价于 `floor` 和 `ceiling` 设成同一个值。 |
| `tenants` | 生效租户 | 只对列出的租户生效。不填 = 所有租户。 |
| `profiles` | 生效 profile | 只对列出的 profile 生效。不填 = 所有 profile。 |
| `hours` | 生效时段 | `[起, 止)` 的**UTC 整点**区间，可跨零点。不填 = 全天。北京时间 09:00–18:00 写 `[1, 10]`。 |
| `notBefore` | 生效起始时间 | 这个时间点之前不生效。ISO8601（如 `"2026-02-01T00:00:00Z"`）或毫秒时间戳。 |
| `notAfter` | 失效时间 | 到点**立即**失效，不用记得回来删。临时措施必须填这个。 |
| `ratio` | 灰度比例 | `0`–`1`。只对一部分**会话**生效（不是一部分轮次——同一会话必须始终在或不在，否则档位中途横跳会反复丢缓存）。 |
| `priority` | 优先级 | 命中多条时数字**大的赢**，同级按书写顺序。新加窄规则能压过既有宽规则，不用重排文件。 |
| `enabled` | 启用 | `false` = 停用但保留。用于"先关掉看看"，不用删了再凭记忆写回来。 |
| `dryRun` | 影子模式 | `true` = **只记录不生效**。照常算出规则会产生什么结果并落库，但实际服务的仍是模型的原判断。上线前用它量影响面。 |

### 0.3 轨迹与响应字段（排查时会看到的）

| 字段 | 中文 | 说明 |
|---|---|---|
| `finalTier` | 实际服务档 | 这一轮真正用了哪个档。客户端逐字执行，所以它就是事实。 |
| `classifierTier` | 模型选的档 | 模型自己的判断，还没经过管控/就近/粘滞。 |
| `baselineTier` | 基准档 | 没有管控规则时会服务的档。和 `finalTier` 一比就知道规则改了什么。 |
| `biasRule` | 命中的规则名 | 空字符串 = 没有规则命中。 |
| `biasMode` | 规则模式 | `applied` = 真的生效了；`dry_run` = 只记录没生效；空 = 没命中规则。 |
| `stuck` | 被粘滞按住 | `true` = 这轮的档位是被上一轮的粘滞保持下来的，不是重新判断的结果。 |
| `tainted` | 已被干预（不进训练集） | `true` = 这轮的服务档位是运营决定的，不是模型判断的，**训练时必须丢弃**。见 §4B.9。 |
| `clampUnsatisfiable` | 上下界无法满足 | `true` = 规则的档位区间和这个 profile 配的档位没有交集，规则在这里落不了地。运营需要改配置。 |
| `decisionId` | 决策 id | 每一轮的唯一编号。端上日志和中央轨迹靠它对上——中央不存明文，要看原文得拿这个 id 去端上查。 |
| `policyVersion` | 策略版本 | 这一轮用的是哪版策略，用于复现和回滚。 |

### 0.4 工程术语（读代码/文档时会遇到）

| 术语 | 中文 | 说明 |
|---|---|---|
| **fail-open** | 失败放行 | 出错时**不阻断**，继续用旧的/默认的。反义是 fail-closed（出错就拒绝）。这条链路上全部选 fail-open——路由挂了就发不出消息，代价比跑一会儿旧策略大得多。 |
| **last-known-good** | 最后一份可用配置 | 新配置加载失败时继续用的那一份。 |
| **hot reload** | 热加载 | 改了配置文件不用重启服务，后台自动生效。 |
| **single-slot snapshot** | 单槽快照 | 配置在内存里只有"当前这一份"，更新时整份替换。好处是请求永远读到完整的一份，不会看到改了一半的状态。 |
| **postprocess** | 后处理 | 模型出概率之后的一串修正规则（margin 升档、欠路由安全网等）。 |
| **degrade / fallback** | 降级 / 兜底 | 主路径不可用时退到简单路径。中央的降级是无依赖启发式；客户端的兜底是 profile 的默认档。 |

---

## 1. 背景与目标

OpenSquilla 的 SquillaRouter 会给每一轮对话选一个「够用的最便宜模型」。目标是把这套
能力搬到 OpenClaw，让选择了智能路由的会话每轮自动选档，既省成本又不牺牲难任务的质量。

约束（贯穿始终，不可回退）：

- **触发要显式**：OpenClaw 里模型很多，用户明确选了某个真实模型时**绝不劫持**。智能路由
  只在会话选中**虚拟路由模型 id** 时触发（见 §3）。
- **成本要真降**：换模型会丢掉 provider 侧 KV-cache，短续轮硬切「更便宜」的模型反而
  更贵，所以必须有 KV-cache 粘滞——但粘滞现在由**中央**执行（见 §4.5）。
- **决策集中**：路由智能集中在中央一处，改策略一处生效，并能给自学习集中供料。
- **插件零判定**：插件不做任何路由判断。它只负责触发门、把这轮的事实报给中央、把返回的
  档位查表落地；中央不可达时服务该 profile 的默认档。
- **库不存明文**：接口可传原文（内部系统），但决策库**不落任何消息文本**，只存派生数据。
- **可追踪**：出问题能查，靠 `decisionId` 把「端上明文」和「中央无明文轨迹」关联起来。
- **协议通用**：中央算法可整体替换，客户端协议只认抽象档位 + decisionId，不绑定算法。

---

## 2. 演进历程

1. **启发式层移植（PoC）**：把 OpenSquilla 无依赖的规则层移到插件，验证端到端可行。
2. **KV-cache 粘滞**：短续轮阻止下调档位（保住热缓存），升档放行。
3. **中央化**：路由智能搬到独立部署的**中央服务**（Python，`services/squilla_central/server.py`），
   插件退化为瘦客户端；接口传明文，库不存明文，每次返回 `decisionId`。
4. **MySQL + 通用协议**：决策库换成 MySQL（PyMySQL）；`/v1/route` 响应改为通用契约
   `{decisionId, tier, confidence, policyVersion, meta}`，算法专属细节收进不透明 `meta`。
5. **插件瘦身（第一轮）**：删掉插件里重复中央规则层的完整启发式，换成粗略兜底；客户端只读
   `tier` + `decisionId`；插件配置全走 `openclaw.json`。
6. **虚拟 id 触发 + 多 profile**：`profiles` 以虚拟路由模型 id 为键，命中才路由；每个
   profile 带自己的档位→模型表；中央感知 `profile`（决策轨迹 + stats）。
7. **完整移植真实 V4 Phase 3**：中央的分类器从「零训练锚点相似度」占位换成 **OpenSquilla
   训练好的 V4 Phase 3 集成模型**（BGE-ONNX + LightGBM + MLP，经 `V4Phase3Strategy`）。
   删掉外部 embedding 端点与锚点占位；BGE 改为 bundle 内 ONNX 进程内推理。
8. **插件纯透传（本次）**：把插件里**剩下的全部判定**——档位就近（snap）、图片轮处理、
   KV-cache 粘滞、粗略兜底猜档——统统搬到中央。插件删掉 `session-store.ts`、
   `applySticky`、`fallbackTier`、`resolveRoute`；请求体新增 `availableTiers` 与
   `hasImage`。中央粘滞的「上一轮档位」从决策库读（`idx_decisions_session`），多实例/
   重启都正确。**通用协议形状不变。**

代码位置：

| 侧 | 文件 | 职责 |
|---|---|---|
| 插件 | `extensions/squilla-router/index.ts` | 触发门 → 调中央 → 查表落地覆盖；失败时用 `defaultTier` |
| 插件 | `extensions/squilla-router/central-client.ts` | 调 `/v1/route`（带 `profile`/`availableTiers`/`hasImage`），只解析 `tier` + `decisionId` |
| 插件 | `extensions/squilla-router/router.ts` | `matchProfile`、`availableTiers`、`targetForTier`、配置解析 |
| 中央 | `services/squilla_central/server.py` | 分类（V4Classifier / 启发式兜底）→ snap → 粘滞 → 落库 → 通用响应；MySQL 存储 |
| 中央算法 | `opensquilla/squilla_router/v4_phase3.py` + `models/v4.2_phase3_inference/**` | 真实 V4：390 维特征 + LGBM+MLP 集成 + 校准 + 后处理（含 LFS 权重 bundle） |

第 8 步之后，插件侧仅剩三个纯函数：`matchProfile`（触发门）、`availableTiers`（把配置的
档位列表报给中央）、`targetForTier`（档位→模型查表）。没有一个包含路由判断。

---

## 3. 触发机制：虚拟路由模型 id

**什么时候用智能路由？—— 用户选它的时候。**

- 运维在 `openclaw.json` 里定义若干**虚拟路由模型 id**（`profiles` 的键，形如
  `provider/modelId`）。它们不对应任何真实模型，只是路由的开关兼配置选择器。
- 会话把模型切到某个虚拟 id（如 `squilla/auto`）即开启智能路由；切回任何真实模型即关闭。
  **真实模型的会话，插件一行逻辑都不执行**。
- 实现：`before_model_resolve` 钩子 ctx 自带会话当前请求的 `modelProviderId`/`modelId`
  （OpenClaw 核心已传，**零核心改动**）。`matchProfile` 先按 `provider/modelId` 全名匹配，
  再按裸 modelId 匹配（OpenClaw 的 modelId 本身可能含 `/`）。
- 虚拟 id 在钩子里就被替换成真实模型，**永远到不了模型解析**——所以命中 profile 后插件
  必须无条件给出覆盖（含中央宕机时）。
- **多 profile**：不同虚拟 id 绑定不同的 4 档模型组合（省钱型/高质量型/合规型），同一网关
  同时提供；中央按 `profile` 字段区分统计与（未来）策略。

配置示例（`openclaw.json` 里插件配置位于 `plugins.entries.<pluginId>.config`，`pluginId`
即 `openclaw.plugin.json` 的 `id`；`config` 下的字段由插件 `configSchema` 定义）：

```json
{
  "plugins": {
    "entries": {
      "squilla-router": {
        "config": {
          "profiles": {
            "squilla/auto": {
              "defaultTier": "c1",
              "tiers": {
                "c0": { "model": "deepseek/deepseek-v4-flash" },
                "c1": { "model": "deepseek/deepseek-v4-pro" },
                "c2": { "model": "z-ai/glm-5.2" },
                "c3": { "model": "z-ai/glm-5.2" }
              }
            },
            "squilla/auto-max": {
              "defaultTier": "c2",
              "tiers": {
                "c2": { "model": "z-ai/glm-5.2" },
                "c3": { "model": "z-ai/glm-5.2", "provider": "zai" }
              }
            }
          },
          "central": {
            "url": "http://router-box:8710/v1/route",
            "tenantId": "team-a",
            "apiKey": "sk-...",
            "timeoutMs": 2000
          }
        }
      }
    }
  }
}
```

注意配置里**已经没有 `sticky` 块**：粘滞归中央，用中央的 `SQUILLA_STICKY*` 环境变量控制。
`defaultTier` 在启动时被钉到该 profile 真正配置了的档位上（优先取等于或更强的，绝不静默
降级），运行期不再做任何就近计算。

---

## 4. 中央算法：真实 V4 Phase 3 管线 + 全部判定

中央的分类器是 OpenSquilla 训练好的 V4 Phase 3 集成模型（经适配器 `V4Phase3Strategy`
调用其 `InferenceCore`）。中央服务里 `classifier` 是唯一分类接缝：生产是 `V4Classifier`，
测试注入 fake，`None` 则退到无依赖启发式。

### 4.1 一次预测的内部流程（`InferenceCore.predict`）

```
消息 →① 390 维特征装配 →② 两个头各自打分 →③ 概率融合 →④ 后处理 → route_class(R0-R3) → tier(c0-c3)
```

**① 390 维特征**（`inference/features.py` 拼 8 段）：

| 区间 | 通道 | 维数 | 来源 |
|---|---|---|---|
| `[0:51]` | HC 手工特征 | 51 | 当前消息的计数类信号（长度/代码块/标点/关键词） |
| `[51:153]` | TF-IDF + SVD | 102 | TF-IDF → SVD 降维，零填充到 102 |
| `[153:163]` | context | 10 | 请求上下文元数据 |
| `[163:179]` | history 统计 | 16 | 上几轮路由决策统计 |
| `[179:243]` | BGE 当前用户 | 64 | 当前消息 BGE embedding，PCA→64 |
| `[243:307]` | BGE 历史用户 | 64 | 历史用户消息 BGE，PCA→64 |
| `[307:371]` | BGE 上轮助手 | 64 | 上轮助手回复 BGE，PCA→64 |
| `[371:383]` | 助手 HC | 12 | 上轮助手信号（拒答/追问/用量） |
| `[383:385]` | continuation | 2 | 短续接线索 |
| `[385:390]` | reasoning | 5 | 推理密集线索 |

BGE 走 **bundle 内的 ONNX 模型**进程内推理（不再需要外部 embedding 端点）。

**② 两个头**（`inference/heads.py`）：LightGBM 主模型（+可选 aux）出一组类别概率；MLP
（ONNX）出 logits，经温度/校准得另一组概率。
**③ 融合**（`ensemble.py`）：按 per-class alpha 加权融合两个头的概率。
**④ 后处理**（`postprocess.py`）：margin/难度/flag/sticky 等规则，产出最终 `route_class`
（R0-R3）、`difficulty`、`margin`、`flags`。适配器把 R0-R3 映射到 c0-c3（1:1）。

### 4.2 我们只喂当前轮

中央决策库不存明文，也就没有会话历史可喂给 V4 的历史通道（上轮用户/助手文本、路由历史）。
因此这些通道（约 163/390 维）为空——即 V4 处理「首轮」时的特征质量。**这是刻意的隐私取舍**：
换取「明文只存在于请求体（用后即弃）」的干净契约。若将来要补历史特征，需要中央持有短时的
per-session 明文环（内存、不落库、TTL），是可选增强，非本次范围。

### 4.3 BGE 固定进程内 ONNX（否决外部 emb）

- **BGE 就在 bundle 内、进程内跑 ONNX，纯 CPU**（`bge_onnx.py` 硬编码
  `CPUExecutionProvider`，取 CLS + L2 归一，512 维）。bge-small INT8 对单条短消息编码
  在 CPU 上是毫秒级、低 QPS 够用，**不需要也未启用 GPU**。
- **不接外部 embedding 端点**（曾评估、明确否决）：训练好的 PCA（512→64）和 LGBM/MLP 头是
  **焊死在这个 INT8 BGE 输出空间上**的（`feature_schema_version` 对这几个产物取哈希防漂移）。
  所以 BGE 不是可替换的「通用 embedding 服务」，而是模型的第一层。换外部端点只有两种结果：
  要么端点必须是**同一个模型**（收益仅是把已经很快的 CPU 编码挪到另一台机器，却凭空多一跳
  网络和一个故障面，还要严守逐位一致契约），要么是**不同模型**——那会让向量落在分布外、
  PCA+头静默失效、选档乱掉。两者都不值当，故完整移植就用进程内 ONNX，简单且正确。

### 4.4 分类之后：snap（缺档向上）

客户端 profile 可能只配了 c0-c3 的一个子集。请求体里的 `availableTiers` 就是「这个 profile
真正能服务的档位」，中央用 `snap_to_available` 把模型选出的档位落到其中：**同档优先 → 向上
找 → 都没有才向下**。向上优先是为了「缺档不静默降级」——宁可贵一点，也不要因为运维少配了
一档就把一个难任务悄悄发给弱模型。

snap 放在中央而不是插件，是因为插件必须零判定：这样落库的 `final_tier` 就是真正服务的档位。

### 4.5 KV-cache 粘滞（`apply_sticky`）

换模型会丢掉 provider 侧的 prompt cache，短续轮切到「更便宜」的模型往往**更贵**（整个上下文
要重新未命中地付一遍）。所以：**短续轮（`len(message) <= maxUserLen`，默认 200）不允许比
上一轮更低的档位**；升档放行（升档同样炸缓存，但真变难的一轮值得）。

- 「上一轮档位」不再来自插件进程内存，而是 `MySqlStore.last_tier()`——按
  `idx_decisions_session` 取该 `(tenant, sessionKey)` 最新一条的 `final_tier`。这样多实例
  部署、进程重启、会话在不同 OpenClaw 实例间漂移，粘滞都仍然正确。
- 开关：`SQUILLA_STICKY`（默认开）、`SQUILLA_STICKY_MAX_USER_LEN`（默认 200）。
- 记录：`stuck` 列标记本轮是否被粘滞按住；`classifier_tier` 保留模型自己的选择做诊断。

### 4.6 图片轮

插件报 `hasImage`，中央决定：图片轮**跳过文本分类器**（文本复杂度说明不了视觉需求），直接
服务 `availableTiers` 里最强的一档（最可能有视觉能力），band 记为 `image`。

### 4.7 降级与部署要求

- **降级**：V4 bundle/依赖不可用时，中央退到无依赖的 band 启发式（`classify_heuristic`），
  snap 与粘滞照常执行，路由照常应答。`SQUILLA_V4=0` 可强制启发式。
- **部署**：V4 路径需要 `opensquilla[recommended]`（numpy / lightgbm / onnxruntime /
  scikit-learn / joblib）+ Git-LFS 模型 bundle（`git lfs pull`，权重约 lgbm 40MB、
  BGE-ONNX 24MB）。`PYTHONPATH=src` 让服务能 import opensquilla 包。
- **自学习**：`/v1/feedback` 收点赞点踩；捕获与导出见 §4A。`SQUILLA_CAPTURE_FEATURES=0`
  可关掉特征捕获（省存储，代价是不再产生训练语料）。
- **升级已有库**：新列由启动时的 `SHOW COLUMNS` + `ALTER TABLE ADD COLUMN` 补齐
  （`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作）。每个新列都带 DEFAULT，老行照常可读。

### 4.8 纯透传对自学习的意义

插件透传之前，中央落库的是「中央想要的档位」，而端上可能再 snap/粘滞一次——落库档位与
**实际服务的模型**可能不一致，训练标签因此带噪。现在客户端逐字应用返回值，于是：

- `decisions.final_tier` == 实际服务的档位 → 可直接作训练标签；
- `decisions.classifier_tier` == 模型自己的选择 → 做诊断与「后处理改了多少」的度量；
- `stuck` 标出被粘滞按住的轮次 → 这些轮的档位不是模型判断的结果，训练取样时可据此筛除。

---

## 4A. 自学习：中央要存哪些数据

判断标准不是「存得多」，而是「离线管线（`self_learning/`）真的读哪些字段」。对照
`schema.py:RouterTrainSample`、`alignment.py`、`dataset.py` 逐项过一遍：

### 4A.1 缺什么（本次补齐）

| 字段 | 谁消费 | 不存的后果 |
|---|---|---|
| `features_390_b64` | `dataset.py` 的 X 矩阵 | **致命**：没有特征就没有训练集，其余字段再全也没用 |
| `feature_schema_version` | `dataset.py` 只保留占多数的版本 | bundle 一升级，新旧特征基**静默混训**，模型学到错位的空间 |
| `raw_bge_1536_b64` | MLP 头重训 | 只能重训 LGBM 头，MLP 头冻结（行大小 ×4，故 opt-in） |
| `route_class`（模型原始预测） | `alignment.py` 的回溯/踩票闸门（与 `final_route_class` 分开读） | 回溯纠正判不出「原本路由得低不低」，纠正信号丢失 |
| `turn_index` | `align_session` 排序 + `i+1`/`i+2` 邻接 | 回溯纠正（看下一轮是否抱怨）整个失效 |
| `complaint_detected` | `REASON_IMMEDIATE_COMPLAINT` / `REASON_RETROSPECTIVE` | **最关键**：见 4A.2 |

`complaint_detected` 由中央用 `detect_complaint()` 在请求时从消息算出，**只落布尔值**，
契合「库不存明文」。它复用 OpenSquilla 的 `COMPLAINT_TERMS` 表（懒加载；拉不到会打印告警，
因为静默丢失它等于静默废掉自学习）。

### 4A.2 为什么 complaint 是分水岭

`dataset.py` 的证据账本按 `reason` 给权重：纠正类（回溯 1.0 / 即时抱怨 0.9 / 显式踩票 1.2）
高，确认类（normal 0.3）低且按相同特征向量的出现频次开方衰减。

没有 complaint 信号，**每一行都是 `REASON_NORMAL`**——训练集变成「模型自己的输出」，
重训只是让模型更确信它原来的判断。这不是自学习，是自我强化，且会放大既有偏差。所以
「数据够不够」的真正问题不是行数，而是**纠正信号占比**；`/v1/stats` 因此新增
`training` 块（`decisions` / `withFeatures` / `tainted` / `complaints` / `trainable`）。

### 4A.3 不需要补的

- `anti_downgrade_applied`、`large_context_floor_applied`：`capture.py` 写了，但
  `alignment.py` / `dataset.py` **从不读**。
- `confidence_gate_applied`：中央这条路径不存在置信度闸门——`v4_phase3.py:78` 存了
  `confidence_threshold` 却再没用过，V4 在 postprocess 内部自己做门控且不暴露标志位。
  所以导出时钉为 `False`，而不是造一个假列。
- `exploration`：`schema.py:78` 定义、`capture.py:99` 写入，但没有任何读取方；
  我们的「人为干预」走 `tainted` **排除**，与 exploration 的「保留并做反事实」相反。
- `image_route`：由 `band == "image"` 派生，已有。
- `executed_kind`（feedback）：中央只有单模型轮，导出时恒为 `single`，不需要列。

### 4A.4 取数：`GET /v1/train/export`

按 `RouterTrainSample` 的字段名直出，离线端可直接 `from_json_dict`。过滤条件写在 SQL 里
（`tainted = 0 AND features_b64 IS NOT NULL`），**不是**写在 endpoint 里——排除是数据的
属性，任何将来的读取方都自动继承，不会有人忘记加。

---

## 4B. 运营管控面（`policy.py`）

分类器决定这一轮**需要**什么档；管控面决定业务**想要**什么档。两者刻意分开：模型是
统计判断，管控是人的决定，混在一起就说不清一次路由到底是谁决定的。

### 4B.1 要解决的运营场景

| 场景 | 运营诉求 | 手段 |
|---|---|---|
| 夜间/低峰降本 | 这段时间别用最贵的档 | `ceiling` 封顶 |
| 大客户保质量 | 这个租户不许走最便宜档 | `floor` 托底 |
| 供应商故障 | c3 的模型挂了，先别路由过去 | `ceiling: c2` 或 `pin` |
| 大促/发布期 | 这几天整体提档 | `weights` + `notBefore/notAfter` |
| 压测/演练 | 某 profile 全部写死到某档 | `pin` |
| 想调但不敢调 | 先看看影响面再决定 | `dryRun` |
| 灰度 | 先在 10% 会话上试 | `ratio` |
| 临时措施 | 到期自动失效，不用记得去删 | `notAfter` |

### 4B.2 三种动作，一种内部形态

| 动作 | 语义 | 强度 |
|---|---|---|
| `weights` | 给各档概率乘系数，改变模型的倾向 | 软（模型仍在决策） |
| `floor` / `ceiling` | 服务档位的硬上下界 | 硬（模型只能在区间内选） |
| `pin` | 写死到某一档 | 最硬（模型不参与） |

`pin` 是**配置糖**：解析期归一成 `floor == ceiling`，运行时只有一条路径。所以
「写死」不是特例分支，而是区间退化到只剩一个元素。

### 4B.3 硬边界靠"收窄候选集"实现，不是事后检查

这是最容易做错的地方。`floor/ceiling` 不能在算完档位之后再校验一次，因为它后面还有两步
会移动档位：

- **snap**：客户端 profile 只配了部分档，缺档要就近。如果 ceiling=c1 而 profile 只有
  c0/c3，事后校验的写法会让 snap 向上跳到 c3，**冲破封顶**。
- **sticky**：短续轮会保持上一轮的档。如果上一轮是 c3、本轮 ceiling=c1，sticky 会把 c3
  按住，**同样冲破封顶**。

所以 clamp 的作用对象是**候选集合**，而不是某个档位值：

```
candidates = [t for t in availableTiers if floor <= t <= ceiling]
desired    = snap(desired, candidates)          # 只能从区间里挑
previous   = snap(last_tier, candidates)        # 粘滞也只能按住区间内的档
served     = sticky(desired, previous, ...)
```

收窄集合之后，后面两步**在结构上就出不去**——不需要任何额外的校验。

区间和客户端档位表**没有交集**时（如 ceiling=c0 但 profile 只有 c2/c3），规则对这个
profile 是不可能满足的。此时不报错、不阻断路由，返回全集并在响应/轨迹里标
`clampUnsatisfiable`，让运营看见「你这条规则在这个 profile 上根本落不了地」。

### 4B.4 软动作：加权后的 argmax 位移，而不是重新选档

```
a0 = argmax(probabilities)              # 模型原始 argmax
a1 = argmax(probabilities × weights)    # 加权后的 argmax
desired = clamp(index(classifier_tier) + (a1 - a0))
```

**关键是用「位移」而不是直接 `argmax(加权概率)`**。V4 的 postprocess 会在 argmax 之上再做
margin 升档、欠路由安全网等修正，`classifier_tier` 往往**高于**它自己的 argmax。若直接按
加权概率重新选档，会把这些修正统统抹掉——连没打算干预的轮次都被静默降级。位移法保留
postprocess，只叠加运营意图。

### 4B.5 作用域：五个维度取交集

| 维度 | 字段 | 省略时 |
|---|---|---|
| 租户 | `tenants` | 所有租户 |
| profile | `profiles` | 所有 profile |
| 时间段 | `hours`（**UTC** `[start, end)`，可跨零点） | 全天 |
| 生效期 | `notBefore` / `notAfter`（ISO8601 或毫秒） | 永久 |
| 灰度比例 | `ratio`（0–1） | 100% |

`ratio` 按 **sessionKey 哈希分桶**，不是逐轮随机采样：同一会话必须始终在桶内或桶外，
否则档位会在对话中途反复横跳，每跳一次就丢一次 KV-cache——灰度本身反而变成了成本事故。
没有 sessionKey 的轮次共用一个桶（要么全在、要么全不在），行为确定且可解释。

`notAfter` 是**排他**的：到点立即失效。这条不是可选项——临时规则会因为没人记得删而变成
永久规则，管控面没有过期机制就会积沉淀。

### 4B.6 优先级、开关、灰度、影子

- **`priority`**：命中多条时取优先级最高的，同级按列表顺序。用优先级而不是纯列表顺序，
  是因为运营面是**往上追加**的：新加一条窄规则要能压过既有的宽规则，而不用重排整个文件。
- **`enabled: false`**：停用但保留。运营需要「先关掉看看」，而不是删掉再凭记忆写回来。
- **`dryRun: true`**：**影子模式**。照常计算规则会产生什么结果并落库，但服务的仍是模型
  的原判断。上线前用它量影响面；因为什么都没改，这些轮次**仍然可训练**。
- **`note`**：写清楚这条规则为什么存在（工单号、负责人）。三个月后没人记得。

### 4B.7 配置与热加载

```json
[
  {
    "name": "night-cost-cap",
    "note": "OPS-1234 夜间降本，2月复评",
    "priority": 10,
    "ceiling": "c2",
    "tenants": ["team-a"],
    "hours": [14, 22],
    "notAfter": "2026-02-01T00:00:00Z"
  },
  { "name": "vip-floor", "floor": "c2", "tenants": ["vip"], "priority": 20 },
  { "name": "c3-outage-pin", "pin": "c2", "enabled": false, "priority": 100,
    "note": "供应商故障时手动打开" },
  { "name": "peak-boost", "weights": { "c3": 3.0 }, "hours": [1, 10],
    "ratio": 0.1, "dryRun": true }
]
```

- `SQUILLA_POLICY_FILE` 指向这个 JSON 文件，后台线程按 mtime 变化热加载
  （`SQUILLA_POLICY_RELOAD_SECONDS`，默认 30s）。
- `SQUILLA_TIER_BIAS` 保留为**内联引导**：小规模/单容器部署懒得挂文件时直接用环境变量。
  两者都配时**文件优先**。
- 热路径只读单槽快照（一次属性读取），新快照在后台线程构建好后一次性替换：重载既不会给
  某一轮加延迟，也不会让一轮看到「改了一半」的规则集。
- **坏配置永不影响路由**：单条规则解析失败只丢那一条并记录原因；整个文件读不到或 JSON
  坏了则**保留 last-known-good**。丢掉正在控成本的 ceiling，比多跑一会儿旧规则更糟。

### 4B.8 可观测与预演

| 接口 | 用途 |
|---|---|
| `GET /v1/policy` | 当前生效的规则集、加载来源/版本/时间、被拒绝的规则及原因 |
| `POST /v1/policy/reload` | 手动触发重载（"我刚推了文件想立刻生效"） |
| `POST /v1/policy/simulate` | 给定 tenant/profile/时间/档位，回答"哪条规则会命中、会变成什么"——**不落库、不影响流量** |
| `GET /v1/stats` → `rules[]` | 每条规则命中多少次、其中真正改变档位多少次 |

`simulate` 是这套管控面的安全网：`floor/ceiling/pin` 是钝器，运营应该能在规则碰到真实
流量之前，拿具体的租户/profile/时间点验一遍——包括还没启用、或处于 dryRun 的规则。

`stats.rules[]` 回答两个运营问题：一条规则**天天命中但从不改变档位**，说明它是死规则；
一条规则**命中即改变**，说明它可能过宽。没有这个分解，两种情况都看不见。

### 4B.9 干预过的数据怎么丢弃

判定「干预过」的边界是这套设计的实质，分三层：

1. **规则命中但没改变服务档位 → 仍然可训练。** 判定基准是**最终服务的档位**与「无规则时
   会服务的档位」的对比，而不是某个中间值。加权把档位推高、结果被 snap 或 sticky 又收
   回原处的轮次，标签并没有被污染。只按「有规则生效」丢弃会白扔大量干净样本。
2. **规则真的改变了服务档位 → `tainted = 1`，丢弃。** 这一轮服务的是运营的决定。
3. **粘滞把污染传到下一轮 → 同样丢弃。** 上一轮被抬到 c3，本轮短续轮被 sticky 按在 c3，
   这个 c3 依然是干预的后果。所以 taint 沿粘滞链传播：
   `tainted = (served != baseline) or (stuck and 上一轮 tainted)`。漏了这一层，干预会在
   一轮之后「洗白」成学习到的标签。

`dryRun` 的轮次**永远不 tainted**——它什么都没改。这正是影子模式的价值：拿到影响面数据的
同时，语料一点没少。

> 为什么是丢弃而不是反事实校正：反事实（IPS/DR）要求随机化与已知倾向性。这里的干预是
> **确定性**的——同一作用域内所有匹配轮都被同样处理，没有重叠支撑，倾向性恒为 0 或 1，
> IPS 权重无定义。所以唯一正确的处理就是丢弃。（`ratio` 灰度确实引入了随机化，但它是
> 部署手段而非实验设计，桶内桶外的流量分布并不可比，仍不构成合法的倾向性估计。）

`decisions` 记 `bias_rule`（哪条规则）、`bias_mode`（`applied` / `dry_run`）、
`baseline_tier`（无规则时会是什么）、`tainted`（是否排除）。响应 `meta` 同样带这些，
运营不用翻库就能解释某一轮为什么是那个档位。

---

## 5. 系统架构

见 [`squilla-central-routing/architecture.drawio`](squilla-central-routing/architecture.drawio)。

三个部署单元 + 一个中控：

- **OpenClaw 实例（fleet）**：`squilla-router` 插件（**纯透传**）。只有触发门、把
  `availableTiers`/`hasImage` 报上去、档位→模型查表、进程内覆盖。明文只在端上 transcript。
- **中央路由服务（Python）**：拥有一切决策。`V4Classifier`（真实 V4 集成模型，含 bundle 内
  BGE-ONNX）→ snap → KV-cache 粘滞 → 落库 → 返回抽象档位；分类器不可用时退启发式。
- **MySQL**：`decisions` / `feedback`，**无明文列**，位于隐私边界内。一份数据三种用途：
  决策轨迹（排查）、粘滞状态源（上一轮档位）、自学习语料（特征 + 对齐信号，见 §4A）。
- **V4 模型 bundle**：`models/v4.2_phase3_inference/`（LGBM/MLP-ONNX/BGE-ONNX/PCA/TFIDF/SVD，
  Git LFS），随中央服务部署在同一台机器，进程内加载。
- **tokenhub（中控）**：**只向中央服务**下发版本化策略配置；**不下发到插件**。详见 §7。

---

## 6. 单轮请求流程

见 [`squilla-central-routing/request-flow.drawio`](squilla-central-routing/request-flow.drawio)。

插件侧（`index.ts`，全流程无一处判断路由该选哪档）：

0. **触发门**：`matchProfile(ctx.modelProviderId, ctx.modelId)` 未命中 → 直接返回。命中 →
   取该 profile，此后必须返回覆盖。
1. **调中央**：`routeRemote` POST `/v1/route`，带 `{tenantId, sessionKey, profile, message,
   attachmentCount, hasImage, availableTiers}`；拿回 `tier` + `decisionId`。
2. **失败即默认档**：中央缺配置/超时/报错 → `tier = profile.defaultTier`，节流告警。
3. **查表落地**：`targetForTier(profile, tier)` —— 纯查表（中央已保证档位可服务）。
4. **记账**：debug 日志带 `profile`、`source`、`tier`、`decisionId`。
5. 返回 `{modelOverride, providerOverride?}`。

中央侧（`server.py` `Central._route`）：

1. 读 `hasImage` / `availableTiers`（缺省视为全 4 档）。
2. **图片轮** → `bypass_outcome(最强可用档, "image")`；否则 `classifier.classify(message)`；
   分类器为 None → `classify_heuristic`。
3. **选规则**（§4B）：`select_rule` 按 tenant/profile/时间/生效期/灰度取优先级最高的一条；
   图片轮不参与。
4. **跑两遍尾部链**（`plan_tier`）：一遍 `rule=None` 得到 **baseline**（模型独自会服务的
   档），一遍带规则得到 **proposed**。每一遍内部都是 `weights 位移 → clamp 收窄候选集 →
   snap → sticky`。`dryRun` 时服务 baseline，否则服务 proposed。
5. **粘滞的上一轮档位**来自 `store.last_decision(tenant, session)`，同一次查询顺带取回
   `tainted` 与 `turnIndex`，热路径仍只有一次往返。
6. **落库（无明文）**：`final_tier` 记真正服务的档，另存 `classifier_tier`、`baseline_tier`、
   `stuck`、`bias_rule`、`bias_mode`、`tainted`，以及自学习捕获
   （`features_b64` / `turn_index` / `complaint`）。
7. **返回** `{decisionId, tier, confidence, policyVersion, meta}`，meta 含 routeClass /
   band / flags / margin / difficulty / `classifierTier` / `stuck` / `biasRule` /
   `biasMode` / `baselineTier` / `clampUnsatisfiable` / `tainted`。

为什么要算两遍：`tainted` 的定义是「**服务档位**与 baseline 不同」，不是「某个中间值被动
过」。跑两遍是拿到这个对比的最直接方式，而且顺带就是 dryRun 的答案。两遍都是纯函数，
唯一的 IO（`last_decision`）只做一次。

本节的新增**都在中央内部**，`/v1/route` 的请求与响应形状没变，所以插件一行没改——这正是
通用协议想要的效果。

---

## 7. 中控（tokenhub）—— 只喂中央，边界很窄

**中控只做一件事：向中央服务下发版本化的路由策略配置。** 不参与单轮决策，永不进热路径，
也**不下发任何东西到插件**。插件配置一律走 `openclaw.json`。中央对 tokenhub **fail-open**
（拉不到就用 last-known-good），后台定时刷新 + 单槽快照，`_route` 只读快照。决策轨迹记
`policyVersion`，可复现/可回滚。V4 落地后，「策略」自然从锚点/阈值演进为「模型 bundle 版本 +
后处理阈值 + 粘滞参数」；bundle 本身较大，走部署发布而非 tokenhub 热下发，tokenhub 只下发
轻量后处理/阈值/粘滞/默认档参数。详见
[`squilla-central-routing/config-distribution.drawio`](squilla-central-routing/config-distribution.drawio)。

> 取舍：tokenhub 不下发到插件，改 fleet 的 profile/模型映射要逐个改 `openclaw.json`——刻意
> 保持插件侧零外部配置依赖、启动即定；集中改用配置管理/发布流程推 `openclaw.json`。
> 好处是「热改的东西」（策略、阈值、粘滞）与「冷改的东西」（哪个虚拟 id 对应哪些真实模型）
> 各有一个明确的所有者，互不重叠。

---

## 8. 隐私与可追踪契约

- **明文只存在两处**：请求体（内部网传输，用后即弃）、客户端会话记录（端上）。
- **决策库无明文**：`decisions` 表没有文本列，只存 profile、字符数、flags、4 类概率、
  margin、`base→gated→final` 档位轨迹、`classifier_tier`/`stuck`、route_class/difficulty、
  版本号、延迟，管控轨迹（`bias_rule`、`bias_mode`、`baseline_tier`、`tainted`），
  以及自学习捕获（`turn_index`、`complaint` 布尔、`features_b64`、
  `feature_schema_version`）。
- **特征向量不是明文**：`features_b64` 是 390 维 float16，经 PCA/TF-IDF-SVD 等**已拟合且
  不可逆**的投影得到，无法还原出原文。`complaint` 只是一个由文本算出的布尔值。两者都属于
  「派生数据」，与不落明文的契约一致。
- **decisionId 关联**：每次决策返回 `decisionId`，插件打进 debug 日志（与端上明文并排）。
  查问题四步：`/v1/stats` 看分布（档位/band/**profile**/评分）→
  `/v1/decisions?sessionKey` 找 turn → `/v1/decisions/{id}` 看完整轨迹（含
  `classifierTier` vs `finalTier` 与 `stuck`，一眼看出是模型选的还是被粘滞按住的）→
  需要原文则去端上日志 grep `decisionId`。

---

## 9. 失败模式与降级

| 失败点 | 行为 | 用户可见影响 |
|---|---|---|
| 中央超时/宕机 | 插件服务 profile 的 `defaultTier`，节流告警 | 该 profile 退化为单一固定档，不阻塞 |
| V4 bundle/依赖不可用 | 中央退无依赖 band 启发式；snap/粘滞照常 | 路由变粗，客户端无感 |
| MySQL 挂 | 决策落库失败（路由本身仍返回）；粘滞读不到上一轮 → 不粘滞 | 轨迹缺失，路由可用，可能多一次 cache miss |
| tokenhub 挂 | 中央保留 last-known-good 策略快照 | 无 |
| 会话首轮 / 无历史 | `last_tier` 为空 → 不粘滞，自由路由 | 无 |
| 完全没配 `central` | 每轮都走 `defaultTier`（等价于把虚拟 id 钉死成一个模型），启动日志告警 | 智能路由静默失效，属运维配置错误 |
| 单条策略规则写坏 | 跳过该条并记录原因（`/v1/policy` 可见），其余照常生效 | 该规则不生效，路由不中断 |
| 策略文件读不到 / JSON 坏 | 保留 last-known-good 快照并告警 | 无（跑的是上一版策略） |
| 策略的 floor/ceiling 在某 profile 上无法满足 | 返回全集并标 `clampUnsatisfiable` | 该规则在这个 profile 上不生效，路由不中断 |
| complaint 词表拉不到 | 打印告警，`complaint` 恒为 false | 路由不受影响；但语料退化为纯确认，重训无意义（`/v1/stats` 的 `complaints` 会是 0） |
| 配置了虚拟 id 但插件禁用/无 profile | 虚拟 id 走正常模型解析并报错 | 运维配置错误，启动日志有告警 |

原则：路由链路上每个外部依赖都必须能**优雅降级**，绝不让配置/中控/库/模型的故障阻断出词。
注意插件侧的降级现在是**一个常量**（`defaultTier`），不是一套本地猜测规则——本地猜测就是
判定逻辑，而判定逻辑只允许有一个所有者。

---

## 10. 安全

- 中央接口用 `SQUILLA_CENTRAL_TOKEN` bearer 校验；插件用 `central.apiKey`。
- 中央拉 tokenhub 需鉴权（token 从环境/凭据注入，不写进仓库）。
- 不落明文、不打印密钥；MySQL 凭据走 `SQUILLA_MYSQL_*` env，不入库、不入日志。
- 模型标识符不写进 commit / 代码注释 / 文档产物。

---

## 11. 未决问题

1. tokenhub 的实际拉取 API 形状（路径、鉴权、返回信封）需对接后确认，才能落地中央侧
   `configSource`。
2. V4 历史通道目前为空（中央不持有明文历史）。若要补，需设计内存-only、TTL、不落库的
   per-session 明文环，或改由端上在另一个钩子提供历史——权衡隐私 vs 特征完整性。
3. V4 模型 bundle 走 Git LFS + 部署发布；本沙箱无法拉权重/装 ML 依赖，故真实 V4 推理为
   **部署验证**，代码用注入 fake 分类器 + 启发式兜底路径覆盖，并已验证「LFS 指针 → 降级」。
4. fleet 的 profile→模型映射靠 `openclaw.json`；大规模改动依赖外部配置管理/发布流程。
5. 粘滞每轮多一次 `last_decision` 查询（走 `idx_decisions_session` 的单行索引查询）。目前
   规模下可忽略；若 QPS 上来，可在中央加**进程内单槽 session→(tier, tainted) 缓存**（有明确
   所有者与 TTL），而不是把状态退回插件。
6. `features_b64` 约 0.78 KB/行（开 `raw_bge` 再加 ~3 KB），需要定保留期与归档策略；
   `decisions` 目前无 TTL 清理。
7. 离线闭环尚未接：`/v1/train/export` 出的是 `RouterTrainSample` 形状的 JSON，还需要一个
   把它落成 event store 并驱动 `build_training_dataset → train → gates → promote` 的
   调度侧。字段契约已静态核对（键名全对齐、必填项齐全），但沙箱缺 numpy，端到端跑通属
   **部署验证**。
8. 显式反馈的 `executed_kind` 在中央恒为 `single`；若将来中央支持 ensemble 轮，
   `feedback` 表要加这一列，否则 `alignment.py` 会把 ensemble 的踩票误当作档位信号。
9. 策略目前是「文件 + 热加载」，改动靠配置管理/发布流程推文件。真正的运营界面（谁改的、
   审批、一键回滚到上一版）应由 tokenhub 承担；中央这侧已经具备接入所需的形态——
   `PolicySource` 只需换一个拉取实现，快照/校验/fail-open/`policyVersion` 都不用动。
10. `/v1/policy*` 与 `/v1/route` 共用同一个 bearer token。管控接口能改变线上路由行为，
   与只读路由请求的权限级别并不相同，接入 tokenhub 时应拆成独立凭据。
