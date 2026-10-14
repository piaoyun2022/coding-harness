# 本机环境安装与验收记录

2026-10-14，Windows11教育版，约32GB内存；WSL2.6.3已存在。本次创建独立 coding-harness Ubuntu24.04发行版，位于D:\CodingHarnessRuntime\Ubuntu。未重启电脑，未删除旧demo。

Docker Desktop通过官方x86_64安装包安装至当前用户；执行前Authenticode验证Valid、签名Docker Inc。引擎docker info返回29.8.2。实际镜像数据已核验位于 D:\CodingHarnessRuntime\Docker\disk\docker_data.vhdx。后台启动，未开启宿主TCP端口。

WSL内 /opt/coding-harness-eval 已安装 swebench4.1.0；Docker的WSL集成已开启；WSL中docker info返回29.8.2，Python docker.from_env().ping()为True。Windows项目 .venv 已安装核心、开发、Modal可选依赖和benchmark数据依赖pyarrow23.0.1；openai-agents实际版本0.20.0。

真实DeepSeek连通探测通过：模型deepseek-flash，对应服务端名称DeepSeek-V4.1-Flash；一次极小请求返回OK。密钥仅在忽略的.env中，不进入容器/源码/提交。探测详情artifacts/api-probe.json。

全量SWE-bench Lite300题已下载到artifacts/swe-bench-lite，固定revision与checksum见manifest.json。没有正式分数。真实Docker工具冒烟已通过：失败测试、补丁修复、测试通过、导出补丁、源目录保持和容器清理全部验证；报告artifacts/docker-smoke.json。首轮真实模型联调暴露父Git忽略目录复制为空的问题，已修复并补回归；原失败保留于artifacts/live-agent-smoke。修复后的真实DeepSeek+Docker任务成功（artifacts/live-agent-smoke-fixed）：5轮、6677总token、9.531秒，apply_patch修复calc.py、两项测试通过，原输入不变。首题官方评分进行中。

初始磁盘余量C约20GB、D约42GB，全量镜像不能同时缓存。评测按并发1起步，先验证首题资源需求；只清理本任务拥有的资源，避免通用docker system prune。

来源：[Docker安装](https://docs.docker.com/desktop/setup/install/windows-install/)、[SWE-bench官方评测](https://www.swebench.com/SWE-bench/guides/evaluation/)。

首次全仓离线验收为125 passed、2 skipped；两项跳过涉及Windows创建symlink权限。最终修复后的测试数量以最后一次验收为准。

## 最终集成验收（首轮交付）

Windows完整离线测试147 passed、2 skipped；WSL Linux149 passed，无跳过。Windows的两项跳过是创建真实symlink权限，Linux已执行通过。Ruff与文档同步检查通过，wheel成功构建。Linux测试中已消除对python别名的依赖。

artifacts/live-agent-final为最终真实DeepSeek+Docker冒烟：8轮、12448总token、17.297秒；两项测试通过，源目录不变。补丁导出已加新增缓存/秘密过滤，即使预先git add也不会导出；所有改动和原失败轨迹分别保留。

首题裸模型官方验收（psf__requests-1963）成功：FAIL_TO_PASS7/7、PASS_TO_PASS112/112；单次生成35.813秒、23795tokens，官方评分64.599秒。初始8192输出预算截断的记录独立保留，校准后固定32768输出预算，不混分。详细证据artifacts/official-baseline-calibrated/smoke-summary.json。这是1题验证，不是完整基准成绩。

用户随后明确先跑10题。artifacts/swe-bench-smoke10记录固定随机种子42在完整300题中抽取的10题；裸模型批次artifacts/baseline-smoke10与官方验收artifacts/official-baseline-smoke10已启动。agent也按同10题另行运行，使用独立产物，不将冒烟样例或未评分生成数量冒充通过数。结果待实际完成后补充。
