# Agent 批量生成入口

`scripts/run_agent_batch.py` 按输入顺序逐题运行完整 Agent：模型读代码、调用工具、改代码、测试、压缩上下文。一次只运行一个容器；模型结束或预算耗尽后都导出补丁。官方评分由独立入口完成，`finish` 和非空补丁都不代表修对。

## 固定本次实验

使用 `artifacts/swe-bench-first30/tasks.jsonl` 的全部 30 题，保持原 300 题顺序。输入 SHA256：`6bf3a603e53cc0f4e9a1afa5c3c1d26d6befdcc5f3576c776b050a58f54a5276`。核心固定提交 `05359aeddfb57e601ff67a5b3567c15a53a29cb3`；批量脚本单独记录所在提交和文件哈希。

| 参数 | 本次固定值 |
|---|---:|
| 模型 / 推理档 | deepseek-flash / high |
| 单次最大输出 / 请求超时 | 32768 token / 180 秒 |
| 每题循环 / 时间 / 总 token | 30 步 / 900 秒 / 100000 |
| 上下文软上限 / 最近回合预算 | 24000 / 6000 |
| 并发 | 1 |

900 秒是 Agent 开始后的预算；拉镜像、构建工具层和准备容器单独计入总耗时。摘要调用也计入 Agent 的时间和 token。裸模型使用同一模型与推理档，但只请求一次、固定检索 64000 字符、没有工具与测试反馈；不能把这次比较说成相同总算力预算。

## 运行方式

显式传入 settings JSON 和本地密钥文件，输出目录必须不存在：

~~~powershell
.venv\Scripts\python.exe scripts\run_agent_batch.py --tasks artifacts\swe-bench-first30\tasks.jsonl --output artifacts\agent-first30 --settings artifacts\agent-first30-settings.json --env-file .env --source-commit 05359aeddfb57e601ff67a5b3567c15a53a29cb3
~~~

需要 Docker Linux engine，宿主进程 PATH 能找到 docker。密钥通过指定文件的 `dotenv_values(interpolate=False)` 读取，不搜索其他文件、不写入进程环境、不传容器。输入只经过 `Task` 字段白名单，不读取 grader-only 数据、答案补丁或隐藏测试。

## 组件与约定

- `source_manifest(commit)`：运行前及每题前，对照完整 Git SHA 核验所有顶层核心 Python 文件和 pyproject；换行统一后比较。记录核心文件哈希、脚本哈希和当前 wrapper 提交，源码变化则停止。需要在项目的 editable 安装环境调用。
- `BenchmarkDocker(task, output, settings)`：每题新容器，复用现有 Docker 环境与工具层，无宿主目录挂载、网络默认关闭。无需复制宿主仓库快照；仓库来自官方预制镜像。
- 镜像原 HEAD 可以等于题目的 base commit；否则只接受**单个父提交恰好为 base、标题为 SWE-bench 的安装提交**。记录镜像 ID/digest、HEAD、父提交、安装修改路径，并严格验证干净工作区。保留安装提交作为补丁基线，不执行 checkout/reset/clean，避免撤销官方 Astropy 的安装配置。此判定用于当前官方镜像；未知历史结构作为环境错误报告。
- `run_one`：读取仓库根目录 AGENTS.md、调用原有 Agent、无论结束原因都尝试导出补丁，最后关闭模型和容器。原始日志及 V2 Evidence 保留。错误只记录阶段和异常类型，不记录可能含凭证的异常文本。
- `run_batch`：每题仅生成一次；不恢复 Agent 会话、不对失败题重新生成挑优。普通单题失败保留记录后继续，取消、清理失败或源码漂移则停止，未执行题仍在 predictions 中占位。模型 SDK 原有请求行为不变。
- 镜像只清理本题新产生且 ID 未变化的精确 tag，先工具层后基础镜像；不使用 prune、不强制删除镜像、不清理启动前已存在的镜像。容器清理由原环境的 ownership label 校验负责。须与官方 grader 串行协调，避免另一进程同时依赖本题镜像。

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

