# 上游贡献与第三方声明

## 直接依赖：Mi Fitness Data Bridge

- 作者与维护者：[shkyyy18 and contributors](https://github.com/shkyyy18/mi_fitness_data_bridge)
- 使用版本：0.3.3
- 固定提交：[`91ea4f15323c1c699e8c5ee8d044c0a820918851`](https://github.com/shkyyy18/mi_fitness_data_bridge/tree/91ea4f15323c1c699e8c5ee8d044c0a820918851)
- 许可：AGPL-3.0-only，并保留祖先代码的 MIT 声明。
- 贡献范围：小米云端协议适配、数据模型、本地 SQLite 缓存、同步服务、JSON/CSV 导出及兼容接口。这是桌面工具的核心能力，不应归功于本仓库作者。
- 集成方式：独立安装的 Python 依赖；本项目未修改或复制该上游的 Python 源码。
- [保留原历史的 fork](https://github.com/zhao0349/mi_fitness_data_bridge)，用于明确来源及后续向原项目贡献；安装仍固定到原作者仓库的上述提交。

本仓库新增中文桌面界面、独立 Edge 官网登录、Windows 凭据及账号隔离编排、日期导出交互，以及仪表盘的聚合、流式导入、详情视图、分时图表与测试。此说明按功能划分贡献，不将依赖代码计为本仓库原创，也不以缺乏证据的百分比衡量贡献。

## 祖先项目与协议参考

依据上述提交的上游 `LICENSE` 和 `THIRD_PARTY_NOTICES.md`：

- [kubulashvili/mi-fitness-mcp](https://github.com/kubulashvili/mi-fitness-mcp)：原始 MIT 项目，作者 Aleksej Kubulashvili。
- [binglua/mi-fitness-mcp-cn](https://github.com/binglua/mi-fitness-mcp-cn)：MIT 许可的中国地区适配，是直接上游的来源之一。
- [kevinkwee/Mi-Fitness-Sync](https://github.com/kevinkwee/Mi-Fitness-Sync)：上游 FDS 模块参考的协议笔记和 MIT 实现；上游声明除互操作所需协议常量外未逐字复制代码。

本项目未直接修改这些祖先仓库，因此保留链接和声明，不为它们创建无修改的重复 fork。fork 是来源关系与协作入口，不能替代许可证或署名义务。

## 保留的原始声明

[licenses/mi-fitness-data-bridge-LICENSE.txt](https://github.com/zhao0349/mifit-local/blob/main/licenses/mi-fitness-data-bridge-LICENSE.txt) 和 [licenses/mi-fitness-data-bridge-NOTICES.md](https://github.com/zhao0349/mifit-local/blob/main/licenses/mi-fitness-data-bridge-NOTICES.md) 原样保存固定上游提交的完整声明，包括：

> Copyright (c) 2026 Aleksej Kubulashvili
>
> Copyright (c) 2026 shkyyy18 and contributors

本项目新增代码 Copyright (c) 2026 zhao0349 and contributors，按 AGPL-3.0-only 发布，见根目录 `LICENSE`。运行依赖各自继续适用其发行包附带的许可证；本仓库不重新分发其安装包。没有包含小米源代码、标志或官方 SDK，与小米没有隶属或背书关系。
