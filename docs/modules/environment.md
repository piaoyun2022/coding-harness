# Environment：执行后端

共同接口：description 告知 shell/root；execute(command, timeout) 返回 CommandResult；apply_patch(patch, timeout) 返回修改结果；collect_patch() 导出最终 Git diff；close() 清理。后端不判断任务完成，不接触模型密钥。

## LocalEnvironment

构造参数为 workspace、base_commit、output_limit=12000。只运行可信本地测试，必须由 CLI 显式 opt-in；目录副本无法隔离操作系统。close 标记已关闭；之后操作拒绝。补丁大小有界但本地 SDK 调用不能强制抢占 timeout。

## DockerEnvironment

参数：workspace、base_commit、output_limit=12000、lifetime=1200、allow_network=False、image=None、workdir='/workspace'、cpus=2.0、memory='4g'。

默认构建缓存镜像（Python 3.12、Git、ripgrep 与固定当前版本的补丁 SDK），通过 docker cp 上传已过滤快照和 worker，不 bind-mount 宿主目录、Docker socket、用户 home 或密钥。不传入宿主环境。容器禁用网络、去掉 capabilities、限制 CPU/内存/PID，并用 sleep 作为生命周期上限；模型任务本身不能延长容器存活。

image 非空时表示标准 SWE-bench 预制镜像，workdir 应为 /testbed：不上传/覆盖项目，从 conda-forge 追加独立 /opt/harness-tools Python 环境及补丁依赖，保留测试环境。每个 execute 尝试激活原 testbed conda。该模式依赖镜像已有 /opt/miniconda3，不承诺支持任意 OCI 镜像。

构造后检查 repository HEAD 必须匹配完整 base_commit，再运行 git status --porcelain --untracked-files=normal，要求工作树与索引干净且没有未忽略的新文件。两次检查都拒绝非零退出、超时或截断结果；不能把未知状态当成干净基线。HEAD 相同但已有跟踪文件改动或未忽略的附加文件也会被拒绝；这不检查被 Git 忽略的文件，镜像来源仍须可信。不重置未知镜像；验证/复制失败清理本实例容器。RPC 使用 stdin 承载 base64 JSON，避免 Windows 命令行长度限制。客户端 RPC 超时会直接终止自己的容器，因为 docker exec 客户端退出不能保证命令停止。close 核对随机名称与本实例 ownership label，仅删除本实例容器（包含 docker run 客户端超时但服务端可能已创建的情况），缓存镜像保留，绝不执行全局 prune。

构建阶段需要下载基础镜像与依赖，不属于运行时 network=none；首次构建可能较慢。构建缓存以 Dockerfile 哈希区分，不把浮动基础 tag 当 immutable digest。基础镜像来源、实例和 SDK 版本由评测元信息另记。

## ModalEnvironment

同样参数到 workdir，不含 Docker 的 cpus/memory 参数。普通镜像复制已过滤工作区；预制镜像使用 Image.from_registry(add_python='3.12')，保留 /testbed，追加工具 SDK 与 worker。沙箱网络默认关闭，不注入 Secrets，结束 terminate(wait=True)，构造后失败也清理。

参考官方 API：[Image](https://modal.com/docs/sdk/py/latest/Image)、[Sandbox](https://modal.com/docs/sdk/py/latest/Sandbox)。本地检查 SDK 签名，fake 测试覆盖调用与清理；未运行的云端行为不可宣称验证成功。

## 共同限制

沙箱网络开关与生命周期不代替预算逻辑；execute 的控制器超时允许额外清理时间。工具日志返回长度有上限，原始进程临时输出仍会占用磁盘。独立补丁格式和 Git diff 格式分离。可复现评分必须使用官方评分环境，不以 agent 自报成功替代。
