# MiFit Local · 小米运动健康本地工具

中文 Windows 数据导出工具，以及可独立使用的单文件离线健康仪表盘。

[在线演示（虚构数据）](https://zhao0349.github.io/mifit-local/) · [使用说明](使用说明.html) · [上游与贡献归属](THIRD_PARTY_NOTICES.md)

## 功能

- Windows 桌面界面：通过独立 Edge 窗口登录小米官网，按日期同步并导出 JSON/CSV。
- 凭证保存到 Windows 凭据管理器；缓存和导出保存在项目目录内，按账号隔离。
- 离线仪表盘：日常活动、睡眠、心率、身体测量、运动概况、心率分时采样和睡眠阶段顺序。
- 在浏览器中导入 JSON、筛选记录、下载汇总，以及保存包含数据的单个 HTML。
- 无前端框架、CDN、分析统计或数据上传接口。时间统一按北京时间显示。

## 直接使用网页

打开[在线演示](https://zhao0349.github.io/mifit-local/)，初始内容全部是固定种子生成的虚构记录。点击“导入数据”选择 Mi Fitness Data Bridge 导出的 JSON，文件在浏览器本地处理；点击“保存离线副本”即可离线使用。

访问在线页面会向 GitHub Pages 请求网页；健康文件不会由应用上传。下载后的 HTML 包含导入的健康记录，请勿提交到公开仓库或公开分享。更重视隔离时，先保存演示 HTML，再断网打开并导入个人文件。

## Windows 桌面安装

需要 Windows、Python 3.11 或更新版本（含 Tk）、Git、PowerShell 7 和 Microsoft Edge。桌面凭证存储仅支持 Windows。

在 PowerShell 7 中执行：

```powershell
git clone https://github.com/zhao0349/mifit-local.git
Set-Location mifit-local
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\Start-MiFit.ps1
```

也可以双击 `启动小米数据导出.cmd`。先在手机“小米运动健康 Mi Fitness”中同步设备，再在桌面工具内登录和导出。本项目不适用于 Zepp Life。

运行依赖从固定的上游提交安装；`requirements-lock.txt` 是原 Windows/Python 3.14 环境的完整版本快照，其他 Python 版本建议使用 `requirements.txt`。

## 本地生成仪表盘

仅使用 Python 标准库即可生成演示，不需要安装桌面依赖：

```powershell
python dashboard_builder.py --demo --output dashboard/demo/健康仪表盘.html
```

已完成桌面同步后，可从当前账号的本机缓存生成私人仪表盘：

```powershell
.\.venv\Scripts\python.exe dashboard_builder.py
```

默认输出到 `dashboard/output/健康仪表盘.html`。个人缓存、导出和生成的网页均被 Git 忽略。不要使用 `git add -f` 将它们加入仓库。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
node --version
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check .
```

浏览器测试使用 Microsoft Edge 和临时浏览器配置；JavaScript 数据处理测试需要 Node.js。默认跳过大型虚拟文件压力测试，可设置 `MIFIT_RUN_WORKER_STRESS=1` 启用。测试使用虚构记录，不连接真实账号。CI 在 Windows 上执行同一套测试；这不能代替真实设备、账号和实体手机验收。

## GitHub Pages

在仓库 Settings → Pages 中选择 **GitHub Actions**。推送到 `main` 后，工作流先运行测试，再用 `python scripts/build_pages.py` 构建仅含虚构数据的 `_site/`，最后部署该目录。构建器没有个人数据库输入参数，且不复制本地生成的健康网页。

## 上游与许可

本项目不是小米官方产品。核心云端协议、缓存、数据模型和 JSON/CSV 导出由 [shkyyy18/mi_fitness_data_bridge](https://github.com/shkyyy18/mi_fitness_data_bridge) 提供，固定为版本 0.3.3、提交 `91ea4f15323c1c699e8c5ee8d044c0a820918851`；它作为独立依赖使用，未修改其源码。

本仓库新增中文桌面交互、官网登录衔接、账号隔离编排，以及离线可视化和浏览器导入。采用 **AGPL-3.0-only**；完整条款见 [LICENSE](LICENSE)，原始 MIT 归属及上游通知见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。软件不提供担保，健康展示不用于诊断；睡眠阶段时间由顺序和时长近似重建，不是精确阶段时间戳。
