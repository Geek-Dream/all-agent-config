# All Agent Config

AI Agent 装机与配置迁移中心 —— 一键安装、更新、备份、迁移你本机的所有 Agent TUI 工具（Claude Code / Codex / Pi / Gemini CLI / Qwen Code / OpenCode / Kimi Code / Crush / Amp / Copilot CLI / Auggie / Continue / Aider / Goose / Cursor Agent …），跨 macOS 与 Windows。

**全程纯文件操作（哈希比对 / 打包 / 拷贝），不依赖任何 AI。**

## 快速开始

**一键启动（推荐）**：

```bash
./start.sh        # macOS / Linux；首次运行自动建环境，之后秒启
```

macOS 也可以在 Finder 里**双击 `start.command`**；Windows **双击 `start.bat`**。

手动方式：

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
aac                    # 启动并自动打开 http://127.0.0.1:8600
```

开发模式：`python -m app.main`（改 `app/static/index.html` 刷新即生效，改 `.py` 需重启）

开发检查：`make check`（标准库 unittest、compileall、注册表 smoke check、`git diff --check`）

## 功能

### M1 装机管理
- 启动自动扫描本机已安装的工具（npm 全局 + PATH 检测），不会重复安装
- 每个工具显示当前版本与最新版本（npm registry 实时查询），可更新高亮，一键更新全部
- 安装/更新/卸载支持**用户级**（装到 `~/.aac/npm`，免密）与**全局**（系统目录需要时经 `sudo -S` 提权，密码网页输入、不存储）
- 工具清单由 `app/agents.yaml` 注册表驱动，加新工具只改 YAML

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

### M4 云同步与自动备份
- **WebDAV 同步**（坚果云 / Nextcloud / 群晖，零依赖实现）：一键把导出包推送到云端、从云端拉取到新机器，换机不再需要手动拷贝
- **定时增量自动备份**：按间隔检查所选工具，配置（不含对话历史）有变化才自动导出 .aac 包，避免重复打包
- **Windows 全局安装提权**：UAC 弹窗授权（PowerShell `-Verb RunAs`），无需在网页输入密码
- 所有凭据仅存本机 `~/.aac/settings.json`；界面密码框不回显明文

### M5 体验与生态完善
- **三主题切换**：白天 / 黑夜 / 科技（橘黄黑流行星背景），右上角滑动切换、本地记忆；页面内容居中
- **五级排序**：可更新 > 已安装 > 可自动安装（未装）> 仅官网手动（显示「🌐 访问官网」）
- **二次确认弹窗**：安装 / 更新 / 卸载点击后弹确认（卸载带警示），三类操作在设置里独立开关
- **一键更新全部对齐单个更新**：只挑可更新项、弹窗列明工具名；有程序修改的工具自动跳过并提示走补丁保护，绝不静默覆盖
- **配置详情开关面板**：逐工具列出 skills / 扩展 / MCP / hooks 等配置项，滑块即开即关（技能目录用 `.aacoff` 后缀重命名，MCP/hooks 用 JSON 内 `aacDisabled` 命名空间，不删文件随时可恢复），分组可折叠
- **多安装管理器**：npm → pnpm → brew → scoop → winget → pipx 按本机可用自动选择（没装 npm 也能用 pnpm 装 npm 源的工具）
- **环境自检与一键装环境**：启动检测 node/npm/pnpm/brew/scoop/winget/pipx，工具栏实时显示；缺失的可一键安装（brew/winget/pip），装不了的给官方手动指引；安装工具时缺环境自动弹出引导
- **压缩包识别安装**：拖入 .zip/.tar.gz，自动解压、定位可执行文件、装入 `~/.aac/apps/`（用户级），出现在列表、可卸载
- **独立二进制工具受管卸载**（如 Kimi Code）：只删程序文件（bin/cache/updates），配置、会话、skills 保留
- **Windows 深度兼容**：`.cmd/.exe` shim 识别、npm 前缀布局差异、可写性判断、UAC 提权
- **注册表扩至 20 个主流 agent**：新增 Amp / GitHub Copilot CLI / Auggie / Continue CLI / Aider(pipx) / Goose / Cursor Agent，全部附官网链接
- **任务日志可读性**：`npm warn` 行淡化显示，真正的错误才醒目；操作失败可点黄色「重试」按钮

### M5+ 跨平台配置与会话迁移（.aac 包 format v2）
- **路径归一化**：包内路径一律 posix 相对 home 存储，清单记录 `source_os` / `source_home`
- **跨系统目录布局映射**：注册表用 `platform_dirs` 声明同一目录在两套系统下的位置
  （如 `~/.config/opencode` ↔ `%APPDATA%/opencode`），导入时按本机系统自动改写，
  真正的 Mac↔Win 可用；导出时还可选「目标平台」提前按目标系统布局书写包内路径
- **内容级路径映射**：导入时把配置与会话文件里的源机 home 与配置目录绝对路径替换为本机
  （含 JSON 转义写法），并按目标系统规范化路径分隔符，不再出现 `C:\Users\x/dev` 混写
- **Claude 会话目录跨机重编码**：`.claude/projects/-Users-wl-*` 按本机 home 重编码（Win 为 `C--Users-*`），同位置检出的项目换机后历史对话直接接上
- **对话历史时间筛选**：导出时可选 全部 / 近 7 / 30 / 90 天，实时显示将打包的文件数与体积
- **超大文件保护**：单文件 >100MB 不进包，清单记录并在导入预览中明示
- **导入冲突预览**：逐分类三色显示 ⚠️覆盖 / ✚新增 / ⤼跳过 数量（覆盖前自动 `.bak-aac-*` 备份），跨系统与补丁版本不匹配提前警告
- **safe-update 补丁闭环**：更新后自动重打补丁并再次比对官方包验证，日志报告「✅ N 个全部完好 / ⚠️ M 个未移植」

### M5++ 供应商配置中心（对标 cc-switch 核心）
- **多套 API 供应商**：为 Claude Code / Codex / OpenCode / Pi / OpenClaw 保存多套「地址 + 密钥 + 模型」，一键切换
- **从 cc-switch 导入**：设置页可只读 cc-switch 的 `~/.cc-switch/cc-switch.db`，按工具分组列出其中全部供应商，勾选后合并进本机（**只读、不改动 cc-switch**；Gemini 因需写 `.env` 暂不支持）
- **打开即自动扫描**：进入面板时列出工具配置里已定义的全部供应商（地址/密钥/模型），并高亮「使用中」的那条，老配置无需手动导入
- **只改连接字段**：切换只替换请求地址、API Key、模型，插件、Hooks、MCP 和你自己加的设置原样保留；写回前自动备份 `.bak-aac-*`
- **切换式 / 共存式**：Claude Code、Codex 为切换式（同时只启用一套）；OpenCode 为共存式（多套写入 `provider` 段，工具内切换）
- **密钥安全**：密钥仅存本机 `~/.aac/providers.json`（权限 0600）；面板列表默认以占位符 `••••••••` 显示，可点 👁 查看明文、📋 一键复制（服务只监听 `127.0.0.1`），普通列表接口仍脱敏
- **进包可选加密**：供应商配置作为独立分类进 `.aac` 包；导出时可勾选「加密导出」为整包 AES-256-GCM 加密（scrypt 派生密钥），**默认不加密**，导入时自动识别并提示输入密码

## 目录结构

```
app/
  main.py       FastAPI 入口与 API 路由
  registry.py   注册表加载
  detect.py     已安装检测 / 版本查询 / 多前缀扫描（含 ~/.aac/apps、Windows shim）
  installer.py  安装 / 更新 / 卸载（npm/pnpm/brew/scoop/winget/pipx、sudo 与 UAC 提权）
  env.py        运行环境（node/npm/pnpm/brew/scoop/winget/pipx）检测与一键安装
  categories.py 配置分类扫描 + 程序补丁检测（vs 官方包哈希比对）
  configview.py 配置详情开关面板（skills/扩展/MCP/hooks 启用状态读写）
  bundle.py     .aac 包导出 / 预览 / 应用（跨平台路径映射、冲突统计、可选加密）
  providers.py  供应商配置中心（多套 API 端点/密钥、写回配置、脱敏）
  ccswitch.py   只读读取 cc-switch 的 SQLite 库，供选择性导入
  crypto.py     .aac 包加密（scrypt + AES-256-GCM）
  patchwork.py  补丁快照 + 锚点式三方合并移植
  cloud.py      WebDAV 客户端（PUT/GET/PROPFIND/MKCOL）与设置存储
  backup.py     定时增量自动备份后台任务
  static/       Web 前端（单页，三主题 + 滑块式分类选择 + 设置面板）
  agents.yaml   20 个 Agent 工具的安装源（npm/brew/scoop/winget/pipx/manual）、
                官网、配置目录、会话目录与分类定义
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
| providers | 供应商配置（API 端点与密钥，含密钥） | 无 |
| patches | 对程序本体的修改（汉化、进度条魔改） | **高**——升级 agent 会覆盖，导入前页面会校验版本兼容性 |
| conversations | 对话历史（体积可能较大，导出时可按时间范围筛选） | 无 |

## .aac 包格式（v2）

zip 容器，结构如下：

```
manifest.json     格式版本、source_os / source_home / path_platform / conv_days、
                  各分类条目（含 rel_home posix 路径）、platform_dirs 快照、
                  跳过的大文件清单、补丁哈希
<category>/...    各分类文件（相互独立，导入时逐个开关）
providers/...     供应商配置（多套 API 端点/密钥）
patches/...       程序级修改文件（保持包内相对路径）
```

加密包：导出时勾选「加密导出」并设置密码，则整个包以 scrypt 派生密钥 + AES-256-GCM 加密，
文件头带魔术字节，导入时自动识别并提示输入密码；不勾选则导出为普通 zip（默认）。

导入端行为：rel_home 按本机 home 还原（Windows 自动换分隔符）；`platform_dirs`
声明跨系统目录（`~/.config/*` ↔ `%APPDATA%/*`）时按本机系统改写；文本类文件
（.json/.jsonl/.toml/.md 等）里的源机 home 与配置目录绝对路径自动替换为本机，
分隔符按目标系统规范化；Claude 会话项目目录按本机 home 重编码；无法映射的异系统
绝对路径跳过并记日志。

## 路线图

- [x] M1：检测 / 安装 / 更新 / 卸载（用户级 + 全局 sudo）
- [x] M2：分类导出/导入、程序补丁检测、对话迁移
- [x] M3：safe-update 补丁保护更新（快照 → 更新 → 锚点合并自动移植）；brew 源支持
- [x] M4：WebDAV 云同步、定时增量自动备份、Windows UAC 提权
- [x] M5：三主题/确认弹窗/配置开关面板；npm→pipx 多管理器 + 环境自检 + 压缩包安装；
      跨平台路径映射（Mac↔Win）、会话时间筛选、导入冲突预览、补丁闭环验证；20 个 agent
- [x] M5++：供应商配置中心（多套 API 端点/密钥、一键切换、共存/切换式、脱敏、进包加密）
- [x] M6：导出包加密（AES-256-GCM + scrypt 密码保护）
- [ ] M7：.aac 包增量压缩；会话阅读视图

## 安全说明

- sudo 密码通过本机回环 HTTP 接口提交，仅用于当前一次 `sudo -S` 提权命令；不存储、不上传
- WebDAV 仅在用户主动点击推送/拉取时访问配置的远端；自动备份只在本机生成 `.aac` 包，凭据保存在本机 `~/.aac/settings.json`
- 导入包内 `rel_home` 目标仅允许本机 home 内路径；程序补丁仅允许当前已识别安装目录；越界、链接和特殊归档成员会跳过或拒绝
- 供应商密钥仅存本机 `~/.aac/providers.json`（权限 0600）；列表接口脱敏，明文只在点 👁 / 📋 时
  按需由回环（127.0.0.1）接口返回；含密钥的 `.aac` 包可在导出时勾选「加密导出」（scrypt + AES-256-GCM），默认不加密
- 默认服务仅监听 `127.0.0.1`。不要将端口代理或暴露到局域网；本工具未提供账户鉴权

## 相关项目

- [cc-switch](https://github.com/farion1231/cc-switch)：供应商切换 + 部分工具管理（Tauri 桌面端），与本项目互补
- [claudemove](https://github.com/bluewang88/claudemove)：Claude Code 配置备份迁移 CLI
