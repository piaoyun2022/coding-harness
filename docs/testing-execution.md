# 执行层验证

执行层测试不是 benchmark 成绩。此处区分真实本地子进程、真实补丁算法，与仅验证 SDK 调用约定的 fake。

- test_patching：通过安装的 SDK apply_diff 做真实新增、更新、移动、删除与 CRLF 编辑；非法第二个操作、路径冲突、路径越界、秘密 Git 元数据路径、symlink/reparse、超大补丁、模拟 I/O 失败均验证不会留下部分修改。需要主机权限的真实 symlink 测试可跳过，另有无权限要求的 reparse 模拟覆盖。
- test_tools：严格 schema，未知工具，错误参数不执行，超时只能缩小，非零退出观察可见，写失败不自动重试，校验错误不回显秘密输入。
- test_workspace：实际 Git 快照与 patch 回放，新增和删除包含在导出里，原项目不变；忽略规则、已跟踪但后续被 ignore 匹配的文件、常见秘密、缓存、symlink 过滤。
- test_environment：可信临时目录里执行真实 Python 子进程，凭证环境剥离、输出截断、超时终止；Local close 后不能继续操作。Docker/Modal 使用 fake 检查网络/资源参数、无宿主凭证或挂载、stdin 传递大补丁、预制镜像不覆盖 /testbed、HEAD 不匹配清理、客户端超时终止容器、run 未知结果按 ownership label 清理、不误删其他 owner、close 幂等。
- fake 通过不代表 Docker daemon、镜像构建或 Modal 云端已可用；实际连通和云端开销必须单独验证、记录。补丁 SDK 无模型调用、无 Runner，无 trace span，因此这些测试不产生模型 API 请求。

建议命令：python -m pytest tests/test_environment.py tests/test_patching.py tests/test_tools.py tests/test_workspace.py；代码格式与 lint 另执行 ruff。


初轮执行结果：47 passed、2 skipped（Windows 主机不允许创建真实 symlink）；真实 reparse 路径逻辑仍有模拟验证。此记录仅覆盖执行层，不代表整个项目测试或真实容器已完成。


真实 Docker 冒烟验证（2026-10-14）：artifacts/docker-smoke.json 记录 mode=real-docker-trusted-fixture、model_calls=0。包含首次通用镜像构建共 38.91 秒；错误算术函数先测试失败，专用补丁工具修复后测试通过，最终 Git diff 正确，源文件未改变，容器内未发现模型 API key 环境变量，close 后 Docker inspect 确认容器不存在。全部检查通过。这证明通用 Docker 后端真实可用，尚不证明 SWE-bench 预制镜像、完整评测或 Modal 云端可用。

最终执行层回归：50 passed、2 skipped（同上 Windows symlink 权限限制）；针对本次修改的 ruff 检查全部通过。新增跟踪但 ignore 文件的补丁回放，以及 Docker 启动未知结果与 ownership 清理边界均通过。


修复真实模型冒烟发现的空输入问题：Git 仓库根目录与其内部的显式普通目录现在分开处理。回归用例在父 Git 仓库忽略的 artifacts/fixture 下创建源文件，验证文件仍完整进入独立快照，秘密过滤仍生效且父 index 不变；另外验证空目录/仅秘密文件目录在任何输出创建前失败。真实模型首次失败的历史产物保留，不将失败重写成成功。

空输入修复验证：workspace、environment、CLI 三组共 32 passed、1 skipped（同上主机 symlink 权限限制）；本次修改的 ruff 检查通过。


## 预制镜像基线回归

新增测试模拟 HEAD 正确但工作树已有修改、已有未跟踪文件、状态命令失败、截断或超时五种情况。DockerEnvironment 均必须拒绝启动任务，并清理自己创建的容器。fake Docker/Modal 的正常响应区分 HEAD 和 status 请求，避免把只有提交号匹配误当作完整基线验证。该验证同样由两种远端后端共享，不改动未知镜像来“修好”检查。

真实联调追加回归：导出时重建副本index并过滤新增缓存/秘密，验证agent预先暂存__pycache__或.env也不进入补丁，保留正常新增源码，重复导出一致、不修改源目录。


## 上下文 V2：执行证据先保留后预览

本次只改变执行输出捕获，不恢复 SWE-bench 或模型调用。默认每流捕获 1 MiB 原始字节；output_limit 不再丢弃中间证据，Agent 入库后生成预览。测试涵盖 Local 输出中部与 stderr 完整返回、每流 4 MiB 输出在 4 KiB 捕获限额下仍执行到结尾、默认限额、超时前 stdout/stderr 保留及后代终止。Docker/Modal fake 经过真实 worker 命令验证 capture_limit RPC 传递与返回一致性；另有独立 worker stdin JSON 往返和非法限额在环境创建前拒绝。并行管道读取取代无界临时文件；超限始终报告 truncated。

本轮验证（2026-10-14）：`python -m pytest tests/test_environment.py -q` 为 32 passed；三个修改的 Python 文件 ruff 通过。Windows 上真实子进程覆盖上述输出、凭证和 timeout 条件，fake 不产生模型或云端调用。

另用既有通用镜像 `coding-harness-tools:469a9f6d9dfe67cf` 做真实 Docker 烟测（model_calls=0，未构建/下载镜像，未运行 SWE）：stdout 约 50 KiB 与 stderr 约 20 KiB 的中部标记完整返回；两流各 4 MiB 写入在每流 64 KiB 捕获额度下顺利退出并明确 truncated；超时前两流保留、后代 PID 已不存在；原始源文件不变，close 后 inspect 确认仅本次容器已删除。六项检查全部通过，Modal 仍仅 fake 验证。
