# All Agent Config

AI Agent 装机与配置迁移中心 —— 一键安装、更新、备份、迁移你本机的所有 Agent TUI 工具（Claude Code / Codex / Pi / Gemini CLI / Qwen Code / OpenCode / Kimi Code / Crush …），跨 macOS 与 Windows。

**全程纯文件操作（哈希比对 / 打包 / 拷贝），不依赖任何 AI。**

## 快速开始

```bash
cd all-agent-config
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
aac                    # 启动并自动打开 http://localhost:8600
```

开发模式：`python -m app.main`

## 功能

### M1 装机管理
- 启动自动扫描本机已安装的工具（npm 全局 + PATH 检测），不会重复安装
- 每个工具显示当前版本与最新版本（npm registry 实时查询），可更新高亮，一键更新全部
- 安装/更新/卸载支持**用户级**（装到 `~/.aac/npm`，免密）与**全局**（系统目录需要时经 `sudo -S` 提权，密码网页输入、不存储）
- 工具清单由 `registry/agents.yaml` 注册表驱动，加新工具只改 YAML

### M2 配置迁移（核心）
- **八类配置独立导出/导入**：系统配置、Hooks 钩子、Skills 技能、MCP 服务器、主题/美化、自定义个人配置、**程序级修改（补丁）**、对话历史 —— 应用时每个分类一个滑块独立开关，一类坏了不影响其他
- **程序补丁检测**：对 npm 安装的工具，下载同版本官方包逐文件比对 SHA-256，自动发现你对程序本体的修改（汉化补丁、进度条/标题栏魔改等）和新增文件，导出为补丁分类
- **JSON 键级提取**：hooks / mcpServers 等字段从配置文件单独提取打包，导入时合并进目标文件（不动文件里其他设置）
- **导入防丢**：覆盖前自动备份原文件为 `.bak-aac-*`
- **对话迁移**：可选打包 `~/.claude/projects`、`~/.pi/agent/sessions` 等，换机后对话无缝衔接
- **无感换机**：导出为一个 `.aac` 包（zip），新机器打开网页导入、勾选需要的分类即可

## 目录结构

```
app/
  main.py       FastAPI 入口与 API 路由
  registry.py   注册表加载
  detect.py     已安装检测 / 版本查询（semver 比较）
  installer.py  安装 / 更新 / 卸载（用户级与全局，sudo 提权）
  categories.py 配置分类扫描 + 程序补丁检测（vs 官方包哈希比对）
  bundle.py     .aac 包导出 / 预览 / 应用
  static/       Web 前端（单页，滑块式分类选择）
registry/
  agents.yaml   12+ 个 Agent 工具的安装源、配置目录与分类定义
```

## 导入时分类说明

| 分类 | 内容 | 升级冲突风险 |
|---|---|---|
| system | 官方配置文件（settings、keybindings 等） | 低 |
| hooks | 钩子配置（JSON 键提取，合并式导入） | 低 |
| skills | 技能包目录 | 低 |
| mcp | MCP 服务器配置（JSON 键提取） | 低 |
| theme | 主题/美化配置 | 低 |
| custom | 你的个人文件（CLAUDE.md、statusline、扩展等） | 低 |
| patches | 对程序本体的修改（汉化、进度条魔改） | **高**——升级 agent 会覆盖，导入前页面会校验版本兼容性 |
| conversations | 对话历史（体积可能较大） | 无 |

## 路线图

- [x] M1：检测 / 安装 / 更新 / 卸载（用户级 + 全局 sudo）
- [x] M2：分类导出/导入、程序补丁检测、对话迁移
- [ ] M3：升级前自动提示"你的补丁会被覆盖，先导出"；非 npm 源自动安装（brew / scoop / 官方脚本）；Windows 提权
- [ ] M4：WebDAV / 网盘同步 .aac 包；增量备份计划任务
- [ ] M5：导出包加密（含 API key 时的密码保护）

## 安全说明

- sudo 密码仅用于本次安装命令的 `sudo -S` 提权，不存储、不传输
- 配置导出包在本地生成，不上传任何服务器
- 导入覆盖前自动生成 `.bak-aac-*` 备份

## 相关项目

- [cc-switch](https://github.com/farion1231/cc-switch)：供应商切换 + 部分工具管理（Tauri 桌面端），与本项目互补
- [claudemove](https://github.com/bluewang88/claudemove)：Claude Code 配置备份迁移 CLI
