# benchmark：题目隔离、单次生成与评分账本

对应源码：`src/coding_harness/benchmark.py`；对应测试：`tests/test_benchmark.py`。

## 为什么单独放在核心循环之外

评测程序只提供任务、接收预测并读取官方验证结果。Agent 不接触标准补丁、评分测试、FAIL_TO_PASS/PASS_TO_PASS 名单或 issue hints。Benchmark 不调用 Agent 内部循环，也不在宿主机应用或执行模型补丁。

裸模型基线明确定义为：问题文字 + 在 base commit 上确定性检索得到的源码 → 一次模型请求 → unified diff。没有工具，没有测试反馈，没有失败后重试生成。它衡量这一固定检索设置下的模型表现，不是与检索无关的“模型纯能力”。

## 数据和公开方法

- `Task.from_row(row)`：白名单投影为 instance_id、repo、base_commit、problem_statement、version、environment_setup_commit。拒绝危险 ID、非完整基线 SHA、空问题；忽略其他字段。
- `prepare_dataset(output, revision=REVISION, limit=None)`：读取固定 SHA 的官方 test parquet，默认全 300 题，按 instance_id 排序。下载原始 bytes 只暂存在主进程内存；落盘只有安全题目和 manifest。manifest 保存源 URL、revision、原始 SHA256、安全任务文件 SHA256、选题规则。输出目录必须不存在。
- `load_tasks(path)`：投影、唯一性验证；同目录存在 manifest 时核验任务文件 SHA256。LF UTF-8 写入保证 Windows/Linux 校验和一致。
- `prepare_grading_data(output, manifest_path)`：给**独立评分器**下载同 revision 的完整官方行，核对原始 SHA256 后输出 JSON。该文件含官方验证材料，只能保存在 grader 的目录，不得挂载进 Agent 环境。不会调用或应用标准补丁。
- `retrieve_context(task, source, max_chars=64000)`：只运行 Git 只读命令，从指定 commit 的 blob 读取源码；不读取当前工作树修改，不运行仓库程序。问题中至少 3 个字符的标识词构成查询；路径命中加权、内容词频封顶，以路径打破同分。先按路径相关性最多选 2000 文件、32 MiB 原文；排除大于 128000 bytes、symlink/submodule、二进制和常见凭证文件；批量读取 blob 后排序。每文件最多取前 16000 字符，总量受 max_chars 约束。返回精确上下文、选中文件、截断状态、内容 SHA256 和检索版本。
- `generate_baseline(task, source, model, settings, output)`：建立新产物目录，记录安全 task、retrieval、完整 request/settings、模型 message、usage、耗时、prediction 和 changes.patch。一次 `Model.complete`，tools 为空，purpose 为 baseline；模型适配器自己的网络重试不等同于第二次尝试作答。输出上限来自 settings，检索后扣除已用时间再发请求。模型调用失败仅记错误类型，避免异常字符串携带密钥。截断回答、意外工具调用和无法识别的补丁保留为空预测，不补问、不修补 hunk。
- `write_predictions(tasks, run_dir, output, model_name=...)`：将每题 prediction 聚合为官方 JSONL。缺失或失败题仍写空补丁，因此不能靠丢题抬高分数。输出必须不存在。
- `read_official_reports(root)`：读取一次官方 run 目录下的 report.json；同一题出现两个报告立即报错，不混用不同版本结果。同时从同目录实际 patch.diff 计算 _evaluation_patch_sha256，绝不相信报告 JSON 自带的该字段。缺少 patch.diff 时记录为无法绑定。
- `summarize(tasks, reports, run_dir=None)`：提供 run_dir 时，将每题当前 prediction.json（无此文件则使用 changes.patch）与官方实际评分补丁比较 SHA256；当前两个补丁产物同时存在时也必须一致。ID 不符、产物缺失、哈希不符均归为 unverified_reports，不计 resolved/graded，报告 provisional。不提供 run_dir 时允许仅统计所给官方报告，但明确 score_scope=provided_reports_only、comparison_to_current_run=false，不声称结果对应当前生成。保持 planned 分母，分别汇总 attempted、graded、resolved、generation_errors、empty_predictions、missing_reports、unattempted 和明确的 infrastructure_errors。只有官方报告的 resolved=true 算完整解决。修复比例和回归保留比例按“每题比例再平均”，各自显示实际有测试明细的题目数量。
- `evaluation_preflight()`：要求 Linux Python、固定 swebench 4.1.0 和可用 Linux Docker engine。只预检，不安装或启动服务。
- `evaluation_command(predictions, grading_data, run_id, workers=1)`：返回官方模块 CLI 的 argv，不执行。并发默认 1，合法范围 1–16；每题验证超时 1800 秒。每套补丁必须使用新的 run_id，避免官方评测器复用旧报告。

## 评分语义

`resolved_rate_planned = 官方 resolved 数 / 计划题数`。基础设施失败、模型 API 失败、未生成和未评分都不能从分母删除。存在这些不完整状态时，`provisional=true`、`official_score_available=false`；不能把 0/300 叫成已测得的能力零分。即使全是空预测，只要没有任何官方报告，也不宣布 official_score_available。

空或无效模型回答单列 empty_predictions，是生成失败，不算基础设施故障，也不假装等待一个官方会跳过的空补丁报告。合法非空补丁尚无报告才列 missing_reports。缺失官方报告只标记 missing，不猜测它是代码失败还是 Docker 故障。v4.1.0 某些失败只写日志不写 report.json，需要人工检查；明确提供 infra_failure 的报告会单列基础设施故障。

诊断修复比例来自官方 tests_status.FAIL_TO_PASS 的 success/(success+failure)，通过测试保留比例来自 PASS_TO_PASS。它们不是官方榜单分数，不提供给模型，也不把缺失测试明细当作零或一。

## 限制与验证

源码检索是明确而简陋的词法基线；长文件截断、最多扫描文件量和题目措辞会影响成绩。上下文按字符限额，不能视为精确 token 计数；实际 usage 另外保存。默认单次请求预算不代表完整 300 题的总费用上限，批量启动方应固定模型配置并明确计划题数。

数据 revision 已固定；模型别名、Docker 镜像 latest 仍可能变化。正式比较应保存每次运行配置、SDK/评分器版本及镜像 digest。不得把官方 v5 的新 dataset/image schema 与此 v4.1 adapter 混用。

离线测试覆盖：字段白名单、ID 校验、Git 基线读取、凭证与 symlink 过滤、检索确定性、一次无工具调用、源码不被修改、失败不重试、LF 校验和、评分数据校验和、固定分母、基础设施/未评分分离、重复报告拒绝和官方命令参数。真实下载和真实模型/评分结果分别在运行产物中记录，离线通过不代表能力分数。



## 报告与生成绑定回归

测试证明：旧补丁成功报告不能让一个不同的新补丁得到成功分数；相同补丁可绑定；缺失官方 patch.diff、当前 prediction 与 changes.patch 冲突都会阻止正式出分。单独统计官方报告时输出未比较当前运行的范围声明。空回答和截断回答不会被计为基础设施错误，计划分母保持不变。
