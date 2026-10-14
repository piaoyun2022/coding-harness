# Worker：单次命令执行

worker 可作为独立 Python 文件上传沙箱；执行命令分支只依赖标准库。它不负责模型调用、证据存储或运行循环。

`run_command(workspace, command, timeout, output_limit=12000, *, activate_testbed=False, capture_limit=1048576) -> dict` 每次启动一个新 shell（Windows PowerShell / Linux Bash），cwd 固定为工作区，返回 stdout、stderr、exit_code、timed_out、truncated。没有跨调用 shell 状态，安装包/文件等磁盘状态仍保留。

- `capture_limit` 为每个输出流分别允许保留的原始字节数，默认 1 MiB；必须为正整数（不接受 bool）。stdout/stderr 返回各自完整的有界前缀，用 UTF-8 解码并替换非法序列；字节边界可能切断最后一个多字节字符。未超限时不删中段、不插入省略标记。
- `output_limit` 保留旧调用兼容性，仍要求为正数，但不再裁剪执行结果。Agent 收到完整有界结果后先写 EvidenceStore，再根据预览预算裁剪模型观察。上层若需要短输出，必须在证据保存之后生成预览。
- 两条线程并行 drain stdout/stderr；每流最多缓存 capture_limit 字节，超过后继续读取并丢弃，`truncated=true` 明确表示未完整捕获。单次读取块为 64 KiB，不把无限输出写入临时文件；内存量为 O(capture_limit)，与命令累计输出量无关。限额只覆盖捕获器，不限制被执行程序自身的内存或主动写磁盘。
- 超时（包含等待输出管道关闭）和 KeyboardInterrupt 终止子进程树（POSIX process group / Windows taskkill /T），保留截至终止时捕获的输出。杀进程后每条读线程最多额外等待 1 秒；异常读取或仍被逃逸子进程持有的管道标记 truncated，控制器不无限等待。可信本地后端不是进程隔离边界，脱离原进程树的后台进程不承诺可全部清理，沙箱容器生命周期仍必要。
- 白名单传递 PATH、系统目录、临时目录、语言设置，不把控制端 API key 或云端凭证交给命令；不使用 shell profile。
- 不强制覆盖 PATH；预构建 SWE-bench 镜像每次调用尝试激活其既有 testbed conda 环境，工具自身独立 Python 不替换项目解释器。
- CLI 从标准输入（参数 -）或参数接受一个 base64 JSON 请求，分派 execute/apply_patch/collect_patch，stdout 仅返回 JSON；可恢复参数/文件错误返回 ok=false，不输出 traceback。补丁分支才加载 SDK 补丁实现；不运行任何 agent runner。
- 本地调用不能隔离任意进程的文件与网络访问，仅用于显式允许的可信测试；权限边界由 Docker/Modal 沙箱提供。

验证：tests/test_environment.py 检查真实命令、环境凭证剥离、中部证据保留、超过限额持续 drain、stderr、超时后输出与子进程终止，以及独立 worker RPC；Docker/Modal 参数和返回透传用 fake 测试，真实沙箱验证单独记录。
