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

### M3 补丁保护更新（safe-update）
- 点「更新」时自动检测程序修改，弹窗列出将被覆盖的文件
- **💾 备份补丁并更新**：更新前保存补丁快照（官方原貌 + 你的修改版），npm/brew 更新完成后自动重打：
  - 文件未被新版本改动 → 直接打回
  - 文件被新版本改动 → **锚点式三方合并**（唯一 token 锚点 + 窗口定位 + 公共前后缀核心，纯算法、不依赖 AI）把你的修改移植到新版本；实测 pi 的 3.3MB chunk 文件 0.5s 完成移植，修改 100% 保留
  - 定位不到或不确定 → **保守跳过**并在日志里明确列出，绝不写坏文件
- brew 源工具（如 Crush）支持自动安装/更新/卸载与版本查询（未 tap 的仓库需先 `brew tap`）

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
- [x] M3：safe-update 补丁保护更新（快照 → 更新 → 锚点合并自动移植）；brew 源支持
- [ ] M4：WebDAV / 网盘同步 .aac 包；增量备份计划任务；Windows 提权安装
- [ ] M5：导出包加密（含 API key 时的密码保护）

## 安全说明

- sudo 密码仅用于本次安装命令的 `sudo -S` 提权，不存储、不传输
- 配置导出包在本地生成，不上传任何服务器
- 导入覆盖前自动生成 `.bak-aac-*` 备份

## 相关项目

- [cc-switch](https://github.com/farion1231/cc-switch)：供应商切换 + 部分工具管理（Tauri 桌面端），与本项目互补
- [claudemove](https://github.com/bluewang88/claudemove)：Claude Code 配置备份迁移 CLI
