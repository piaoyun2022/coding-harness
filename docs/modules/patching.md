# Patching：明确的文件编辑

`apply_patch(workspace: Path, patch: str) -> dict` 接受 Codex 风格 Begin Patch/End Patch 外壳，支持 Add File、Update File、Delete File 和紧跟 Update 的 Move to。成功返回 ok 与 changed_files；失败抛 ValueError 或 I/O 异常，由工具调度转换成可恢复观察。

文件文本 diff 使用已安装 openai-agents 的公开 `agents.apply_diff`，没有复制 SDK parser，也没有使用其 Runner 或 tracing。我们只实现多文件外壳解析、路径约束和文件落盘；SDK 的上下文匹配/空白容忍语义保持原样。

先完成所有操作的解析与计算，才开始写文件。因此第二个文件上下文错误不会导致第一个文件已被修改。拒绝同一补丁中重复来源/目标、路径祖先冲突、覆盖已有 rename 目标、绝对路径、..、.git、符号链接及 Windows reparse 点。限制 256 KiB 补丁、100 个操作、单文件 5 MiB。只支持 UTF-8 文本，二进制编辑不在首版范围。

每个文件用同目录临时文件和 replace 写入，保留原文件权限；跨文件 I/O 异常通过内存备份恢复。这只是失败处理，不提供用户撤销功能，也不保证断电/进程强杀时多文件事务完整；恢复过程中再次出现磁盘错误也可能阻止完整恢复。落盘前再核对内容与路径，不能防止恶意并发进程在检查后换链接；安全边界仍为独立沙箱。

工具产出的最终 Git diff 与输入的 Codex patch 是不同格式：前者用于交付/评测，后者用于逐步编辑。测试覆盖新增、修改、删除、移动、路径拒绝、全量预验证和 I/O 恢复。
