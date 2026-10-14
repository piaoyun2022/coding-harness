# Worker：单次命令执行

worker 可作为独立 Python 文件上传沙箱；执行命令分支只依赖标准库。它不负责模型调用或运行循环。

`run_command(workspace, command, timeout, output_limit=12000, activate_testbed=False) -> dict` 每次启动一个新 shell（Windows PowerShell / Linux Bash），cwd 固定为工作区，返回 stdout、stderr、exit_code、timed_out、truncated。没有跨调用 shell 状态，安装包/文件等磁盘状态仍保留。

- 白名单传递 PATH、系统目录、临时目录、语言设置，不把控制端 API key 或云端凭证交给命令；不使用 shell profile。
- stdout/stderr 写临时文件，返回长度分别截断，保留开头和结尾。磁盘上的原始临时输出不设配额，沙箱生命周期与资源限制仍必要。
- 超时和 KeyboardInterrupt 终止子进程树（POSIX process group / Windows taskkill /T），避免超时任务继续修改文件。
- 不强制覆盖 PATH；预构建 SWE-bench 镜像每次调用尝试激活其既有 testbed conda 环境，工具自身独立 Python 不替换项目解释器。
- CLI 从标准输入（参数 -）或参数接受一个 base64 JSON 请求，分派 execute/apply_patch/collect_patch，stdout 仅返回 JSON；可恢复参数/文件错误返回 ok=false，不输出 traceback。补丁分支才加载 SDK 补丁实现；不运行任何 agent runner。
- 本地调用不能隔离任意进程的文件与网络访问，仅用于显式允许的可信测试；真正权限边界是 Modal 沙箱。

验证：tests/test_environment.py 检查真实命令、环境凭证剥离、输出截断和超时；云端行为用 SDK fake 测试，真实沙箱验证单独记录。
