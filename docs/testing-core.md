# 核心协议与上下文测试

这些测试使用离线模型或 HTTP 替身，不应当作模型代码能力成绩。

- tests/test_provider.py：通过真正 OpenAI SDK 与 HTTP MockTransport 检查工具/推理字段保真、
  请求参数、缓存用量、summary 不传空工具列表，以及无 choices 时的协议失败。
- tests/test_context.py：两个工具调用构成一个完整回合，压缩不能破坏调用/结果配对；
  原任务、规则、最近回合保持，返回快照不能反向修改内部状态；
  空摘要、截断、超长、工具响应和不完整回合都拒绝。
- tests/test_agent.py：完整循环中的工具 ID 配对、跨回合复用 ID、坏参数恢复、finish 独占、
  截断响应零执行、纯文本提醒、步数/token/时间预算、多调用批次停止、
  压缩用量归入总预算、压缩失败保留上下文及完整日志。

验证命令：
python -m pytest tests/test_provider.py tests/test_context.py tests/test_agent.py
python -m ruff check src/coding_harness/provider.py src/coding_harness/context.py src/coding_harness/agent.py tests/test_provider.py tests/test_context.py tests/test_agent.py

测试不启动云端沙箱、不读取本地密钥、不调用真实模型。
真实 API 连通性、真实沙箱运行与 benchmark 结果由其他运行报告单独记录。
