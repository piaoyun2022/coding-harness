# 全量官方评分监督器

`scripts/run_official_batch.py` 是与模型生成隔离的单 worker 评分入口。它轮询固定安全任务清单对应的生成产物，接收已经完成的一次性补丁，再调用固定 `swebench==4.1.0` 的官方验证器。它不加载 API 密钥，不调用模型，不读取 generation 请求来修补答案，不向 agent 返回金标准或评分测试。

## 输入与调用

在 Linux/WSL 的独立评测 Python 下运行；官方 grader JSON 可以含 gold，仅由本评分进程和官方验证器读取。`--tasks` 与 grader 的 instance_id、repo、base_commit、version 必须完全一致；`--expected-count` 默认 300，分母不随失败、空补丁或缺失报告缩小。

```bash
/opt/coding-harness-eval/bin/python scripts/run_official_batch.py \
  --tasks artifacts/swe-bench-lite/tasks.jsonl \
  --generation artifacts/baseline-full300 \
  --grading-data artifacts/grader-only/swe-bench-lite-pinned.json \
  --output artifacts/official-baseline-full300
```

实际长跑使用测试通过后的脚本稳定副本，progress 中记录该副本 SHA256、Python 路径/版本、数据和生成 plan SHA256、评分器版本与超时。每题创建唯一 run_id、独立 attempt 目录，记录 predictions、invocation、官方日志和 outcome。官方工具在 attempt 目录运行，避免旧 report 被自动复用。

## 生命周期与资源边界

每次调用先快照本题精确镜像 tag 是否存在及所有现存 image ID。磁盘检查通过后，只有不存在的本题镜像由监督器显式 `docker pull`；记录远程 manifest digest/压缩体积及实际 image ID/digest。Docker 磁盘余量至少为 max(4 GiB, 3×压缩层大小+2 GiB)，产物盘至少 2 GiB。不足时记录 infra_disk_headroom，不删除其他数据腾空间。

官方调用强制 `--max_workers 1 --cache_level instance --clean False --namespace swebench`。每题之后仅尝试删除该题本次新拉的精确 tag，force=false、noprune=true；先前存在的 tag 或 image ID 均保留。不会全局 prune，也不会删除其他任务镜像。其余持久缓存由用户管理。

官方测试超时默认 1800 秒；单次子进程监督超时默认 3600 秒，镜像拉取最多 1200 秒。超时或中断只向自己创建的新进程组发 TERM/KILL；容器补充清理要求本次唯一名称和 Config.Image 同时匹配，先前同名容器会阻止启动。凭证环境变量不传给子进程。没有整个 300 题批次或等待生成的一小时截止，允许生成与评分长期并行。

生成失败/invalid_response/空补丁无需镜像；API 异常、源码准备异常分别标记。生产者 result JSON 写入尚未完成时继续等，生产者 batch-result 已存在却缺结果时记 infra_generation_missing。progress.json 每题原子更新，等待时也更新；run.json 保存最终或中断状态。最终未处理题仍计入 planned/pending。

## 评分与可审计性

有效生成必须满足 prediction.model_patch UTF-8 SHA256 与 changes.patch 的原始字节 SHA256 完全一致。官方结果只在其独立 attempt 的 patch.diff 也与该哈希一致时接纳。官方 report.json 的 resolved 必须是真布尔值；逐题 FAIL_TO_PASS 与 PASS_TO_PASS 明细单列。

4.1.0 补丁应用失败可能不产生 report。只有补丁哈希绑定通过，且 run_instance.log 同时存在带时间戳 logger 前缀的框架失败记录、最后匹配该题的 EvaluationError traceback 时，才记 patch_rejected。仅出现类似关键词、测试输出伪装的普通行或缺失日志都不足以判失败；不伪造官方 report。其他缺报告/超时/拉取/磁盘问题为未知基础设施状态，provisional=true。

主分母固定为全部计划题。全部题达到 graded、generation_invalid、empty_patch 或已验证 patch_rejected 才能输出完整 official_resolved_rate。存在 pending、API/setup 或未知基础设施状态时该字段为 null，只给 observed_resolved_lower_bound 与覆盖率。辅助指标为有有效明细题中逐题通过比例的平均，附 coverage，不能冒充官方分数。

`--resume` 校验任务、生成 plan、grader 数据、脚本和解释器 fingerprint，复用已完成账本。`--resume --retry-infra` 只重新运行未知评分/缺产物检查，生成已确认失败和 API 错误不重新调用模型；每次评分仍有新 run_id，旧 attempt 保留。模型补考永远不属于此脚本功能。中断正在执行的题会保留 attempt/outcome，主账本继续标为待处理，可在恢复时重评。

## 验证与限制

`tests/test_official_batch.py` 用临时目录覆盖：任务/金标准身份不符拒绝；生成状态分类；三方补丁绑定和旧报告拒绝；严格框架失败证据；300 分母与暂定分数；镜像/容器精确 ownership；超时仅终止自身进程组、环境密钥剥离；等待并发生成、缺失结果、只重评未知题。测试不请求模型、不访问 Docker 或网络。

本监督器依赖官方镜像和官方测试质量，不把基础设施故障归责于模型。完整保留 stdout/test_output 可能消耗磁盘，运行前与每题都检查余量。官方参考：[4.1.0 run_evaluation](https://github.com/SWE-bench/SWE-bench/blob/v4.1.0/swebench/harness/run_evaluation.py) 与 [docker_build](https://github.com/SWE-bench/SWE-bench/blob/v4.1.0/swebench/harness/docker_build.py)。

本次冻结前离线验证（2026-10-14）：20 passed，脚本和测试 ruff 通过。运行副本、源码 SHA 与解释器约定记录于 artifacts/frozen-official-v1/receipt.json；正式运行状态以 progress.json/run.json 为准。

## 固定前三十题与已验证报告复用

用户将范围缩为原安全清单顺序前30时，新建独立 tasks/manifest 与 grader-only 子集，不能用随机抽样替换，旧300产物封存、不计入30分母。通过 `--expected-count 30 --reuse-from <旧评分输出>` 可复用同题已有的明确评分；逐题再次检查当前 prediction/changes.patch、旧 prediction、旧官方 patch.diff 的 SHA，且旧 grader 的完整该题数据与当前一致、版本同为4.1.0。复制原 attempt 证据到 reused/，保留源路径、源 fingerprint、progress SHA 与 reuse-audit；旧未知结果或校验不符不复用，正常重新评分，不调用模型。复用只节省相同补丁的重复验收，不把旧模型状态套给新生成。

Agent 批次可使用同样的 runs/<id> 布局：result.mode=agent，status=generated 仅表示非空补丁已导出，原 outcome/stop_reason 在汇总行保留；completed、预算停止等都不能冒充测试通过。empty_patch 单列已知生成失败；有非空补丁的预算停止仍交官方验收。中断最终 progress 会清除 active_instance，但未完成题继续保留在 pending。

范围缩为固定前30后的追加验证：报告复用/数据一致性/Agent预算停止覆盖在内共24项离线测试通过。baseline-first30 从已生成的 full300 首30文件逐字节复制，source-manifest 保存每个源文件 SHA，不重发模型请求。
