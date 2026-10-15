# 运行环境与验证边界

## 新机器的前置条件

准备 Git、Python 3.11+ 及能够执行 Linux 容器的 Docker。先在将运行评测的 Linux 或 WSL 终端验证 `docker info` 成功。Windows 使用 WSL2 与 Docker Desktop 的 WSL 集成；本仓库不安装、重启或重新配置这些系统服务。

填写仓库根目录 `.env` 中的 `DEEPSEEK_API_KEY` 后，使用 [README](../README.md) 的一键入口；依赖安装、题集准备、批量生成、官方评分和输出位置详见 [评测指南](evaluation.md)。路径均以本地 checkout 为准，不要求特定用户名、盘符或系统级虚拟环境。

## 已有验证与当前结果

实际开发环境为 Windows 11 / WSL2 / Linux Docker，宿主约 32 GB 内存。已验证真实 DeepSeek API、容器内命令与补丁、相关测试通过后导出补丁、输入目录保持不变，以及独立官方评分。历史预备阶段使用过单题与十题冒烟；它们不是公开汇总表的分母。

C 版原顺序前 30 题最终为 20/30，裸模型同题为 9/30；完整结果、停止原因和预算以 [评测结果](evaluation-results.md) 为准。历史实验曾采用机器专用的虚拟环境路径和调度辅助脚本；公开入口将这些步骤收为仓库内脚本。新增入口的测试通过不代表再次获得该成绩。

镜像下载、构建和测试会消耗磁盘与内存，并发度需要与机器资源匹配。仅清理本轮拥有且不再使用的容器或镜像，不使用全局 Docker prune。

## 参考

- [Docker Desktop Windows 安装](https://docs.docker.com/desktop/setup/install/windows-install/)
- [SWE-bench 官方评测](https://www.swebench.com/SWE-bench/guides/evaluation/)
