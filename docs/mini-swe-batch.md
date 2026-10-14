# mini-SWE-agent 三题对照入口

`scripts/run_mini_swe_batch.py` 在独立 WSL 环境中调用固定官方源码的 `DefaultAgent + LitellmModel + DockerEnvironment`。包装层只管理凭证、资源、日志、结果与清理，不改模型提示、原生循环、工具解析、历史管理或重试。

## 固定来源和设置

- mini-SWE-agent **2.4.6**，提交 `04d809ceab9df28f9adaed044884180159172930`，在 `artifacts/vendor/mini-swe-agent-04d809c`；启动核对 Git SHA、tracked diff 和实际 import 路径。
- 原配置 `src/minisweagent/config/benchmarks/swebench.yaml`：**250 步、3 美元**；继承 AgentConfig 连续格式错误上限 3、原生 wall time 0（不限时）。不要与 `DefaultAgent` 单独实例化时 step_limit=0 的类默认混淆。
- 模型 DeepSeek Flash/high、单次最大输出 32768、请求超时 180 秒。LiteLLM 同时保留 `reasoning_effort=high` 和 `extra_body.reasoning_effort=high`，后者确保传到 DeepSeek 的请求体含具体推理强度。实际参数及离线 HTTP 检查由 `artifacts/vendor/mini-swe-deepseek-settings.json` 与验证产物固定。
- 保留 native `cost_tracking=default`；设置 `LITELLM_LOCAL_MODEL_COST_MAP=True` 使用安装版本自带价格表，缺失价格会失败，不能悄悄忽略费用限制。模型请求仍使用原生重试器，默认最多 10 次请求尝试；一次题目不会重新生成挑选答案。
- 外层宿主计时 **900 秒**，包括拉镜像、环境准备与运行；清理另有有界宽限。宿主保护与 native wall_time 默认分别记录；effective-config/result 明示 deadline_scope=environment_setup_and_agent，result 另存 agent_seconds（不含准备和清理）以便比较。**没有额外 30 步或百万 token 限制。**
- Docker 默认无网络、2 CPU、4 GiB、256 PID，无宿主目录挂载。复用官方镜像的 Bash/测试环境，不构建我们的补丁 SDK 工具层。

## 行为与边界

`load_tasks` 仅投影安全字段，并要求三个唯一任务；读取固定 `artifacts/swe-bench-first3/tasks.jsonl`，不读取 gold 或 grader-only 文件。每题创建新容器。只读验证 HEAD 是题目 base，或它的唯一直接子提交且标题为 SWE-bench；检查干净工作区，保留安装提交，绝不 reset。

`protected_classes` 提供三个薄适配：密钥只在 `_query` 参数中临时注入，既不进入模型配置也不写入环境变量；`save` 对保存的 native trajectory 脱敏；Docker cleanup 同步校验本次唯一 ownership label 后移除容器。它们不重写 native query/step/run 或 observation 格式。

`run_case` 执行一题并在关闭环境后最后发布 result。**主预测只使用官方 `agent.run()` 返回的 submission**。mini 原生 runner 遇预算终止时不会自动提交工作区差异；包装层也不这么做。`diagnostic-workspace.patch` 保存当前 tracked 差异，仅供诊断，不混入官方主分数。这与我们在预算结束时自动交补丁的规则不同，是需要报告的行为差异。

环境清理只查询本次随机 label；镜像只在本次开始不存在且当前 ID 等于本次拉取 ID 时非强制删除。没有 global prune。原生 shell 工具的命令超时和输出行为保持原样；外层计时终止后会移除容器。运行期间不与评分程序争用同一题镜像。

## 入口及产物

在 `/opt/mini-swe-agent-2.4.6/bin/python` 下执行，显式传入 tasks/output/settings/env-file/vendor；输出 `artifacts/mini-swe-first3` 必须是新目录。默认每题一次、串行，不自动恢复。

根目录保存 plan、progress、batch-result、predictions；每题 `runs/<id>/` 保存 task、source、effective-config、native trajectory、changes.patch、prediction、diagnostic-workspace.patch、result。plan 包含官方与包装源码哈希、依赖版本、完整有效配置和预算区别。result 记录原生 stop_reason/cost/calls、tokens、请求适配次数、主补丁 SHA256；评分程序核对与官方 patch.diff 一致。

`generated` 仅表示 native submission 非空，不代表修复成功；`empty_patch`、`invalid_response`、`setup_error`、`agent_error` 单列。只用官方 grader 做正确性验收，主分母固定三题。

用户取消记录 agent_error/outcome=cancelled 并停批，不计成已知空补丁能力失败。

## 验证

`tests/test_mini_swe_batch.py` 使用纯离线替身，覆盖 native submission 优先、预算停止不拿诊断补丁顶替、凭证不入配置/日志、资源配置、精确清理、输入白名单和异常分类。真实模型调用与官方评分结果单独报告；离线测试通过不能视为 mini 已修复任务。

## 本次三题实际结果

固定原顺序前三题的任务文件 SHA256 为 `c174e7dafc19fe9265d9062738f712fc852fe17b9bc1d8ff8119f3be65612c20`。三题均只生成一次，原生终止原因全部是 `Submitted`；没有预算停止、额外重跑或使用诊断补丁替代提交。

| 任务 | 原生调用数 | 累计 token | Agent 用时 | 官方验收 |
| --- | ---: | ---: | ---: | --- |
| astropy-12907 | 28 | 569,698 | 161.014 秒 | 通过，F2P 2/2、P2P 13/13 |
| astropy-14182 | 64 | 3,641,588 | 376.475 秒 | 通过，F2P 1/1、P2P 9/9 |
| astropy-14365 | 16 | 212,389 | 54.412 秒 | 未修复，F2P 0/1、P2P 8/8 |

总计 108 次调用、4,423,675 token，其中缓存输入 4,246,400 token、模型输出 69,616 token；原生 LiteLLM 价格表估算费用合计约 **0.07066 美元**，不代表账单结算额。Agent 用时合计 591.901 秒，含准备和清理的逐题耗时合计 600.2 秒。

官方最终 **2/3**，无基础设施失败，三份主预测均核对生成补丁与官方 `patch.diff` 的 SHA256 一致。第三题补丁能应用，但目标测试失败。产物分别在 `artifacts/mini-swe-first3` 与 `artifacts/official-mini-swe-first3`，后者的 `progress.json` 保存逐题测试结果、报告哈希及补丁绑定记录。两侧进程已正常结束；没有留下本次运行容器。

这是三题调试对照，不能当作完整 SWE-bench Lite 成绩。mini 使用原生 250 步/3 美元配置，我们当轮使用 30 步/百万累计 token；提示、工具、历史与提交规则也不同。第二题 64 次调用表明 30 步可能限制了探索空间，但仅这组结果不能证明加步数就一定修好；第三题 mini 16 次提交仍未通过。

离线入口验证为 8 项测试通过，真实生成与官方评分证据如上，三者分别记录。
