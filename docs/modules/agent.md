# 核心循环 agent.py

## 意图与职责
这是 from scratch 的核心控制逻辑，其他组件只提供模型、上下文、工具和环境能力。
Agent 不创建或销毁沙箱，不导出补丁，不负责官方评分。finish 只表示模型声明完成。

## 方法与结果
Agent(model, environment, settings, trace).run(task, instructions="") -> dict。
每个 Agent 实例只运行一次，重复调用抛 RuntimeError。
结果包含 outcome、summary、stop_reason、steps、usage、elapsed_seconds、compactions。
outcome 为 completed、budget_exhausted、failed 或 cancelled；steps 只计主模型请求，
压缩调用另计 compactions，但其用量及耗时计入同一预算。

## 每轮顺序
1. 检查时间、总 token 和步数。
2. 必要时压缩老的完整回合，记录压缩前后估计量。
3. 调模型，记录请求/原始响应，累积实际报告用量。
4. 验证工具 envelope，逐个执行，并按 ID 写回观察。
5. 完整回合入上下文；finish 单独且参数合格才能结束。

## 工具协议与错误
- 同回合调用 ID 必须非空且唯一；不同回合可重复。
- 结束原因不是 stop/tool_calls（包括截断或过滤）的响应不执行任何工具；结构无效结束为 failed。
- 未知工具、非法参数、执行异常作为对应 ID 的错误观察，允许模型修复。
- finish 必须独占整个回合，参数只能有非空字符串 summary；混合回合全部拒绝执行。
- 多工具回合中途预算耗尽，未执行调用收到预算错误观察，不重放已经执行的写操作。
- 纯文本响应附继续使用工具的提醒；它不算任务完成。
- KeyboardInterrupt 生成 cancelled；其他异常只记录异常类型，不将可能含密钥的异常正文写入产物。

## 预算与日志
模型请求 timeout 受剩余总时间限制；工具 timeout 同时受 command_timeout 限制。
主请求和摘要的输出上限受剩余 token 限制，但输入 token 及 SDK 重试仍可能使最终用量超过阈值；
上限是操作边界保护，不是严格费用承诺。达到预算后不会启动新操作。
压缩失败保留历史并停止；不能为“继续跑”静默丢上下文。
Trace 写出每次模型请求、响应、工具观察、压缩和最终状态。压缩从不改写原始轨迹。

## 验证与限制
tests/test_agent.py 覆盖 ID 配对/重复、截断禁止执行、坏参数恢复、finish 约束、
文本回复与预算、批次中途超时、摘要失败和摘要用量。
本模块不进行独立验收，不将 finish 当成测试成功，不实现会话恢复与自动动作重试。
