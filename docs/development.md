# 开发约束与文档同步

源码在 src/，设计文档在平行 docs/。每次代码、测试、脚本、依赖或配置变更，必须同步修改对应设计说明。说明应包含意图、职责、方法契约、副作用、错误语义、验证依据和限制；仅增加空行不算更新。

## 映射与三种检查

docs/code-map.json 使用**精确文件路径**映射到一个 Markdown 文档或文档列表，不使用通配符。检查器要求当前快照中的每个受管文件都有映射，所有目标文档均存在且非空；空映射不能放行新增源码。失效映射必须删除。新增模块先添加说明与映射，再提交。

受管文件包括 src/、tests/、scripts/、.github/、.githooks/ 下全部受 Git 管理或未忽略的文件，以及根目录配置、依赖锁定文件、requirements*.txt、.env.example、.gitignore、.gitattributes、Dockerfile、Makefile 和根 shell 脚本。README/AGENTS 等纯文档不要求另配一份文档。真实 .env 必须保持 Git 忽略。

```text
python scripts/check_docs_sync.py
python scripts/check_docs_sync.py --staged
python scripts/check_docs_sync.py --base main
python scripts/check_docs_sync.py --base EMPTY
```

- 默认：HEAD 与当前工作区比较，包含未忽略的新文件。新仓库没有 HEAD 时以空树为基线。
- --staged：只比较 HEAD 与 Git 索引；映射、代码、文档全部从索引读取。工作区已写但未暂存的文档不能替代码提交过关；工作区后续变化也不能污染索引检查。
- --base REF：只比较指定提交与当前 HEAD，用于 CI；不读取未提交的内容。EMPTY 或 40–64 个零代表空树，用于首个提交或新分支推送。
- --staged 与 --base 互斥。有未解决合并索引条目时拒绝检查。

内部 Snapshot 对同一个快照统一读取文件清单与字节内容，check 返回可读错误列表，命令行成功退出 0，违规或读取失败退出 1。脚本不修改代码、映射、索引或历史。

## 删除、迁移和绕过边界

删除代码时读取旧提交中的映射，仍要求更新原文档，不能靠同时删掉当前映射绕过。整个组件连同专属文档删除可以通过；共享文档仍被其他当前源码引用时不能删除。

修改映射指向时，旧说明和新说明都必须更新，避免把代码临时指向一个碰巧修改的无关文档。文档是否发生实质变化通过去除空白后比较 UTF-8 内容判断；能拒绝纯空格/换行改动，但不能代替人工判断说明是否准确。

检查器验证的是一组净差异。CI 的跨提交检查不能证明每个中间提交都合规，因此本地 pre-commit 对每个提交的索引再执行检查。

## Hook 与 CI

启用本仓库 hook：

```text
git config core.hooksPath .githooks
```

pre-commit 依次使用 Windows .venv、POSIX .venv 和 PATH 的 Python，执行 --staged。文件必须保存为 LF，Git 模式应为可执行。首次配置由仓库维护者完成，脚本不修改全局 Git 设置。

GitHub Actions 在 Ubuntu 与 Windows、Python 3.12 上分别安装 dev 依赖、执行 ruff 和离线 pytest，并用 PR base 或 push before SHA 检查文档差异。checkout 拉取完整历史，初始推送的全零 SHA 作为空树处理。CI 不配置模型密钥，不调用真实 API、Modal 或完整 benchmark；安装依赖仍需要网络。

pyproject.toml 的职责及依赖版本见 [依赖说明](dependencies.md)。.gitignore 隔离本地密钥、虚拟环境、运行产物和缓存；.gitattributes 将文本统一为 LF；.env.example 只记录所需变量名，不能填入真实密钥。

## 验证依据

tests/test_docs_sync.py 创建独立临时 Git 仓库，验证：

- 单独代码改动失败，代码与文档一起更新通过。
- 未暂存文档不能修复 staged 检查，索引内容不受后续工作区回退影响。
- 未知源码、空映射、未暂存映射、纯空白文档改动被拒绝。
- 删除源码仍检查旧文档；重映射必须更新旧、新说明。
- 初始提交、首次暂存和跨提交 --base 比较有效。
- 测试、脚本、CI、hook、根配置和依赖文件都在检查范围。

这些测试只操作临时仓库，不运行模型、不接触用户源仓库，也不会触发主仓库 hook。
