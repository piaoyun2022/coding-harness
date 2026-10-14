# Agent 批量生成入口

`scripts/run_agent_batch.py` 按输入顺序逐题运行完整 Agent：模型读代码、调用工具、改代码、测试、压缩上下文。一次只运行一个容器；模型结束或预算耗尽后都导出补丁。官方评分由独立入口完成，`finish` 和非空补丁都不代表修对。

## 固定本次实验

最初计划使用 `artifacts/swe-bench-first30/tasks.jsonl` 的全部 30 题，后按用户要求先验证其中原顺序前三题（见下文最新范围）。原 30 题输入 SHA256：`6bf3a603e53cc0f4e9a1afa5c3c1d26d6befdcc5f3576c776b050a58f54a5276`。核心固定提交 `05359aeddfb57e601ff67a5b3567c15a53a29cb3`；批量脚本单独记录所在提交和文件哈希。

| 参数 | 本次固定值 |
|---|---:|
| 模型 / 推理档 | deepseek-flash / high |
| 单次最大输出 / 请求超时 | 32768 token / 180 秒 |
| 每题循环 / 时间 / 总 token | 30 步 / 900 秒 / 1000000 |
| 上下文软上限 / 最近回合预算 | 24000 / 6000 |
| 并发 | 1 |

900 秒是 Agent 开始后的预算；拉镜像、构建工具层和准备容器单独计入总耗时。摘要调用也计入 Agent 的时间和 token。裸模型使用同一模型与推理档，但只请求一次、固定检索 64000 字符、没有工具与测试反馈；不能把这次比较说成相同总算力预算。

## 运行方式

显式传入 settings JSON 和本地密钥文件，输出目录必须不存在：

~~~powershell
.venv\Scripts\python.exe scripts\run_agent_batch.py --tasks artifacts\swe-bench-first30\tasks.jsonl --output artifacts\agent-first30-1m --settings artifacts\agent-first30-1m-settings.json --env-file .env --source-commit 05359aeddfb57e601ff67a5b3567c15a53a29cb3
~~~

需要 Docker Linux engine，宿主进程 PATH 能找到 docker。密钥通过指定文件的 `dotenv_values(interpolate=False)` 读取，不搜索其他文件、不写入进程环境、不传容器。输入只经过 `Task` 字段白名单，不读取 grader-only 数据、答案补丁或隐藏测试。

## 组件与约定

- `source_manifest(commit)`：运行前及每题前，对照完整 Git SHA 核验所有顶层核心 Python 文件和 pyproject；换行统一后比较。记录核心文件哈希、脚本哈希和当前 wrapper 提交，源码变化则停止。需要在项目的 editable 安装环境调用。
- `BenchmarkDocker(task, output, settings)`：每题新容器，复用现有 Docker 环境与工具层，无宿主目录挂载、网络默认关闭。无需复制宿主仓库快照；仓库来自官方预制镜像。
- 镜像原 HEAD 可以等于题目的 base commit；否则只接受**单个父提交恰好为 base、标题为 SWE-bench 的安装提交**。记录镜像 ID/digest、HEAD、父提交、安装修改路径，并严格验证干净工作区。保留安装提交作为补丁基线，不执行 checkout/reset/clean，避免撤销官方 Astropy 的安装配置。此判定用于当前官方镜像；未知历史结构作为环境错误报告。
- `run_one`：读取仓库根目录 AGENTS.md、调用原有 Agent、无论结束原因都尝试导出补丁，最后关闭模型和容器。原始日志及 V2 Evidence 保留。错误只记录阶段和异常类型，不记录可能含凭证的异常文本。
- `run_batch`：每题仅生成一次；不恢复 Agent 会话、不对失败题重新生成挑优。普通单题失败保留记录后继续，取消、清理失败或源码漂移则停止，未执行题仍在 predictions 中占位。模型 SDK 原有请求行为不变。
- 镜像只清理本题新产生且 ID 未变化的精确 tag，先工具层后基础镜像；不使用 prune、不强制删除镜像、不清理启动前已存在的镜像。容器清理由原环境的 ownership label 校验负责。可以与不同题目的官方 grader 各运行一路；须协调同题镜像的使用和清理，避免另一进程仍依赖本题镜像时删除。

## 产物与状态

批次保存 `plan.json`、`tasks.jsonl`、`settings.json`、`progress.json`、`predictions.jsonl`、`batch-result.json`。计划记录原输入及规范化任务哈希、settings 哈希、源码版本和工具反馈模式。

每题在 `runs/<instance_id>/` 保存 `task.json`、`source.json`、`trajectory.jsonl`、`evidence/`、`changes.patch`、`prediction.json`、`result.json`。环境构造失败时可能没有 source/trajectory/evidence；结果明确为 setup_error。补丁哈希绑定实际导出的字节，官方评分还需核对其 patch.diff 哈希。

| result.status | 含义 |
|---|---|
| generated | 成功导出非空候选；即使 outcome 为 budget_exhausted，也照常官方评分 |
| empty_patch | 已运行但没有非空补丁，不把模型声称完成算正确 |
| invalid_response | 模型截断或非法消息/工具调用，没有可导出补丁 |
| setup_error | 环境、规则读取、补丁导出或其他包装层失败 |
| agent_error | 核心 Agent 运行异常且没有补丁；保留 stop_reason，作为未判明失败单列 |

`mode=agent` 与原始 `outcome/stop_reason` 都保留。批次结果只统计生成与资源用量，`graded=false`、`resolved_rate=null`；评分系统不得把 generated 当作通过，也不得丢弃预算耗尽时已生成的补丁。

## 验证与局限

`tests/test_agent_batch.py` 使用真实 Agent 循环、假模型/环境和临时本地 Git；覆盖顺序、失败不漏题、预算停止补丁、模型输出异常分类、清理失败停止、源码冻结、精确 dotenv、安装提交校验和镜像清理。2026-10-14：17 项离线测试通过；不联网、不调用模型、不启动 Docker。

已有一次 Astropy12907 Docker 预检保存在 `artifacts/agent-first30-preflight.json`，证明官方镜像存在 synthetic 安装提交和 testbed Python。该旧预检曾在临时容器 checkout base，容器与当次镜像已清理；正式入口已改为保留经过验证的安装提交。它不等于完整 Agent30 或官方评分成功。

## 百万 token 实验的范围变更

2026-10-14，用户明确将下一轮每题累计 token 预算提高到 1000000。旧 100000 预算试跑保留在 `artifacts/agent-first30`，停止后续排队；它的部分结果不能拼接成新实验成绩。新实验从相同顺序的第一题开始，每题生成一次，输出 `artifacts/agent-first30-1m`，独立官方评分输出 `artifacts/official-agent-first30-1m`；不在两轮之间挑选较好补丁。设置差异和范围变更保存在 `artifacts/agent-1m-scope-change.json`。

唯一模型运行参数变化是 `max_total_tokens`：100000 → 1000000。30 步、900 秒、单次输出 32768、上下文预算和冻结核心均不变。一百万是所有 API 调用输入加输出的累计上限，包含缓存命中的输入；它不是单次输出额度或上下文窗口。当前实现按回复后的实际用量判断停止，未在发请求前预留该次输入，因此可能略超累计上限；本次配置实验不更改这段语义。步数、时间也可能先于 token 触发停止。

裸模型已有的前三十题生成与官方验收继续沿用，不为这次 Agent 预算变化重新生成。两组比较仍需单列不同的多轮资源预算。

## 最新范围：先验证前三题

同日用户进一步要求：百万预算先只跑原顺序前三题，跑不通时可以根据实际预算不足继续调大。当前百万实验不重新生成已开始的题目，在第三题结束后停止后续任务；原运行目录 `artifacts/agent-first30-1m` 留存实际记录，前三题另聚合为 `artifacts/agent-first3-1m`，独立评分目录为 `artifacts/official-agent-first3-1m`。这次验收分母为3，未执行的其余27题既不是失败，也不得补到本次成绩。题目与评分数据分别为 `artifacts/swe-bench-first3/tasks.jsonl` 和 `artifacts/grader-only/swe-bench-lite-first3-pinned.json`。

先确认停止原因：token、步数或时间不足时，可按用户授权在独立新实验增加对应预算，保留旧结果；补丁错误或环境故障不能被简单归因为token不足。不拼接不同预算的最好结果，不把重复尝试的通过率称为单次通过率。当前这一轮仍固定30步、900秒、1000000累计token。


## 下一轮配置：300 动作轮、500 万累计 token（未运行）

用户授权的下一轮配置独立保存在 `artifacts/agent-first3-300steps-5m-settings.json`。以百万实验的 `artifacts/agent-first30-1m-settings.json` 为基础，**仅修改 `max_steps`：30 → 300，以及 `max_total_tokens`：1000000 → 5000000**。900 秒、DeepSeek Flash/high、单次输出 32768、请求超时 180 秒、上下文与证据限额等其他字段完全不变。300 指 Agent 动作循环上限，不等于工具调用次数；500 万仍是所有模型调用累计输入加输出，包含缓存输入。

本次只保存并校验配置，**尚未启动模型、Docker 容器或官方评分**；没有更改冻结核心，也没有覆盖旧结果。真实 `Settings` 校验已通过，逐字段比较确认只有上述两项不同。动作轮、累计 token 和 900 秒三个限制仍由最先触发者结束任务；提高前两项不保证 900 秒内能走满 300 轮。

实验题目边界仍为 `artifacts/swe-bench-first3/tasks.jsonl` 中原顺序的 astropy-12907、astropy-14182、astropy-14365，任务 SHA256 为 `c174e7dafc19fe9265d9062738f712fc852fe17b9bc1d8ff8119f3be65612c20`，分母仅为 3。后续如启动，使用全新输出目录与独立评分记录，不与旧百万实验或 mini-SWE-agent 对照结果混合、挑优；其他 27 题不属于本轮范围。上下文交接和提交规则的修复仍另行讨论，此配置文件不表示它们已经修改或验证。
