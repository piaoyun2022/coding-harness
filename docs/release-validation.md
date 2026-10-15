# 发布入口验收记录

查证日期：2026-10-14（America/New_York）。本页区分离线测试、真实环境预检和已有补丁重放；**没有重新运行收费的 30/300 题模型评测**。历史 C 版 20/30 成绩仍来自 [结果摘要](../results/README.md)。

## 已完成的检查

| 检查 | 结果 | 能说明什么 |
|---|---|---|
| Linux 全量离线测试 | 313 passed，15.43 秒 | 核心与批处理回归，包括新增入口和 shell 路径测试 |
| 新入口定向回归 | 25 passed，另 6 项 Linux shell 测试通过 | 生成/评分衔接、固定分母、每题一次、清理异常、代码/镜像身份、错误配置提示 |
| 静态与文档同步检查 | ruff、代码文档映射通过 | 新脚本有对应职责、约定及限制说明 |
| 实际 PowerShell → WSL bootstrap | exit 0，ready=true，model_called=false | 独立环境安装、明确 .env 读取、源码/import 与 Linux Docker 前置检查成功 |
| 新 Pipeline 的真实 Docker 官方验收 | 既有补丁 1/1 通过，unknown=0 | 公开编排代码确实能接通官方评分及精确资源清理；不代表新模型生成 |

预检只确认指定配置中的密钥存在，不通过模型请求验证密钥或余额。离线替身测试也不能替代真实模型能力分数。

## 一次真实的已有补丁重放

测试对象为已经在 C 版验收通过的 `astropy__astropy-12907`。重放先重新核对原 prediction、changes.patch、原官方 patch.diff/report 的 SHA，确保拿到的就是同一份已验收候选。

驱动只覆盖 `Pipeline.generate`：复制现有补丁并明确标记 `replayed_existing_candidate`，随后调用当前公开入口的 `Pipeline.run_mode("agent")`。原模型生成入口和模型构造函数都设为禁止调用；不读取 .env 或真实密钥。当前评分所需的官方数据仅从原 grader-only 清单筛选这一题，Agent 不执行也不接触测试答案。重放记录 input/output/cached/total tokens 全为 0，模型守卫触发 0 次。

| 项目 | 本次实测 |
|---|---|
| 新模型/API 请求 | **0** |
| 当前公开源码提交 | `a02ab335da2eaaf7a20a63e5701ea94d2284a950` |
| 冻结核心 | 与 C 提交 `098e27d19042e28f0592dd27dc76ab7859bb19ff` 一致 |
| 官方评分器 | SWE-bench 4.1.0 |
| 官方结果 | resolved=true，overall_complete=true，unknown=0 |
| FAIL_TO_PASS | 2 通过 / 0 失败 |
| PASS_TO_PASS | 13 通过 / 0 失败 |
| 官方评分耗时 | 67.101 秒 |
| 官方 attempt | `official-a1c2f65a209d48e5a8470576ee07e72a` |

补丁 SHA256（原候选、重放 prediction/changes.patch、官方 patch.diff 一致）：

```text
3b70d8655268f8f4ca4a3cac06deb2713c4c88c2802242dd3ef6398129d6e56e
```

官方 report SHA256：

```text
fe3f3bddc9631279d3babf7270913070d91e9207c3468bd947823aee3e7b1b86
```

预期源镜像、评分前 source tag 和官方实际 evaluated_image_id 完全一致，也与原 C 验收的镜像 ID 一致：

```text
sha256:e082963099ed7d5a5f75a0be46e338be36faae33b2cba5289a9ddbd505997f1f
```

官方框架完成时已移除其唯一容器；它看到源 tag 由本次 Pipeline 预先持有，因此将源清理交还 Pipeline。Pipeline 按本轮首次拉取记录与精确 ID 删除该源 tag。随后独立连接 Docker 再查询，确认该评分容器及源 tag 均不存在，Docker 本身仍正常可达。没有全局 prune，没有删除已有镜像或 BuildKit 缓存。

本地保留原始证据于 `artifacts/public-pipeline-replay-smoke/`：

- `replay-receipt.json`、`replay-inputs/`：原候选、原评分绑定和驱动 SHA。
- `source.json`、`preflight.json`：实际代码、包版本和环境检查。
- `official-agent/instances/`：真实官方 invocation、日志、report、outcome。
- `validation.json`、`report.json`：明确标记零模型请求的验收结果。
- `agent/cleanup/`、`cleanup-verification.json`：清理归属和独立后置查询。

验证驱动 SHA256 为 `442b1314e1cdd2880c9e8bd4e049448760f32c89d7a9fb686ae0d1cfc973319b`。本地原始产物不随公开仓库上传；本页保留最小验收结论和绑定哈希，不公开测试答案、私人路径或密钥。

## Windows 首次安装问题与处理

最初在 WSL 的 Windows 挂载目录 `/mnt/c` 内创建环境，pip 安装大量小文件很慢。只读采样显示，5 秒内写入字符数仍增加 52,224、写调用增加 44，进程在运行与 `p9_client_rpc` 等待间切换，说明安装仍推进，瓶颈在跨文件系统的小文件/元数据访问。

因此主动停止了那次尚未完成的预检安装，保留旧环境，不把它计作模型或评分失败。bootstrap 改为：当 WSL checkout 位于 `/mnt/<drive>` 时，把 checkout 专用环境放到 Linux 缓存，路径由 checkout SHA 区分；普通 Linux 仍用 `.harness/eval-venv`，并支持 `HARNESS_EVAL_VENV` 覆盖。没有改 HOME、系统 Python 或冻结核心。新入口的实际 PowerShell → WSL 安装与预检随后成功，前述真实 Docker 重放使用的就是这个新环境。

本次实际版本：Python 3.12.3、openai 2.54.0、openai-agents 0.20.0、SWE-bench 4.1.0、pyarrow 23.0.1。更完整的运行方法、并发与磁盘限制见 [一键评测](evaluation.md)。

此验收确认公开入口能够安装、预检、调度现有候选、执行官方评分并清理自身资源；它没有声称重新生成了历史 20/30，也没有额外测量新一轮模型性能。

