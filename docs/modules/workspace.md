# Workspace：独立输入快照

本模块只复制输入项目，不修改其工作区和 Git index。先筛选、检查大小，再新建目标目录，并建立内部初始提交；后续补丁相对此提交计算。

- `prepare_workspace(source, destination) -> dict`：source 必须存在，destination 必须尚不存在。返回 base_commit、实际复制的 files、excluded 和总 bytes。仅当 source 自身等于 Git 顶层目录时，通过 ls-files 复制 tracked 与 nonignored untracked 文件；其他显式目录（包括祖先仓库中被忽略的 fixture/子项目）遍历普通文件，不继承祖先 Git ignore 规则。普通目录仍执行本模块的秘密、缓存、链接过滤，但不会解析其 .gitignore；需要完整 Git ignore 语义时应传仓库根目录。筛选后强制加入初始快照，确保源仓库已跟踪但被后续 ignore 规则匹配的文件仍保留跟踪。
- 排除 .git、虚拟环境、依赖缓存、artifacts、常见凭证文件、符号链接和 Windows reparse/junction。单文件最多 5 MiB、合计 100 MiB；超限在创建目录前失败。筛选后没有任何文件也在创建目录前 fail fast，避免空输入继续消耗模型调用；首版不支持完全空项目输入，可先添加任务说明或 README。规则是常见凭证防误传，不是完整的秘密检测。
- `git` 不继承 GIT_* 环境、全局配置和 hooks；不使用原项目历史。
- `collect_patch(workspace, base_commit)` 先用原始基线重建隔离副本的index（不修改工作树或HEAD），暂存已跟踪文件的修改/删除，再逐个暂存经过过滤的新增文件，最后导出binary diff，最大10 MiB。新文件同时遵守Git ignore与快照的秘密/缓存/链接过滤，额外排除.pyc/.pyo；即使agent此前已暂存缓存或.env也不会混入导出。原基线已跟踪文件仍保持原有差异语义。这个index只属于隔离副本。
- 失败通过异常交由调用者处理。复制中途遇到 I/O 错误可能留下未完成的目标目录，调用者不得把它当成功结果；不会主动删除用户目录。
- 输出路径可以位于源项目内；候选集在创建前固定，且不复制输出自身。命令执行的任意写权限由沙箱提供，本模块不能让本地执行成为安全沙箱。

验证依据见 tests/test_workspace.py：源文件不变、Git ignore/秘密过滤、symlink/junction、补丁回放、被祖先仓库忽略的显式源目录，以及空树提前拒绝。


真实联调发现无.gitignore的小项目会把测试缓存导出，现有回归验证预先暂存的缓存和秘密仍不进入patch、新代码仍保留、重复导出一致且不删除工作树文件。
