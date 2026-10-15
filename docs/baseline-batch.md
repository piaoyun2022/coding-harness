# 裸模型批量生成入口

> 下文离线验证与全量计划保留实现时的历史范围，不代表全量300题已经完成。当前已核验的裸模型前30题为9/30；设置、失败分类与C对照见 [评测结果](evaluation-results.md)。

`scripts/run_baseline_batch.py` 复用 benchmark.load_tasks / generate_baseline / write_predictions。目标是把固定任务清单逐题生成一次补丁，保留失败，供官方 grader 评分；不运行 agent 循环、不执行仓库代码，也不直接报告解决率。

## 调用

在安装项目的 Python 环境中：

```powershell
python scripts/run_baseline_batch.py --tasks artifacts/swe-bench-lite/tasks.jsonl --output artifacts/baseline-full --env-file .env --settings artifacts/baseline-settings.json --workers 2
```

上述是模板路径，调用前应确认已有任务与设置文件。output 必须全新，workers 默认 2，仅支持 1–4；settings 文件是 Settings 对象的 JSON，所有题使用同一个经过验证的配置，检索上下文固定为 64,000 字符。若任务清单包含完整 300 题，正常结束后 predictions.jsonl 必须包含全部 300 个 ID；测试支持小清单，不能把小清单生成称为全量成绩。

env-file 是唯一密钥来源，使用 dotenv_values(interpolate=False) 精确读取 DEEPSEEK_API_KEY，不搜索父目录、不使用 os.environ 中的替代值、不插值、不修改进程环境。进度和错误只输出题目 ID、状态与异常类型，绝不输出异常字符串或密钥。

## 数据与调用流程

- 保存安全投影后的 tasks.jsonl、settings.json、plan.json。plan 包含任务 SHA256、检索版本、计划数量、worker 数、固定上下文长度与模型配置，便于复跑比较。
- RepositoryCache 为每个公共 GitHub owner/repository 创建一个 bare Git 缓存，没有 checkout；只 fetch 每题精确 base_commit，depth=1、no-tags，并保存不可变 commit ref。按 repo 加线程锁，避免同一 bare 库并发 fetch；不同 repo 可并发。
- 缓存只供读取 Git blobs，绝不运行仓库脚本、hook、构建或测试。缓存 Git 命令不继承全局配置、credential helper 和调用端秘密环境。生成过程通过现有 benchmark 检索器使用题面和源代码，不读取 gold patch 或评分测试字段。
- 每题拥有单独 SDK 客户端，传入内存中的 API key，完成后关闭。一次逻辑生成调用、不提供工具和测试反馈；SDK 自身的传输重试仍遵循既有 Provider 设置。
- 每题目录 runs/instance_id 保存 request、retrieval、response、changes.patch、prediction、result。fetch/初始化失败仍保存 task、空补丁、prediction 和只有异常类型的 result。模型错误由 generate_baseline 记录，不能从分母删掉。
- 最后按计划顺序汇总 predictions.jsonl 与 batch-result.json；结果包含生成状态计数、token 总量和耗时，graded=false、resolved_rate=null。必须另接官方 grader 后才有代码能力分数。

## 方法与限制

`run_batch(tasks, output, settings, *, api_key, workers=2) -> dict` 执行一轮不可恢复的批量生成。为了离线测试，Python 方法允许注入 model_factory 和 origin_for_repo；CLI 不提供自定义仓库源，只使用公开 GitHub HTTPS 地址。

`RepositoryCache.ensure(task) -> Path` 返回含指定 commit 的 bare 库。fetch 最多 300 秒；全批没有额外全局 token/费用限制，总预算由每题 Settings 与计划题数决定。缓存位于本次新 output 内，同一 repo 各题复用，跨批不共享。不承诺在网络失败时自动重试整个任务。

本入口不实现恢复或复杂调度；中断会取消未开始的 future，已开始的任务等待完成，SDK 传输重试可能延长这一等待。保留已产生文件，但不把中断目录冒充完整输出，也不允许覆盖它重新跑。只读 bare 缓存指不 checkout/修改源文件，fetch 本身仍会写缓存的 Git objects/refs。

验证见 tests/test_baseline_batch.py：假模型 + 真实本地 Git fetch，覆盖同 repo 去重、多个不可变 commit、失败空预测、密钥不进入环境或产物、配置留档和全量计划 ID 保留。它们不访问外网、不调用模型 API、不等于真实 benchmark 得分。



已完成离线验证：tests/test_baseline_batch.py 共 6 passed，针对脚本和测试的 ruff 检查通过。该次离线检查没有进行真实网络获取、真实模型调用或完整300题运行；后续已完成子集实测，见本文顶部结果入口。

本次正式全量计划采用统一配置：max_output_tokens=32768、request_timeout=180、max_seconds=300、reasoning_effort=high、workers=2；64,000 字符上下文由脚本固定。配置由显式 settings JSON 传入并存档，必须全题一致，不能仅给失败题更大预算后混算成绩。模板如下：

```json
{
  "model": "deepseek-flash",
  "reasoning_effort": "high",
  "max_output_tokens": 32768,
  "request_timeout": 180,
  "max_seconds": 300
}
```
