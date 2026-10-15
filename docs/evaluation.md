# 一条命令完成评测

本入口把已经验证的 Agent、裸模型和 SWE-bench 官方评分接起来。它自动准备数据、控制并发、逐题生成及验收、保存补丁和日志、汇总最终分数。无需 Codex 或人工调度；不包含 mini-SWE。默认 **both、原顺序前 30 题、2 路任务**，300 题必须明确传 `--limit 300`。这个入口没有另外发起收费评测；发布时使用离线集成测试和只读环境预检验收。

## 使用

需要 Linux x86_64、Python 3.11+（含 venv）、Git、运行中的 Docker Linux engine，以及 Docker Hub/Hugging Face/GitHub/DeepSeek 网络可达。Windows 使用已安装的 WSL2 和 Docker Desktop 的 WSL integration。脚本不会自动安装或重启系统组件。

```bash
cp .env.example .env
# 用编辑器在 .env 填入 DEEPSEEK_API_KEY=你的密钥
bash scripts/evaluate.sh --env-file .env
```

shell 只为本 checkout 建立 `.harness/eval-venv`，按 `scripts/evaluation-constraints.txt` 安装依赖；默认不用已有全局 Python 包。固定 openai-agents 0.20.0，因为补丁 SDK 及 Docker 工具镜像会读取此版本。每轮 source.json 另记实际依赖版本，Python 路径在进程启动记录中可核对。不会把密钥加入命令行、os.environ、Docker 或结果文件。

```bash
bash scripts/evaluate.sh --env-file .env --preflight
bash scripts/evaluate.sh --env-file .env --mode agent --limit 30 --workers 2
bash scripts/evaluate.sh --env-file .env --mode both --limit 300 --workers 4
# Docker 数据位于另一磁盘，尤其 Docker Desktop 的 backing disk，请显式指定对应挂载点
bash scripts/evaluate.sh --env-file .env --docker-disk /mnt/d
```

Windows PowerShell，在仓库目录执行：

```powershell
Copy-Item .env.example .env
# 编辑 .env 后运行；选项后面的路径用 WSL/Linux 格式或相对仓库路径
powershell -ExecutionPolicy Bypass -File scripts/evaluate.ps1 --env-file .env
# 指定 WSL 发行版：-Distribution Ubuntu
```

PowerShell 使用默认 WSL 发行版，也支持 `-Distribution`（别名 `-distro`）覆盖；不依赖某台电脑的用户名、发行版名称或 /opt 环境。参数以数组转发，不拼接 shell 字符串。WSL 未安装或 Python venv 缺失时给出明确提示。Windows 克隆需让文件保持 LF（Git attributes 中已经配置）。

## 默认实验与资源

默认模型 DeepSeek Flash/high；Agent 每题 300 动作轮、累计 5,000,000 tokens、900 秒，单次输出上限 32768、请求 180 秒；24k 上下文及其他参数来自冻结 C 的 Settings。裸模型使用相同模型和推理档，64,000 字符的固定检索输入，只请求一次，没有工具或测试反馈。因此算力预算不同，报告同时列 usage，不把分差称为同等成本下的收益。

`--settings file.json` 可显式替换 Settings；完整有效配置被保留。默认从当前 HEAD 冻结源码，`--source-commit` 可传完整 SHA。启动和每题执行前核验 src 全部文件与提交内容；同时确认 Python 实际导入路径属于本 checkout，防止旧 editable 安装混入。编排脚本另存哈希。脚本不修改 Agent 提示词、循环、上下文、完成规则。

`--workers 1..8` 控制完整任务 lane：生成→官方验收→本题清理，最多 workers 个任务。both 先运行 baseline，再运行 agent；两种实验不混分。冷环境构造全局最多 1 路，模型循环及不同题评分可以并行。构造锁等待和 Docker setup **不计入** Agent 的 900 秒。默认 Docker 数据盘剩余至少 20 GiB、产物盘 2 GiB，冷构造前 Linux MemAvailable 至少 8 GiB；可分别显式配置 `--docker-disk`、`--min-disk-gib`、`--min-memory-gib`。这是当前余量门槛，不是磁盘/内存预留，增加 workers 前需要足够宿主资源。Docker Desktop 的数据盘与 Linux 根盘可能不同，需正确指定。

首次下载/构建会占较多空间。300 题需要准备充足磁盘及 API 额度，入口不承诺在 20 GiB 总空间内完成。只清理本轮精确镜像标签，**不做全局 prune，也不清 BuildKit 缓存**。缓存由 Docker 自身配置管理；空间不足会停止新增任务，报告保留 pending，不冒充模型失败。

## 输入、输出与评分边界

数据固定为 benchmark 模块中的 SWE-bench Lite revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`，按 instance_id 排序取前 N，与已公布的 first30 一致。数据 SHA 和题目顺序存 manifest。任务输入只保留 Task 允许字段；完整测试与标准补丁只保存在 `data/grader-only` 并交官方评分，绝不挂载进 Agent 容器。

每次必须使用全新输出目录；默认 `artifacts/evaluation-<UTC>-<random>`。不自动恢复、不重复失败题、不挑选最好补丁。冻结 C 的 SDK 仍配置 max_retries=2，传输失败可能重试同一逻辑请求；这里的“不重跑”指不重新生成任务或择优，并非 HTTP 尝试次数等于逻辑模型调用数。目录已有时拒绝启动。任务只提交一次，线程池内不排整批待执行队列。Ctrl+C/SIGTERM 停止提交新任务，让已经开始的 lane 自然完成并保留证据；不会自动重放写操作或重发模型。

- `data/tasks`：安全任务和 pinned manifest。
- `source.json`、`settings.json`、`plan.json`：代码、模型、数据及并发约定。
- `agent/`、`baseline/`：各自 plan、runs/<id>/{task,result,prediction,changes.patch}、轨迹、汇总 predictions。
- `official-agent/`、`official-baseline/`：逐题官方 attempt、绑定报告及 progress/run。
- `report.json`、`report.md`：最终并列分数、覆盖率、未知状态及 tokens。
- 各生成目录 `cleanup/<id>.json`：源镜像的前置存在性、ID 与精确清理结果。

Agent 的 finish 只是停止信号；预算停止但有非空补丁仍验收。官方评分复用 `run_official_batch.Grader` 和其严格三方 patch SHA 绑定，测试超时默认 1800 秒，评分子进程 3600 秒。仅完整官方 report 或已核验的框架 patch rejection 判定成功/失败。空补丁及无效响应计入固定分母。API、setup、未判明异常为 unknown，未启动为 pending；任一存在时正式 rate 为 null，只列观测下界。基础设施/API 故障后不继续消耗模型调用。report.json 显式记录 requested_modes、completed_modes、not_run_modes、overall_complete；即使 baseline 已全部验收，若清理失败或取消导致 agent 没运行，整批也不能成功。退出码 0 表示所有请求模式完成且有完整评分，2 表示暂定/中断/清理未完成，1 表示启动失败。

## 方法职责与隔离

`scripts/evaluate.py` 提供 `default_settings/read_key/frozen_source/preflight/prepare_data` 与 `Pipeline`。read_key 只解析指定 dotenv，禁止搜索及插值；frozen_source 校验源码和实际 import；prepare_data 下载同 revision 并再次校验 grader 数据身份；Pipeline.run_mode 有界调度，one 复用既有生成与评分函数，没有重新实现 Agent。public 报告只保存异常类型，HTTP 异常内容可能含凭证，不能原样输出。

`scripts/evaluation_images.py` 的 SourceImage 在每题首次使用时记录 tag 前置状态、immutable ID、digest；生成和评分都完成后才清理。grade 前重新核对 source tag 的 ID，grade 后再核对官方 image_before 和 evaluated_image_id，任一不一致把该结果列为 unknown，不接受错误镜像上的评分。已有 tag/ID、ID 改变或仍有容器引用的来源都保留。派生 overlay 的 RootFS 也纳入引用检查。private_environment 给 Agent 分配 UUID 私有 FROM alias，从不可变 ID 运行；overlay 只由本题按 ID 精确清理，模型容器无宿主目录/密钥/Docker socket 挂载。保留官方安装 HEAD，并使用 BenchmarkDocker 原有严格验证。官方评分在本 lane 的生成容器关闭后运行，同题不会在该批次重叠；不同调用仍应各自使用全新输出，资源门槛不等价于全局调度器。

## 验证

离线测试应覆盖：默认预算、显式密钥及不污染环境、已存在输出拒绝、数据 allowlist 与 grader 分离、真任务产物接入官方判定、并发每题一次、失败停止派发/固定分母、setup 锁串行、exact-ID 镜像清理和已有镜像保留。运行 `pytest tests/test_evaluate.py tests/test_evaluation_images.py`。这些测试不连接 DeepSeek、不下载镜像；真实依赖安装和 `--preflight` 不等于新一轮真实模型分数。


发布入口离线首轮验证：新入口及既有批处理回归通过（当前具体计数见发布验收记录），包含真实本地 bare Git 的 baseline 管线、真实 Agent 循环接替身模型/执行器、逐题官方 patch/report 绑定；不代表新的真实模型评测。

官方评分器若报告容器/镜像清理异常（包括无法核验 ownership），保留其已经绑定的评分，但立即停止新增任务，整批 overall_complete=false/退出码2；已有 source 被保留不能掩盖官方残留资源。

最新发布入口定向验收：25 passed（包括三种官方清理失败、源镜像变化拒绝、foreign editable 拒绝），此前复用批处理相关 51 项通过。
