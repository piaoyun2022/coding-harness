# 实测结果与证据范围

**冻结 C 版在固定前 30 题解决 20 题（66.7%）；同题裸模型基线解决 9 题（30.0%）。这是开发用子集成绩，不是完整 SWE-bench Lite 榜单分数。**

这些结果来自发布前的真实运行。C 核心版本为 098e27d19042e28f0592dd27dc76ab7859bb19ff；本次新增的公开一键入口、发布包装和文档没有因此获得一次新的模型评测。后续运行应写入独立结果目录，不覆盖这里的历史数据。

## 数据和计分

- 数据集：princeton-nlp/SWE-bench_Lite，test split，固定 revision 6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2。
- 子集：该固定版本先按 instance_id 升序排序，再取任务 manifest 前 30 题，6 题 Astropy、24 题 Django；前三题对照就是其中最前面的三题。这是 prepare_dataset 的排序约定，不是数据文件天然行序。没有随机抽样，也没有按结果选题。
- 模型：所有组的动作/裸模型请求使用 deepseek-flash、reasoning_effort=high，每次请求最多输出 32,768 tokens；Harness 的摘要请求使用 reasoning_effort=none，并有独立摘要输出额度。模型名是当时服务端别名，不声称锁定不可变权重。
- 验收：Linux Docker 上的 swebench==4.1.0 官方验证器，每个候选独立 run ID；主分数为官方 resolved 数量 / 固定计划题数。官方工具的执行方式见 [SWE-bench Evaluation](https://www.swebench.com/SWE-bench/guides/evaluation/)。
- 空补丁、截断的无效输出、已绑定候选的补丁应用失败保留在分母。未知基础设施状态单列；本报告各组未知数均为 0。finish 或 Submitted 不是通过判据。
- Agent 接收任务、仓库及自身工具反馈，官方验收数据独立交给 grader。本公开包不分发 gold patch、验收测试、测试名称或完整测试日志。

任务 ID、仓库与基准提交见 [tasks-first30.json](../results/tasks-first30.json)。所有汇总见 [summary.json](../results/summary.json)。

## 同 30 题：裸模型与 C 版

| 配置 | 解决 | 有官方报告 | 其他已知失败 | 模型调用 | 总 tokens |
| --- | ---: | ---: | --- | ---: | ---: |
| 裸模型：固定检索、单次输出补丁 | 9/30 | 13（9 通过、4 未通过） | 8 补丁应用失败、9 输出截断 | 30 | 1,136,926 |
| C：上下文调整 + 收尾提示 | 20/30 | 24（20 通过、4 未通过） | 6 空补丁 | 1,101 | 12,492,812 |

裸模型通过仅依赖 issue 文本的固定词法检索选择最多 64,000 字符的仓库上下文；一次请求，无工具调用和测试反馈，不使用 gold 修改文件或测试定位。其旧通用 settings 中的多轮预算字段不表示进行了多轮 Agent 执行。

C 每题上限为 **300 个动作轮、累计 5,000,000 tokens、900 秒 Agent 循环**。1,101 次模型调用包括 897 次动作请求和 204 次摘要请求；摘要也计入 token 预算。这里的调用数是逻辑 complete 调用；冻结 Provider 的 SDK 配置为 max_retries=2，可能发生传输重试，未单独计数。本报告“不补考”指不重新生成整题候选、不挑优，不声称 HTTP 请求从不重试。C 的限制是上限，不表示实际每题花满。

两组使用同模型和同题，但工具反馈、上下文和计算量不同；20/30 相对 9/30 是系统级对照，不能解释成等算力消融结果。有限子集不能推出完整 300 题成绩。

C30 的前三题复用原 C3 的同一候选和同一官方报告，包含未通过的第二题；146 个生成文件逐一验证字节一致。后 27 题各生成一次，没有模型补考或挑优。新增部分实际消耗 984 次模型调用、11,119,644 tokens；表中的完整 30 题资源包含此前三题，不能当作这次扩展新增的费用。

C30 停止原因为 19 次 finish、11 次 compaction_failed；后者包含 5 个非空补丁，仍正常验收。6 个空补丁都在压缩失败后停止。compaction_failed 不代表必然耗尽 300 轮或 5M tokens。

逐题结果如下；“失败”表示已有官方报告但未解决，“拒绝”表示补丁无法应用，“截断”表示无效模型输出，“空”表示空补丁。更完整的停止原因、调用数、token、分阶段时间和哈希见 [first30.csv](../results/first30.csv) / [first30.json](../results/first30.json)。

| 题目 | 裸模型 | C | C 停止原因 | C tokens | C 循环秒数 |
| --- | --- | --- | --- | ---: | ---: |
| astropy__astropy-12907 | 截断 | 通过 | finish | 170,355 | 65.484 |
| astropy__astropy-14182 | 拒绝 | 失败 | compaction_failed | 770,612 | 245.233 |
| astropy__astropy-14365 | 拒绝 | 通过 | finish | 432,201 | 161.528 |
| astropy__astropy-14995 | 失败 | 通过 | compaction_failed | 181,933 | 78.617 |
| astropy__astropy-6938 | 通过 | 通过 | finish | 132,241 | 67.175 |
| astropy__astropy-7746 | 失败 | 通过 | finish | 189,419 | 119.393 |
| django__django-10914 | 通过 | 通过 | finish | 748,007 | 387.558 |
| django__django-10924 | 拒绝 | 空 | compaction_failed | 61,329 | 42.667 |
| django__django-11001 | 通过 | 通过 | finish | 479,436 | 280.934 |
| django__django-11019 | 截断 | 空 | compaction_failed | 761,990 | 433.402 |
| django__django-11039 | 通过 | 通过 | finish | 163,013 | 77.028 |
| django__django-11049 | 拒绝 | 通过 | finish | 116,477 | 68.878 |
| django__django-11099 | 拒绝 | 通过 | finish | 29,249 | 27.580 |
| django__django-11133 | 通过 | 通过 | finish | 164,869 | 65.613 |
| django__django-11179 | 通过 | 通过 | finish | 117,657 | 63.198 |
| django__django-11283 | 截断 | 空 | compaction_failed | 298,565 | 239.175 |
| django__django-11422 | 通过 | 通过 | finish | 154,306 | 71.467 |
| django__django-11564 | 截断 | 空 | compaction_failed | 745,641 | 553.338 |
| django__django-11583 | 通过 | 通过 | finish | 333,682 | 222.703 |
| django__django-11620 | 拒绝 | 通过 | finish | 469,142 | 231.180 |
| django__django-11630 | 截断 | 空 | compaction_failed | 551,193 | 407.183 |
| django__django-11742 | 拒绝 | 空 | compaction_failed | 1,380,090 | 889.463 |
| django__django-11797 | 截断 | 通过 | finish | 422,815 | 268.247 |
| django__django-11815 | 拒绝 | 通过 | compaction_failed | 157,317 | 75.284 |
| django__django-11848 | 失败 | 失败 | compaction_failed | 565,294 | 417.564 |
| django__django-11905 | 截断 | 失败 | finish | 688,135 | 352.614 |
| django__django-11910 | 截断 | 失败 | compaction_failed | 1,408,536 | 616.633 |
| django__django-11964 | 失败 | 通过 | finish | 174,420 | 112.295 |
| django__django-11999 | 通过 | 通过 | finish | 227,140 | 185.851 |
| django__django-12113 | 截断 | 通过 | finish | 397,748 | 325.425 |

## 原前三题：A / B / C / D 与 mini-SWE-agent

A/B/C/D 每组都是同三题、同 300 动作轮 / 5M 累计 tokens / 900 秒循环上限，每组每题独立生成一次。改动方向如下：

| 组 | 冻结核心 | 相对于前一组的主要变化 |
| --- | --- | --- |
| A | 05359aeddfb57e601ff67a5b3567c15a53a29cb3 | 原版核心，放宽执行预算；运行包装版本另记为 405db4949667ea9435c8df99b1bddf51b3f5ccfb |
| B | 6b050fe6a88fc234808007aa1b0c02e513b6dd4c | 不先清理旧预览；摘要校验成功再原子替换；增加重复操作证据提醒 |
| C | 098e27d19042e28f0592dd27dc76ab7859bb19ff | 在 B 上只调整收尾提示；完成相关验证及 diff 检查后收尾 |
| D | 2f77d31c8107f2b440d0ac64a9638dfbb0702234 | 有界活跃事实、分页历史索引及证据存储调整；保留为独立实验 |

mini 使用官方源码 04d809ceab9df28f9adaed044884180159172930（2.4.6）的 **SWE-bench 配置**：250 步 / 每题 $3 估算成本上限，外层另有含环境准备的 900 秒截止时间，没有自设累计 token 上限。这不是所有 mini 入口统一的默认值；具体配置见 [固定版本 swebench.yaml](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/config/benchmarks/swebench.yaml#L112-L113)。只验收原生提交的补丁，不用工作区诊断 diff 替代提交。三题都自然 Submitted，没有触及该预算停止；估算成本不是账单。

| 组 | 12907 | 14182 | 14365 | 解决 | 动作 + 摘要 = 模型调用 | 总 tokens | 循环秒数和 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| A | 通过 | 失败 | 通过 | 2/3 | 181 + 15 = 196 | 2,484,140 | 671.687 |
| B | 通过 | 失败 | 失败 | 1/3 | 201 + 67 = 268 | 3,567,426 | 1138.589 |
| C | 通过 | 失败 | 通过 | 2/3 | 94 + 23 = 117 | 1,373,168 | 472.245 |
| D | 通过 | 失败 | 空 | 1/3 | 45 + 6 = 51 | 498,312 | 192.683 |
| mini | 通过 | 通过 | 失败 | 2/3 | 108 + 0 = 108 | 4,423,675 | 591.901 |

同前三题裸模型为 0/3（来自裸模型 30 题原记录，不是另跑一轮）。A/B/C/D 实际资源消耗不同，mini 的上限和工具协议也不同；不能凭这三题声称某组显著优于另一组。开发期间反复查看这些任务，属于开发评测，不是未接触过的保留测试集。C3 已计入 C30，两者不独立。

D 的第三题在摘要 schema 校验时收到 4 个 next_steps，超过最多 3 个的约定，因而空补丁停止；这次结果没有补考。A 的第二题则是候选压缩后仍超过上下文容量，不能把不同压缩失败归为同一原因。

每个组每题的动作/摘要调用、停止原因、时间、token 和补丁/报告哈希保存在 [first3.csv](../results/first3.csv) / [first3.json](../results/first3.json)。

## 时间、行为指标与可追溯性

active_seconds 对裸模型指单次模型请求耗时，对 Harness/mini 指 Agent 循环耗时，字段 active_seconds_scope 标明含义。generation_total_seconds 是各包装器记录的单题生成总耗时；环境准备和调度等会使它大于循环时间。grading_seconds 是逐题验收耗时。汇总均为**单题耗时之和**，并发运行时不能解释为整批墙钟或速度排名。

完整 C30 循环耗时和 7,152.710 秒，生成总耗时和 15,051.719 秒，验收耗时和 769.761 秒；包含复用三题的历史时间。本次新增 27 题循环耗时和 6,680.465 秒、验收耗时和 603.392 秒。运行中根据用户资源安排从 2 个生成 worker 增至最多 6 个，环境构造最多 1 个，官方验收始终 1 个；准备条件并非各实验严格相同。

tokens 是记录中的累计输入加输出，缓存 token 是输入的子集，不能再相加。重复输入在后续调用中会重新计数；不能把累计 tokens 当作独立代码量或美元账单。行为字段只统计可可靠识别的简单读取、完全相同的请求和专用补丁到 finish 的轮数；重复读取不自动等于无效操作，shell 内编辑未强行推断。

公开导出时再次检查了预测补丁、导出补丁、官方 patch.diff 的 SHA-256，以及官方报告的真实 resolved 值；补丁拒绝必须同时有相同补丁哈希和框架终态异常。C 的冻结源码和前三题复用文件也重新核对。检查计数与原始汇总文件哈希见 [provenance.json](../results/provenance.json)。

[results/README.md](../results/README.md) 定义字段和文件用途，[SHA256SUMS](../results/SHA256SUMS) 校验公开包文件。公开内容限于配置、题目 ID、计数、时间和原始文件哈希；不含宿主绝对路径、密钥、完整模型轨迹、补丁正文或验收测试。**由于原始日志没有分发，哈希提供来源绑定和完整性检查，不能代替第三方重跑验证。**

D 的实验结果照实保留；发布以 C 核心为基础不意味着 D 的问题已修复，也不把未重新实测的发布包装记作新成绩。
