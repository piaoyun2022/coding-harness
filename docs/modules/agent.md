# 核心循环 V2

## 意图与职责
`Agent` 自己实现模型—工具循环：整理工作上下文，调用模型，校验工具请求，顺序执行，再把完整观察配对写回历史。调用者负责工作区、环境生命周期、最终补丁导出与独立评分；`finish` 只是模型声明，不代表评测成功。

模型调用复用 Provider，命令/补丁执行复用 Environment，参数校验复用 tools，证据保存复用 EvidenceStore。没有现成 agent runner、MCP、长期记忆、撤销或会话恢复。

## 外部方法
`Agent(model, environment, settings, trace).run(task, instructions="") -> dict` 仍是唯一运行入口。每个 Agent 仅运行一次；二次调用抛 RuntimeError。返回 outcome、summary、stop_reason、steps、usage、elapsed_seconds、compactions。`completed` 表示合法 finish；`budget_exhausted` 表示步数/时间/token/上下文/证据边界停止；`failed` 表示协议或运行错误；KeyboardInterrupt 返回 cancelled。

run 在 Trace 目录下建立本次专用 evidence 子目录，使用 Trace.clean 脱敏，绝不读取模型密钥到沙箱。完整系统提示由固定规则、环境说明和仓库根规则拼成后交给 Anchor，不被摘要重写。

## 一轮操作
1. 检查剩余时间、累计 token 与主请求步数。超过输入预算时先清理旧预览，仍不够再做一次结构化摘要。
2. `_call` 将消息和工具 schema 都计入输入估算，预留本次最大输出与 margin，再调用模型。摘要不传工具，输出上限由 checkpoint_output_tokens 控制，Provider 使用 summary 推理设置。所有调用和用量均计入总预算。
3. 不完整/过滤响应不执行任何工具。校验 assistant、调用 envelope、同轮重复 ID；跨轮重复 ID 合法。保留原始 reasoning_content。
4. 顺序执行工具。每个动作前重新检查预算，已超预算的后续调用收到配对的“未执行”观察。坏 JSON、未知工具、参数错误作为可恢复观察；不会自动重试写操作。
5. `execute/apply_patch` 的完整有界结果及工具名、原始参数 JSON、call ID 先存为脱敏证据，再生成短预览与事实/ID。`read_evidence` 的页结果直接回传，不再存自身结果，避免无限递归。
6. 同批全部结果都按调用顺序写回，才进入下一轮。finish 必须是唯一工具且参数仅含非空字符串 summary；与其他工具混用时全批不执行。纯文本回复加一次提醒并仍受轮数限制。

## 工具证据与失败处理
原始有界工具结果先写入 `tool_result` 轨迹，再存 EvidenceStore；预览按 `max_output_chars` 截短，不改变真正的 exit_code、timed_out、truncated。模型用证据 ID 和字符偏移分页读取参数及结果。预览截短与执行捕获截断是不同字段；捕获截断丢失的内容不可恢复。

`_store_result` 仅从实际结果提取程序 ToolFact。Checkpoint 的模型解释与程序观察分开存放，模型不能通过摘要覆盖实际失败状态。模型获得的预览和回读内容都经过已配置密钥字符串脱敏；不声称能自动识别所有未知秘密。

证据总额不足、写入失败或单项被截断时，当前动作可能已经生效：保留完整有界原始日志，停止后续动作，返回 evidence_budget，并明确禁止自动重放。单项存储产生的部分诊断文件可以保留，但不作为完整证据发送给模型或登记为事实。这避免为了“重新获取输出”重复执行刚才的写操作。

## 上下文与预算
上下文结构和原子替换条件见 `context.md`。模型摘要只尝试一次，拒绝空、截断、非法 JSON、额外事实字段、伪造/不可用引用、无显著缩小或仍超预算的候选。失败保留历史并停止，不反复付费尝试同样压缩。无法保留最新完整回合时同样停止。

主调用使用配置软输入阈值；摘要可使用预留输出后剩余的模型硬窗口，处理略超软阈值的旧历史。摘要同样计入时间和累计 token，传输错误只记异常类型。SDK 重试与在途消耗意味着累计预算不是账单硬上限。

事件包含 agent_start、model_request/response、tool_result、evidence_stored/error、context_cleanup、compaction、agent_end。压缩不覆盖轨迹。日志写入失败不触发工具重放；环境清理和最终产物由外层 finally 负责。

## 验证与限制
离线测试验证顺序/配对、重复 ID、非法响应不执行、坏参数继续、混合 finish 拒绝、时间/token中断、完整证据先存后预览及回读、已知秘密脱敏、证据耗尽后补丁不重放、schema/输出预留计入预算、摘要失败回退和原始日志保留。

本轮不使用真实 API 或 SWE-bench 检验此改动。离线通过不代表模型一定给出正确检查点、修复成功或运行成本下降；真实能力需要后续固定配置评测。
