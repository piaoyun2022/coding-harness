# 命令入口 cli.py / __main__.py

## 意图与边界
CLI 只装配已有组件、负责资源生命周期与产物；不再实现第二套 agent 循环。
python -m coding_harness 与 coding-harness 使用同一个 main(argv=None) -> int 入口。
默认读取开发仓库根目录的精确 .env 文件；安装后若无法定位源码仓库，则用当前目录 .env。
--env-file 可覆盖；不向父目录搜索，不枚举环境变量，已有 DEEPSEEK_API_KEY 环境变量优先。

## Agent 运行
run --repo PATH (--task TEXT | --task-file PATH) --output NEW_DIR
默认 backend=docker。可选 --backend modal；可信本地测试必须同时给 --backend local --allow-local。
--settings 接受 JSON 对象字符串或 JSON 文件路径；--model、--reasoning-effort 覆盖其中同名值。
--allow-network 为远程沙箱显式联网开关，默认关闭。

顺序：读取任务 → 新建产物目录 → prepare_workspace 创建副本 → 读取副本根 AGENTS.md →
构造环境与模型 → Agent.run → finally 导出补丁、关闭环境和模型、写 result.json。
不自动执行祖先/子目录 AGENTS 文件发现；当前约定只加载根目录规则。
每次 output 必须不存在，不覆盖已有运行。输入仓库始终不修改。

预制镜像模式：--image REF --workdir /testbed。
镜像已经拥有仓库，不复制本机文件；本机 repo 必须是干净 checkout，取其原始 HEAD 作为镜像基线，
环境验证镜像 HEAD 一致。普通模式使用副本新建的 Git snapshot 基线。
本地后端拒绝 image 参数。

## 失败与产物
产物包含 workspace/、workspace.json、trajectory.jsonl、changes.patch、result.json。
模型运行的 outcome/stop_reason 保存在 agent_outcome/agent_stop_reason；补丁导出或清理失败不能抹去它们。
errors 按 workspace_setup、environment_setup、model_setup、patch_export、
environment_close、model_close、patch_write/result_write 分层记录异常类型。
模型成功但产物/清理失败时，最终 outcome 为 failed，不能报整个运行成功。
中断为 cancelled，仍尝试导出与清理。外部异常正文不回显，避免可能的密钥泄漏。
已知密钥从控制台、Trace 与补丁文本中脱敏；环境不会接收模型密钥。

退出码：0 成功；1 运行、模型或产物失败/预算耗尽；2 明确配置错误；130 用户中断。
Agent completed 仅表示模型调用 finish，不代表官方测试通过。

## API 连通性
probe [--output NEW_DIR] 调一次小输出、关闭推理的请求，保存脱敏 result 与轨迹。
connected=true 表示收到有效模型回复，不是 benchmark 成绩。
默认产物放当前目录 artifacts/probe-随机后缀；不扫描或打印其他凭证。

## Benchmark 简单入口
- dataset --output NEW_DIR [--revision SHA] [--limit N]：
  下载 benchmark.prepare_dataset 的题面安全投影，默认完整测试集。
- baseline --tasks tasks.jsonl --instance-id ID --repo CHECKOUT --output NEW_DIR：
  固定检索 + 单次生成补丁，无工具/测试反馈，不应用补丁；支持通用模型配置。
  关闭客户端并对该次生成的文本产物脱敏。
- grade-command --predictions FILE --grading-data FILE --run-id NAME [--workers N]：
  只输出官方 grader argv 与环境 preflight，不执行评分。
  可带 --manifest FILE，按已固定 manifest 准备评分端专用数据；
  评分数据不可进入 agent 工作区。
- score --tasks FILE --reports OFFICIAL_RUN_DIR [--run-dir GENERATIONS] [--output NEW_JSON]：
  汇总现有官方报告，不生成测试通过声明；可将完整结果写入新 JSON 文件。

## 验证与限制
tests/test_cli.py 使用离线模型和可信本地小仓库，覆盖真实 patch → 运行检查 → finish 闭环、
源文件保持、规则加载、明确 opt-in、设置覆盖、中断清理、失败分层、脱敏、
单次 baseline 和不依赖模型凭证的评测辅助命令。
CLI 不自动批量调度 300 题，不声称已有全量成绩；真实 API 与真实容器另行验证。
