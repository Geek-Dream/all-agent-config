# All Agent Config

AI Agent 装机与配置迁移中心 —— 一键安装、更新、备份、迁移你本机的所有 Agent TUI 工具（Claude Code / Codex / Pi / Gemini CLI / Qwen Code / OpenCode / Kimi Code / Crush …），跨 macOS 与 Windows。

## 快速开始

```bash
cd all-agent-config
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
aac                    # 启动并自动打开 http://localhost:8600
```

开发模式：`python -m app.main`

## 当前进度（M1）

- ✅ 启动时自动扫描本机已安装的工具（npm 全局 + PATH 检测）
- ✅ 显示每个工具的当前版本与最新版本（npm registry 实时查询），可更新高亮
- ✅ 一键安装 / 更新 / 卸载（npm 源），支持**用户级**（装到 `~/.aac/npm`，免密）与**全局**（系统目录，需要时通过 sudo 提权，密码在网页输入）
- ✅ 任务日志面板，实时滚动输出
- ✅ 工具清单由 `registry/agents.yaml` 注册表驱动，加新工具只改 YAML

## 路线图

- [ ] M2：配置导出 / 导入（.aac 包），改动 diff 快照（标记你自定义修改过的文件，如状态栏、汉化补丁），升级前提醒先导出
- [ ] M3：对话历史迁移（`~/.claude/projects`、`~/.pi/agent/sessions` 等），无感换机
- [ ] M4：非 npm 源工具自动安装（brew / scoop / 官方脚本），Windows 提权安装
- [ ] M5：MCP / skills / hooks 配置项级勾选导出

## 目录结构

```
app/
  main.py       FastAPI 入口与 API 路由
  registry.py   注册表加载
  detect.py     已安装检测 / 版本查询
  installer.py  安装 / 更新 / 卸载（用户级与全局，sudo 提权）
  static/       Web 前端（单页）
registry/
  agents.yaml   12+ 个 Agent 工具的安装源与配置目录注册表
```

## 安全说明

- sudo 密码仅用于本次安装命令的 `sudo -S` 提权，不存储、不传输。
- 配置导出包为本地生成，不会上传到任何服务器。

## 相关项目

- [cc-switch](https://github.com/farion1231/cc-switch)：供应商切换 + 部分工具管理（Tauri 桌面端），与本项目互补
- [claudemove](https://github.com/bluewang88/claudemove)：Claude Code 配置备份迁移 CLI
