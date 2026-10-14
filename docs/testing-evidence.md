# 证据回查验证

本组为离线安全和方法契约测试，不调用模型、不运行 benchmark，不证明模型已经会正确检索证据。

- `tests/test_evidence.py` 验证脱敏在哈希和落盘之前发生，Unicode 按字符分页可完整拼接，单项按 UTF-8 字符边界截断并暴露标记，总额不足时没有新增文件或额度消耗。
- 随机未知 ID、路径字符串、另一实例旧 ID 均不可读取；非法字符范围、布尔值和超长请求被拒绝。EOF 与空文本有明确终止语义。
- 返回 metadata 被调用方篡改不会影响原记录；同内容仍产生独立 ID；模拟 UUID 碰撞必须拒绝而不覆盖旧证据、不消耗容量。外部修改相同长度、扩张、删除文件都被识别，symlink 拒绝分支以无平台权限依赖的模拟覆盖。
- 脱敏函数异常不会写入或把秘密/路径写进可显示的错误。此处检查错误字符串，控制层仍不应把原始异常链暴露给模型。
- `tests/test_tools.py` 验证 read_evidence 严格参数、缺少仓库和未知 ID 的可恢复错误、分页读不执行 Environment、提供 evidence 参数后 execute 输出不变且不会自动入库。已有执行超时、坏参数和写入不重试回归继续保留。

验证命令：`.venv/Scripts/python -m pytest tests/test_evidence.py tests/test_tools.py`，以及对应源码/测试的 ruff 检查。具体运行结果由集成记录报告；这些测试不覆盖 Agent 的 checkpoint 生成或模型行为。

本次单模块验证（2026-10-14）：证据仓库与工具测试共 47 passed；未产生模型请求或 benchmark 运行。
