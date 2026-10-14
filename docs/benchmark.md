# 可重复的代码能力评测

主评测选 SWE-bench Lite 的完整 test split（300 题）。评分仍是离散的，每解对一题提高约 0.333 个百分点；辅助修复比例提供更细的诊断信号，但不意味着每个改动都会涨分或结果没有随机波动。

## 固定口径

每个实验固定：题目 revision 和 SHA256、计划题目集合、模型配置、输入检索版本、单题预算、运行代码版本、官方评分器版本。相同模型下比较单次生成基线和多轮 agent，而不是把二者说成与模型无关的纯 harness 能力。官方验证器运行在与 agent 分离的新容器中。

首版裸 DeepSeek：给问题与固定词法检索源码，一次请求生成 unified diff，不调用工具、不看测试反馈、不利用标准补丁决定看哪些文件。它与 agent 的差异就是模型能否主动探索、修改和根据运行结果迭代。

## 已实现的适配边界

1. `prepare_dataset` 下载并固定 300 题安全投影，支持明确标记的开发子集。
2. `generate_baseline` 接收一题及含 base commit 的 Git 仓库，生成并存档单次补丁。不会自动克隆仓库，也不会在宿主机应用补丁。
3. 批量调用方按题建立独立目录并以默认并发 1 调用；产物账本可以恢复**评测批次**，这不要求 agent 支持会话恢复。不要覆盖已有题目录或将不同模型配置拼进同一批。
4. `write_predictions` 聚合完整计划集合；失败和未完成题仍保留。
5. `prepare_grading_data` 另存同 revision 的官方 grader JSON；只有评分程序能读它。
6. 在独立 Linux 环境执行固定 `swebench==4.1.0` 的官方验证命令，读取其每题报告汇总。

完整方法契约见 [benchmark 模块](modules/benchmark.md)。CLI 命令由入口层提供，模块不重复实现另一套 agent runner。

## Windows 上的评分环境

推荐 WSL Linux Python + Docker Desktop 的 WSL integration。数据准备与单次生成可以在 Windows 完成。官方 v4.1.0 脚本写入 shell 文件时没有固定 LF，原生 Windows 的换行转换可能污染容器里的脚本，因此预检要求评分进程在 Linux 内运行。

在 Linux 的独立 virtualenv 安装 `swebench==4.1.0`，确认 `docker info` 能连接 Linux engine。模型 API 密钥不需要交给官方验证器。以固定的 grader JSON、预测 JSONL 和全新的 run ID 调用：

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name /path/to/grader-only.json \
  --predictions_path /path/to/predictions.jsonl \
  --max_workers 1 \
  --run_id unique-baseline-run \
  --timeout 1800
```

WSL 可从 Windows 挂载盘只读取得输入文件；建议在 Linux 文件系统下运行评分，日志和 Docker 数据由对应环境管理。全量镜像下载与测试比 agent 核心实现重，需要先用一题验证环境再启动全量。官方失败日志可能没有逐题报告，必须排查而不是计为模型已失败。

## 结果如何看

- 主指标：官方完整解决题数 / 计划题数。
- 辅助指标：有有效测试明细的题中，逐题 FAIL_TO_PASS 通过比例的平均值；同时给 PASS_TO_PASS 保留比例及覆盖题数。
- 过程指标：模型请求次数、tokens、耗时、生成状态。
- 运行质量：已尝试、已评分、未完成、缺失报告、明确基础设施故障、API 错误分别计数。
- 不完整批次标记 provisional；没有正式验证报告不能宣布得分。重复运行可衡量模型波动。

不要按辅助分数将测试细节喂给下一轮 agent。开发时长期盯同一 test split 会带来适配偏差；正式汇报需要注明这一点，并保留额外未用于调参的任务集。

## 上游依据与数据版本

- [官方数据集](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite)：固定 revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`，test parquet。运行 manifest 记录原始与投影校验和。
- [官方 v4.1.0 验证入口](https://github.com/SWE-bench/SWE-bench/blob/v4.1.0/swebench/harness/run_evaluation.py)：接受本地数据 JSON 和模型预测，使用 Docker，生成逐题 report.json。
- [官方 v4.1.0 评分实现](https://github.com/SWE-bench/SWE-bench/blob/v4.1.0/swebench/harness/grading.py)：resolved 与测试组状态来源。
- [官方评测说明](https://www.swebench.com/SWE-bench/guides/evaluation/)。

当前实现的离线验证不能替代真实模型或真实容器的执行记录。各阶段完成状态由实际产物与验证记录说明。



## 本次已验证的状态

已真实下载完整 300 题安全投影（12 个仓库），保存于忽略目录 artifacts/swe-bench-lite。原 parquet SHA256 为 7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4；load_tasks 已验证落盘投影的校验和。仅下载成功，不代表生成或评分完成。

默认官方参数 cache_level=env、clean=false，会删除本批新取得的 instance 镜像而保留预先存在的镜像；默认并发 1 减少峰值占用。不要同时启动另一批 SWE-bench，官方清理按镜像名称和运行前快照识别新镜像。每批仍需确认磁盘空间；不能删除用户其他镜像或文件腾空间。


## 防止复用错误版本的成绩

比较新版本时，score 必须同时提供该批 run_dir 和对应官方报告目录。适配器读取官方每题实际应用的 patch.diff 并核对当前预测补丁 SHA256；旧报告对应不同补丁、缺失评分输入或当前产物互相矛盾时，结果标记 unverified/provisional，不算成功。每批仍须使用新的官方 run ID。

只给报告目录、不提供 run_dir 的模式仅汇总这些历史报告，输出 score_scope=provided_reports_only 和 comparison_to_current_run=false。它不能证明当前代码版本已经通过同样的测试。空/无效模型输出单列生成失败，非空预测尚未获得报告单列未评分，两者都不自动归为基础设施故障。
