<div align="center">

# Reason

**为任何自动化流程提供有边界的决策与持续改进。**

一个零依赖的 Python 服务，部署在你的工作流旁边，在每个关键步骤只回答一个问题：*现在应该发生什么？*
先走确定性规则，再走廉价的类型化决策器，只有在升级时才动用推理模型，最后才交给人类。它按自动化流程记住经验教训，每天回放回归测试集，并且绝不允许模型改写自己的测试。

[English](README.md) · [Français](README.fr.md) · [中文](README.zh.md) · [العربية](README.ar.md)

`Python ≥ 3.11` · `零依赖` · `MIT` · `HTTP · MCP · Claude Code 钩子`

版本说明与历史：[CHANGELOG.md](CHANGELOG.md)。

</div>

---

## 为什么需要 Reason

接入自动化流程的智能体会随时间变差，而且原因是有实测数据的，不是传闻：

| 你看到的症状 | 研究结论 |
|---|---|
| 智能体"忘记"了同一次运行中早先给出的指令 | 准确率随上下文长度稳步下降；13 个长上下文模型中有 11 个在 32k token 时跌到短上下文得分的 50 % 以下 |
| 一个早期错误滚雪球 | 每一步的准确率沿轨迹逐步退化，模型会以自己过去的错误为条件继续出错 |
| 每天都犯同样的错 | 两次执行之间什么都不保留，每次运行都从零开始 |
| 昨天还好好的，什么都没改，今天就失败 | 供应商会静默更换模型和基础设施；2025 年标准评测漏掉了一次持续数周的回归 |
| 一旦获得"自我改进"的自由，它就作弊 | 已发表研究中 30 % 到 74 % 的自我修改运行是在钻测试或代理指标的空子，而不是解决任务 |

来源：[NoLiMa, ICML 2025](https://proceedings.mlr.press/v267/modarressi25a.html) · [Sinha 等, ICLR 2026](https://arxiv.org/abs/2509.09677) · [Anthropic，长时运行智能体](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) · [Anthropic 事故复盘，2025 年 9 月](https://www.anthropic.com/engineering/a-postmortem-of-three-recent-issues) · [METR，reward hacking](https://metr.org/blog/2025-06-05-recent-reward-hacking/) · [Reward Hacking in Self-Improving Code Agents，ICLR 2026 研讨会](https://iclr.cc/virtual/2026/10018648)

Reason 采用经得起检验的对策：少决策、决策类型化，只在升级时推理，通过人工审核队列学习，持续度量，并把护栏放在模型之外执行。

## 工作原理

```mermaid
flowchart LR
    E([来自你的自动化的事件]) --> P0

    subgraph Reason
        direction LR
        P0{P0 规则<br/>确定性，可一票否决}
        P1[P1 类型化决策器<br/>严格 JSON schema]
        P2[P2 推理器<br/>高强度，仅用于升级]
        P3[/P3 人类/]
        L[(经验教训<br/>按命名空间)]
        P0 -- 无规则命中 --> P1
        P1 -- 置信度低、abstain、<br/>被否决或参数无效 --> P2
        P2 -- 仍不确定 --> P3
        L -. 注入提示词 .-> P1
        L -. 注入提示词 .-> P2
    end

    P0 -- 强制动作 --> D
    P1 -- 有把握 --> D
    P2 -- 有把握 --> D
    P3 --> D([决策：动作 + 参数<br/>置信度、理由、回滚])
    D --> X[你的自动化执行]
    X -- POST /outcome --> R[Reflector]
    R -- 提案 --> Q[(待审队列)]
    Q -- 人工批准 --> L
```

每个决策都是封闭枚举：模型只能从**你的**候选动作中选一个，或者 `abstain`，或者 `ask_human`。参数会按你为该动作声明的 JSON schema 校验。规则可以在零毫秒内强制一个动作，也可以否决模型提出的任何动作。

### 学习闭环

```mermaid
sequenceDiagram
    participant A as 你的自动化
    participant R as Reason
    participant M as 模型
    participant H as 人类

    A->>R: POST /decide（状态、候选动作）
    R->>R: 规则、防循环、幂等
    R->>M: 带经验教训上下文的类型化决策
    M-->>R: 动作、参数、置信度
    R-->>A: decision_id、动作、needs_human
    A->>A: 执行动作
    A->>R: POST /outcome（success | failure、人工反馈）
    R->>R: 经验计数器、回归数据集
    R->>M: Reflector：应该学到什么？
    M-->>R: 0 到 2 条简短、可检验的经验
    R-->>H: 待审队列
    H->>R: 批准或拒绝
    Note over R: 已批准的经验塑造后续决策，<br/>带有有益/有害计数与自动修剪
```

## 快速开始

```bash
git clone https://github.com/4n0nn43x/reason && cd reason
cp examples/reason.config.json reason.config.json     # 选择你的模型和角色
export OPENAI_API_KEY=... DEEPSEEK_API_KEY=... REASON_TOKEN=change-me
python -m reason init acme/invoices                     # 创建 data/acme/invoices/{rules.json,policy.md}
python -m reason serve --port 8787
```

第一个决策：

```bash
curl -s -X POST localhost:8787/decide \
  -H "Authorization: Bearer $REASON_TOKEN" -H 'Content-Type: application/json' \
  -d @examples/request.example.json
```

```json
{
  "decision_id": "d_3f9a1c2e7b04",
  "action": {"name": "escalate", "params": {}},
  "tier": "decider",
  "confidence": 0.91,
  "rationale": "未知供应商，金额高于已学习阈值 (L-007)",
  "lessons_used": ["L-007"],
  "needs_human": {"required": false},
  "rollback": null
}
```

回报实际发生的结果，让 Reason 学习：

```bash
curl -s -X POST localhost:8787/outcome \
  -H "Authorization: Bearer $REASON_TOKEN" -H 'Content-Type: application/json' \
  -d '{"decision_id": "d_3f9a1c2e7b04", "result": "failure",
       "human_feedback": {"correct_action": "ask_vendor", "note": "未知 IBAN"}}'
```

然后审核提案：

```bash
python -m reason lessons acme/invoices pending
python -m reason lessons acme/invoices approve L-003
```

可选：`pip install -e .`（或 `uv pip install -e .`）安装 `reason` 命令。

## 请求契约

| 字段 | 用途 |
|---|---|
| `namespace` | 一个自动化流程（及环境）：`org/automation/env`。经验、规则、数据集和日志按它分区。 |
| `decision_point` | 决策在工作流中的位置，例如 `before_pay`。 |
| `event`、`state`、`history` | 给决策器的类型化事实。视为不可信数据，绝不视为指令。 |
| `candidate_actions` | 封闭的动作集合，每个动作带参数的 JSON `schema` 和可选的 `rollback` 流程。 |
| `constraints` | `allow_reasoner`、延迟或成本提示。 |
| `models` | 本次请求的角色覆盖，例如 `{"decider": "jev"}`。 |
| `idempotency_key` | 同一命名空间内相同的键返回相同决策，不再调用模型。 |
| `run_id` | 启用防循环：同一运行中同一位置的决策过多时以 `ask_human` 结束。 |

响应始终包含 `tier`（`rules`、`decider`、`reasoner`、`loop`、`escalation`）、`confidence`、`rationale`、`lessons_used`、`needs_human`（带可直接用于表单、MCP 引导或 Claude Code 钩子的扁平 schema）以及你声明的 `rollback`。

## 模型与角色：一切可替换

`reason.config.json` 声明**模型**和指向它们的**角色**。模型可以是任何兼容 OpenAI 的 chat completions API（OpenAI、DeepSeek、OpenRouter、Together、LiteLLM、vLLM、Ollama）或 Jev。

```json
{
  "models": {
    "typed":    {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-6-luna", "json_schema": true},
    "reasoner": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-6.1-sol",
                 "reasoning_effort": "high", "max_tokens_key": "max_completion_tokens", "temperature": null},
    "deepseek": {"base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-flash",
                 "json_schema": false, "reasoning_effort": "none"},
    "jev":      {"provider": "jev", "base_url": "https://api.typesafe.ai", "api_key_env": "TYPESAFE_API_KEY", "model": "jev-latest"}
  },
  "roles": {"decider": "typed", "reasoner": "reasoner", "executor": "deepseek", "reflector": "typed"}
}
```

| 角色 | 职责 | 缺省时 |
|---|---|---|
| `decider` | 类型化决策（P1）：严格 schema，`abstain` 和 `ask_human` 始终可选 | **由 `executor` 自行决策** |
| `reasoner` | 当 P1 犹豫、被否决或返回无效参数时，以高强度重新决策（P2） | 直接交给人类 |
| `executor` | `POST /execute`：携带命名空间经验的写作、抽取、转换（"双手"） | 必需 |
| `reflector` | 把失败或纠正转化为经验提案 | 回退到 `decider`，再到 `executor` |

- 单个模型既决策又执行：`examples/reason.config.executor-only.json`。
- **Jev**（TypeSafe AI）是类型化决策模型，不是 LLM：远低于一秒给出校准过的选择，可直连或经 OpenRouter（`base_url` 为 `https://openrouter.ai/api`）。它不返回参数也不解释，因此需要参数的动作会升级到推理器。
- 对没有严格结构化输出的供应商（DeepSeek）设 `json_schema: false`：Reason 退回 `json_object` 并在本地校验。
- OpenAI 推理模型用 `temperature: null` 与 `max_tokens_key: "max_completion_tokens"`；`extra` 原样透传任何供应商专有字段。

## 规则（P0）

`data/<namespace>/rules.json`：

```json
[
  {"id": "cap",   "if": [{"path": "state.facts.amount", "op": "gt", "value": 5000}],
                  "forbid": ["approve"], "reason": "超过 5000 绝不自动批准"},
  {"id": "small", "if": [{"path": "state.facts.vendor_known", "op": "eq", "value": true},
                         {"path": "state.facts.amount", "op": "lte", "value": 200}],
                  "decide": {"name": "approve", "params": {"limit": 200}}}
]
```

当动作是有效候选时，`decide` 直接绕过模型（0 毫秒，0 美元）。`forbid` 否决模型的输出：该动作落到下一层，再到人类。运算符：`eq ne gt gte lt lte in nin contains exists regex`。供模型阅读的自然语言策略放在 `policy.md`。

## 可评分的估计、延迟结果、规则与经验

除了*选哪个动作*，Reason 还可以回答*可能性有多大*，并在现实结果揭晓时给这个回答打分。本节内容全部可选。

```mermaid
flowchart LR
    Q([问题]) --> E[POST /estimate<br/>p、区间、来源、证据]
    E --> C{该命名空间与来源<br/>有 Platt 校准吗？}
    C -- 有 --> P[校准后的 p]
    C -- 无 --> R[原始 p]
    P --> A([你的自动化执行])
    R --> A
    S[(结算来源<br/>JSONL · HTTP · subgraph)] -- reason poll --> O[POST /outcome v2<br/>或 /estimate/outcome]
    O --> B[Brier · log-loss · RPS<br/>按命名空间、来源、经验]
    B -->|"reason calibrate"| C
    B --> L[(经验计数器)]
```

### 类型化估计

```bash
curl -s -X POST localhost:8787/estimate -H "Authorization: Bearer $REASON_TOKEN" -H 'Content-Type: application/json' \
  -d '{"namespace": "trader/delphi", "question": "X 会赢得初选吗？", "kind": "binary",
       "market_price": 0.62, "resolves_at": "2026-11-04T06:00:00Z", "n_samples": 3}'
```

`kind` 可为 `binary`（p 与 90 % 区间）、`categorical` 或 `ordinal`（总和必须为 1 的分布）、`quantity`（q05、q50、q95）。每个估计都带有 `source`（`fact` 取自带日期的一手来源，`model` 来自可复现的计算，否则为 `opinion`）、`evidence[]`、可选的 `base_rate`、所用经验，以及 `raw` 与 `calibrated` 两组值。当 `n_samples > 1` 时，样本在对数几率空间平均，其离散度成为区间。

问题揭晓后，`POST /estimate/outcome` 为其打分：Brier 与 log-loss（原始与校准），以基准率和市场价格的 Brier 作为参照，有序结果用 RPS，数量用绝对误差和区间覆盖。估计所用的经验会依据分数获得记分。

```bash
python -m reason calibrate trader/delphi            # 按来源：Brier、相对基准率与市场的技能分、Murphy 分解
python -m reason calibrate trader/delphi --fit      # 按 70/30 时间切分为每个来源学习 Platt，仅在测试 Brier 改善时保留
python -m reason calibrate trader/delphi --counterfactual   # 去掉每条经验重放每个已解决估计：因果度量
```

Murphy 分解（Brier = 可靠性 - 分辨率 + 不确定性）把**校准**（可靠性）与**区分能力**（分辨率）分开。Reason 从不优化 ECE，它不是严格恰当评分规则。估计从不决定仓位大小：阈值、弃权与仓位规模属于 Reason 之外的确定性代码。

### 延迟结果与轮询器

`POST /outcome` 现在接受数周后才到达、并且可能改判的结算：

```json
{"decision_id": "d_3f9a…", "result": "no_outcome", "result_detail": "no_outcome", "payout_fraction": 0.12,
 "value": -3.4, "settled_at": "2026-10-03T14:02:11Z", "source": "delphi_subgraph", "source_ref": "0xabc…:17",
 "label_confidence": 1.0, "version": 1, "resolution_text": "…不可信的第三方文本…"}
```

`result` 新增 `partial`、`void`、`no_outcome`，在经验计数中映射为 `success`、`failure` 或 `unknown`（未知结果既不奖励也不惩罚）。`source_ref` 让提交幂等；更高的 `version` 会修订结果，并撤销上一版本对经验的影响。`label_confidence` 低的结果会被记录但不改变计数。每个已结算决策都会生成一份 markdown 复盘（论点、发生了什么并引用结算文本、偏差、建议的经验），修订时连同版本历史重写。

```bash
python -m reason poll            # 对所有已配置来源执行一轮
python -m reason poll --loop     # 每 5 分钟一次
python -m reason outcomes trader/delphi --horizon-hours 48   # 成熟样本上的成功率、标签覆盖率、轮询延迟
```

轮询器从持久化水位线开始读取本地 JSONL、HTTP JSON 接口或 GraphQL subgraph，按配置映射字段，依据决策时传入的 `correlation_key` 和时间窗口把每笔结算匹配到待定决策，并把超过时限的决策标记为 `unknown`。 既不是有效结果、也不在 `result_map` 或 `ignore` 中的事件类型**绝不会**被转成 `unknown`：那会悄悄把亏损排除在评分之外，让所有来源看起来都很好。它会被暂存，在轮询报告的 `unmapped` 中计数并发出警告，配置修正后自动重新处理。交易者的 `events.jsonl` 与 Polymarket 结算 API 的配置见 `examples/reason.config.v02.json`。

### 规则 v2

```json
{"id": "window-closes-before-event",
 "if": [{"path": "state.facts.window_end", "op": "before", "path2": "state.facts.event_at"}],
 "decide": {"name": "ask_human"},
 "reason": "结算窗口（{state.facts.window_end}）在事件（{state.facts.event_at}）之前关闭"}
```

条件可把路径与常量或另一路径（`path2`）比较，用 `all`、`any`、`not` 嵌套，并提供日期运算符（`before`、`after`、`hours_until_gte`、`hours_until_lte`、`within_hours`）。时间取自请求中的 `now`，缺省时在接收时填入并复制到决策中，因此每次评估都可重放。`regex` 作用于 `ruling_text` 等不可信文本：模式只能由规则作者编写，上限 200 字符，加载时检查嵌套量词，作用于截断且可选归一化的文本，匹配到的条款可用 `{match}` 引用。被强制的 `decide` 现在会与 `forbid` 对照。

```bash
python -m reason rules trader/delphi lint
python -m reason rules trader/delphi test --cases examples/rules.test.example.json   # 通过与否、从未触发的规则、矛盾
```

### 带作用域、有效期与 markdown 知识库的经验

经验现在带有 `tags`、`decision_points`、`valid_from`、`valid_until`、`status` 及其来源。注入时按状态、有效期与作用域过滤，按有益/有害计数的 Wilson 下界排序，最多 6 条。`python -m reason lessons <ns> review` 列出可晋升的候选（经人工 PR 成为 P0 规则）以及需要复查的经验。

设置 `vault_dir` 后，Reason 会从 Obsidian 知识库或 git 仓库读取经验：

```markdown
---
id: V-012
kind: lesson
namespace: trader/delphi
tags: [sport]
decision_points: [before_bet]
valid_until: 2026-12-31
author: human
---
赛前赔率在开球时失效。
```

`doctrine/` 中的文件视为已批准，`postmortems/` 中的视为待审，除非 `status:` 另有说明。没有来源的经验会被隔离，重复 id 会被忽略并报告，过期经验自动退出。在 Obsidian 中批准（修改 `status:` 或移动文件）会在下一次决策时生效。Reason 把新提案写成新文件，**从不修改知识库中已有的文件**；计数保存在 Reason 自己的数据中。

## 三个门面，一个引擎

```mermaid
flowchart TB
    subgraph 宿主
        N[n8n · Make · Zapier<br/>脚本 · LangGraph · Temporal]
        C[Claude Code]
        M[MCP 客户端<br/>n8n MCP Client · Agents SDK · Cursor]
    end
    subgraph Reason 服务
        H[/POST /decide · /outcome · /execute<br/>GET /lessons · /health/]
        K[/POST /hooks/claude/]
        P[/POST /mcp  JSON-RPC，无状态/]
        ENG[[引擎]]
        H --> ENG
        K --> ENG
        P --> ENG
    end
    N -- 放在动作之前的 HTTP 步骤 = 关卡 --> H
    C -- PreToolUse：allow / deny / ask<br/>UserPromptSubmit：注入经验 --> K
    M -- 工具：decide、outcome、execute --> P
```

| 门面 | 保证 | 示例 |
|---|---|---|
| HTTP JSON | 放在动作之前的步骤是真正的**关卡** | `examples/n8n-workflow.json`（HTTP Request → Switch → outcome） |
| Claude Code 钩子 | `PreToolUse` 让 Reason 在每次工具调用前允许、拒绝或询问 | `examples/claude-settings.json` |
| MCP | 宿主智能体自行选择调用的工具：是建议，不是关卡 | n8n MCP Client Tool、Claude Agent SDK、OpenAI Agents SDK |

## 评测闭环

```bash
python -m reason eval acme/invoices -k 4     # 回放 data/<ns>/dataset.jsonl：pass@1 与 pass^4
python -m reason fingerprint                 # 每个模型的确定性指纹；供应商静默变更时报告 CHANGED
```

两者都每天调度（cron、n8n Schedule、GitHub Actions）。关注 `pass^k` 而不是平均值：它才是预测生产表现的数字。

## 不提供自我修改的智能体

Reason 不提供任何会修改自身代码或测试的智能体：能编辑评分标准的模型终究会操纵评分。`scripts/check_diff.py` 是独立的 CI 检查，会在任何 pull request 中标记被改动的测试文件、新增的 `skip`/`xfail` 标记以及被删除的断言。
## 证明它有效

合法的 JSON 决策不等于正确的决策，模型自报的置信度也没有经过校准。Reason 用度量代替断言。

**决策置信度校准。** 每个由模型做出的决策的结果，都会以（置信度，是否正确）记录到 `decision_scores.jsonl`。然后：

```bash
python -m reason calibrate acme/invoices --decisions --target-accuracy 0.9
```

为每个层级拟合 Platt（仅当它在留出的时间切分上有所改善时才保留），报告 AUROC（置信度能否区分对错）和 AURC（风险-覆盖曲线下面积），并在较早的 70 % 结果上为每个决策点学习升级阈值：其 Wilson 下界达到目标准确率的最低校准置信度。该阈值下的覆盖率与准确率随后在拟合从未见过的最近 30 % 上报告。学到的阈值永远不会低于配置的阈值（除非设置 `allow_threshold_below_default`），并在 `calibration_max_age_days` 后过期。被升级的提议在人工回复后仍会计分，因此阈值可以回落。此后决策会带有 `confidence`（校准后）、`confidence_raw`、`threshold` 和 `calibrated`。

**样本一致性。** 设置 `"decide": {"n_samples": 3}`（或在单次请求中设置 `constraints.n_samples`）后，决策器会被多次采样；多数动作胜出，其票数占比成为 `agreement`（按请求的样本数计算，服务商故障会拉低它），有效置信度永远不超过一致性。Jev 这类确定性决策器只调用一次。

**给执行端的意图键。** 每个可执行的决策都带有 `intent_key`，即命名空间、决策、动作与参数的稳定哈希。把它作为 `client_order_id`、`Idempotency-Key` 或 `ClOrdID` 传递：源自该决策的订单每次重发都携带同一个键，下游系统可以据此去重。若要在崩溃后取回同一个决策（从而同一个键），请在请求中带上 `idempotency_key`。Reason 自身的幂等性止于决策；你的执行端在重发之前必须先与券商或服务商对账。

**在留出集上做经验消融。**

```bash
python -m reason eval acme/invoices --compare-lessons wrong_lessons.json --after 2026-10-01T00:00:00Z
```

在相同用例上把回归集重放三次（经验开启、关闭、故意错误），只使用给定日期之后的决策，并用精确符号检验报告配对的胜负。重放是 dry run：不写入日志。每次重放都会追加到 `trials.jsonl`，以便在判断结果时知道一共尝试了多少种变体。

需要牢记的数量级：要区分 55 % 与 50 % 的命中率，大约需要 600 到 800 个独立结果；区分 52 % 与 50 %，大约需要 4 000 到 6 600 个。

**影子模式（冠军 / 挑战者）。** 在配置中加入 `"shadow": {"acme/*": {"decider": "challenger-model"}}` 后，匹配命名空间中每个由模型做出的决策都会由挑战者在后台线程中以 dry run 再决策一次：不执行任何操作，返回的决策不等待它，比较结果写入 `shadow.jsonl`。由规则做出的决策和幂等重放不做影子决策。队列最多保存 1 000 个比较（超出的会被丢弃），其线程不会让进程保持运行，因此影子模式面向 `reason serve`：一次性的 `reason decide` 不会等待就退出，并丢失其比较。`python -m reason shadow acme/invoices` 报告一致率、升级率、已结算决策上的配对胜负及符号检验，以及挑战者在冠军升级的用例上会如何表现。没有人工更正时，成功会把冠军的动作标为正确，这对冠军有利；只有人工更正能无偏地比较双方。

**基于历史的规则模拟。**

```bash
python -m reason rules acme/invoices simulate --candidate rules.new.json
```

在每条已记录的请求上重放候选规则，不调用模型，报告哪些决策会改变（被强制的动作、现在被禁止的动作、被移除的强制规则），并在已结算的决策上报告避免的错误、引入的错误、原本成功的决策上新增的升级，以及现在会被自动化的升级。被禁止的动作按升级计算，尽管下一层可能得出结论；请求按记录时的样子评估，`redact_paths` 字段已被遮蔽，并与当前的 `rules.json` 比较（过去的决策可能遵循旧版本）。

**证明测试台。**

```bash
python -m reason bench --namespace acme/invoices
```

以固定随机种子、无网络的方式批量验证不变量，大约十秒：总是提议被禁止动作或越界参数的恶意决策器（规则），服从 `event`、`state` 或 `history` 中隐藏指令的轻信决策器（注入），10 % 的请求以超时、429、非 JSON 或错误 schema 失败的提供方（提供方），每个幂等键 8 个并发请求（并发），在决策循环的随机时刻 `SIGKILL` 后在同一 `data_dir` 上重启（崩溃），以及一条金丝雀经验加 1 000 个模糊测试的命名空间名称（隔离）。每个套件报告失败数和失败率的 95 % 上界（3 000 个用例无失败：低于 0.1 %）；任何失败都会以退出码 1 结束。`--namespace` 会基于已记录的结果加入基线（Reason 对比始终升级、以及在较早 70 % 上学到的多数动作），`--live N` 衡量所配置的决策器被注入载荷劫持的频率（每个用例至少 2 次计费的模型调用，使用 `n_samples` 或推理器时更多，在一次性的 `data_dir` 中）。

## 安全模型

- 设置了 `REASON_TOKEN` 后，除 `/health` 外一切请求都要求 `Authorization: Bearer`（常量时间比较）。绑定 `127.0.0.1` 或前置代理。
- 请求体上限 1 MB，套接字超时 30 秒，命名空间限制为安全字符（无路径穿越），每个 POST 都校验 `Origin` 头（MCP 的 DNS 重绑定防护）。
- 经验绝不由模型直接写入：提案进入待审队列，由人工批准。每条经验上限 300 字符。
- `state`、`event`、工具输入都是不可信数据。提示词会声明这一点，但提示注入仍然可能：把不可逆动作放在 P0 规则或 `ask_human` 之后。
- 每个命名空间有只追加的日志（`DecisionMade`、`OutcomeRecorded`、`Executed`），启动时重新加载。来自 Claude Code 钩子的工具输入在记录前截断到 4 000 字符。
- 没有认证时服务器拒绝启动（`serve --insecure` 仅允许在本地回环地址上使用）。除管理员 `REASON_TOKEN` 外，`config.tokens` 把密钥映射到命名空间（glob 模式）和作用域（`decide`、`outcome`、`execute`、`estimate`、`read`），在 HTTP、MCP 与钩子中都会校验。每个 `data_dir` 只允许一个 `reason serve`（文件锁）。经验手册与日志的写入在进程之间加锁。
- `redact_paths`（例如 `state.facts.iban`，仅限 `event`、`state`、`history`、`context`、`ruling_text`、`input`）会在所有持久化内容（日志、数据集、评分）中屏蔽敏感字段，而模型仍能看到完整请求；模型理由等自由文本不会被屏蔽；`reason prune <ns> --days N` 删除旧日志事件，仍在等待结果的决策除外。

## 项目结构

```
reason/
  llm.py         兼容 OpenAI 的客户端（urllib），严格或 json_object 输出，重试
  jev.py         Jev（TypeSafe）适配器，相同的 chat() 接口
  lessons.py     按命名空间的经验手册：计数器、去重、修剪、待审队列
  reflector.py   失败 → 经验提案
  bench.py       证明测试台：规则、注入、提供方、并发、崩溃、隔离、基线
  evalrun.py     回归回放（pass@1、pass^k）、模型指纹
  server.py      HTTP、MCP JSON-RPC、Claude Code 钩子
  core.py        引擎：校验、层级、日志、outcome v2、复盘、estimate、execute
  rules.py       P0 规则 v2：path2、all/any/not、日期运算符、受限正则、插值、规则测试
  calib.py       Brier、log-loss、RPS、Murphy 分解、Platt、Wilson、bootstrap（标准库）
  poller.py      延迟结果：JSONL / HTTP / GraphQL 来源、水位线、匹配、时限
  vault.py       markdown 知识库：frontmatter 经验、提案、复盘（从不改写已有文件）
  __main__.py    CLI：serve decide outcome execute estimate estimate-outcome lessons rules eval outcomes calibrate poll pending fingerprint bench shadow init
scripts/check_diff.py        PR diff 的测试篡改检测器
examples/                    配置、请求、n8n 工作流、Claude Code 设置
tests/test_reason.py         60 个离线测试
```

```bash
python -m unittest discover -s tests -v
```

## 路线图

- `/v1/chat/completions` 门面，服务只会与"模型"对话的宿主。
- 评测保持通过时自动晋升经验；按标签做金丝雀流量分配。
- 经过验证的 n8n 节点和 A2A Agent Card，作为同一 API 的轻客户端。
- 本地 JEV-9B 适配器（vLLM、logprobs），自托管的类型化决策器。

## 背景

Reason 基于对 2023 至 2026 年智能体可靠性文献的综述设计：context rot（Chroma）、"LLMs Get Lost in Multi-Turn Conversation"、MAST、METR 关于 reward hacking 的研究、ImpossibleBench、Darwin Gödel Machine、Agentic Context Engineering（ACE）、GEPA、OWASP Top 10 for Agentic Applications，以及 Anthropic 关于长时运行智能体的工程笔记。

MIT 许可证。
