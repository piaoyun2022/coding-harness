# mini-SWE-agent 三题对比：独立运行环境与预算

查证日期：2026-10-14。本文记录准备状态和原生行为，不预报评测结果。官方下载源码已保存到 Git 忽略的 `artifacts/vendor/mini-swe-agent-04d809c`，HEAD 已核验为 `04d809ceab9df28f9adaed044884180159172930`；源码和当日 PyPI 最新版本均为 **2.4.6**。

## 独立安装与可复现材料
WSL发行版 `coding-harness` 中使用 `/opt/mini-swe-agent-2.4.6` 虚拟环境，从固定 vendor 源码 editable 安装。实际依赖为 LiteLLM **1.105.1**、OpenAI **2.54.0**、Pydantic **2.14.1**。不修改主项目 `.venv`、核心 `src/` 或官方 SWE-bench 评分环境。

本地产物：
- `artifacts/vendor/mini-swe-install.json`：固定 SHA、依赖版本、模型参数验证及价格快照。
- `artifacts/vendor/mini-swe-requirements.freeze.txt`：独立环境完整依赖快照。
- `artifacts/vendor/mini-swe-deepseek-settings.json`：仅模型覆盖，不含密钥、不覆盖原生 agent 步数/费用限制。

这些配置/元数据属于实验产物，不将第三方源码复制进主产品代码。实验执行入口与官方评分适配由独立 wrapper 负责；本文不宣称模型或评测已运行。

## 原生限制：不能把几个“默认”混为一谈
以下行号来自已下载固定 SHA 的本地源码，避免网页缓存版本行号变化。

| 入口/配置 | 步数 | 费用 | 时间 | 证据 |
|---|---:|---:|---:|---|
| `AgentConfig` 类默认 | 0，不限制 | 3美元估算值 | 0，不限制 | `src/minisweagent/agents/default.py:26–33` |
| 当前 `mini` CLI 使用的 `mini.yaml` | 0，不限制 | 3美元 | 继承0 | `run/mini.py:22`；`config/mini.yaml:101–103`，交互模式 confirm |
| 旧 `default.yaml` 配置文件 | 0，不限制 | 0，不限制 | 继承0 | `config/default.yaml:104–105`；这不是当前mini CLI选用的默认文件 |
| 官方 SWE-bench 配置 | **250** | **3美元** | 继承0 | `config/benchmarks/swebench.yaml:112–113` |
| 本次 mini 对比适配 | **保留250** | **保留3美元** | 外层900秒保护 | 外层时间上限是本次适配，不冒充原生默认 |

本次不添加我们 harness 的30步或累计100万token限制。两者预算策略不同，最终对比须同时给出停止原因、调用量/token/耗时，不能把结果差异全部归因于循环架构。

## 循环与停止语义
`DefaultAgent.run` 在 `agents/default.py:88–124` 建立system/task消息并循环 `step()`；`step` 在126–128行为 query 后执行工具。`query` 在130–152行于每次新模型调用前检查步数和已累计估算费用，再检查墙钟限制，然后增加 `n_calls`。因此250步是逻辑模型调用数，包含格式错误的调用；一轮可有多个bash动作，实际执行命令数可能更多。

成本在模型返回后累计，所以3美元是操作边界阈值，不是绝不越过的账单硬上限。连续格式错误默认3次，达到后以 `RepeatedFormatError` 结束（32–33、100–114行）。Provider 层还可能重试：`models/utils/retry.py:19–24` 默认最多10次，指数等待4–60秒；认证/权限/不支持参数/上下文超窗等异常不重试（`models/litellm_model.py:50–56`）。这不等于工具写操作会自动重放。

正常结束来自环境识别成功命令输出：首行是 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 且returncode为0，其余内容为提交补丁（`environments/docker.py:140–150`）。SWE模板要求先生成并检查patch，再单独提交。预算或连续格式错误退出的原生 `submission` 为空；保留下来的工作区差异只能另记诊断，不应冒充已提交预测。

## DeepSeek适配及无网络验证
加载官方 `config/benchmarks/swebench.yaml`，保持原始system/instance提示和agent设置，仅覆盖模型为 `deepseek/deepseek-flash`，API base为 `https://api.deepseek.com`，输出上限32768、请求超时180秒、推理强度high。费用跟踪保持 `default`，不使用会隐藏错误的 `ignore_errors`。

LiteLLM 1.105.1 的DeepSeek mapper会把 `reasoning_effort` 转成 `thinking.enabled`，但移除具体强度。因此覆盖配置同时提供 `extra_body: {reasoning_effort: high}`。已在拦截所有HTTP并禁用DNS的离线替身中核验最终请求体同时包含：

```json
{"model":"deepseek-flash","reasoning_effort":"high","thinking":{"type":"enabled"},"max_tokens":32768,"parallel_tool_calls":true}
```

返回的reasoning_content也被保存，历史准备仅移除extra字段。检查没有使用真实密钥或发送真实模型请求。密钥只能在宿主机进程内读取并注入请求，不能放到会被serialize的model_kwargs、产物或Docker env/forward_env。

## 费用表与缓存
官方 [DeepSeek价格页](https://api-docs.deepseek.com/quick_start/pricing/) 当日Flash价格与LiteLLM包内表一致：每百万token高峰输入未命中0.30美元、缓存命中0.006美元、输出1.20美元，闲时为一半。本次运行可设置 `LITELLM_LOCAL_MODEL_COST_MAP=True` 固定已安装包的价格快照，避免中途远程价格表更新。

合成响应的离线计价验证：闲时1000输入/100输出、无缓存为0.00021美元；其中800输入命中缓存时为0.0000924美元，成本为正且缓存折扣有效。不需要额外注册自定义价格。估算受供应商时段规则及LiteLLM实现影响，不能称为账户账单；模型成本仍应与原始usage一起保存。

## 任务和镜像边界
只从 `artifacts/swe-bench-first3/tasks.jsonl` 读取已净化的原顺序前三题，将 problem_statement 交给模型；不让官方整份dataset中的标准补丁、测试补丁或FAIL_TO_PASS等grader字段进入模型进程输入。评分在另一进程使用对应pinned grader数据。

直接使用已有官方SWE镜像安装状态，不reset到dataset base：官方setup在base上安装并可创建额外SWE-bench提交，默认mini也不reset。记录镜像初始HEAD与任务base关系，保留setup修改；原生提交的git diff基于镜像HEAD。环境仅运行题目副本，不挂载密钥或grader目录。Docker资源/网络限制、清理及宿主900秒保护属于运行适配，应在实验记录中明示。
