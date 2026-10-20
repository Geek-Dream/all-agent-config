# PLAN — cc-switch 数据库选择性导入

## 目标
在「设置」页新增一块 **从 cc-switch 导入**：只读 `~/.cc-switch/cc-switch.db`，列出其中全部供应商（按工具分组），用户勾选后合并进 AAC 的 `~/.aac/providers.json`。

## 事实（已核实）
- cc-switch 数据在 `~/.cc-switch/cc-switch.db`（SQLite），`providers` 表 23 行，列含 `app_type/name/settings_config/is_current`。
- `settings_config` 各 app_type 形状：
  - claude: `{env:{ANTHROPIC_BASE_URL,ANTHROPIC_AUTH_TOKEN,ANTHROPIC_MODEL,...}}`
  - codex: `{auth:{OPENAI_API_KEY}, config:"<TOML 字符串>"}`
  - opencode: `{npm, options:{baseURL,apiKey}, models:{...}}`
  - pi: `{baseUrl,api,in key,models:[...]}`
  - openclaw: `{baseUrl,apiKey,api,models:[...]}`
  - gemini: 空（`{env:{},config:{}}`）
- app_type→AAC：claude→claude-code, codex→codex, opencode→opencode, pi→pi, openclaw→openclaw, gemini→gemini-cli。
- 原生配置：openclaw `~/.openclaw/openclaw.json` 的 `models.providers.<id>` 与 pi 同形；gemini 无端点字段（须写 `.env`，撞 R3）。

## 方案
1. `app/ccswitch.py`：只读打开 DB，`scan(db_path)` 返回每个 provider 映射后的条目 `{app_type, agent_id, name, base_url, api_key, model, small_model, extra, current, supported, note}`。
2. `providers.py`：新增 `merge_entries(agent_id, entries)`（按 base_url 去重合并，尊重 native_id）。
3. `agents.yaml`：为 openclaw 增加 `provider{adapter:openclaw-json, mode:coexist, target:~/.openclaw/openclaw.json}`；gemini-cli 保持 null（标注 R3 受限）。
4. `providers.py`：新增 openclaw 的 `_scan_openclaw` / `_apply_openclaw` 并注册。
5. 路由：`GET /api/ccswitch/scan`（含 DB 路径，默认探测，可覆盖）；`POST /api/ccswitch/import`（body: `{db, selected:[{app_type,name}]}`）。
6. settings：DB 路径存 `~/.aac/settings.json` 的 `ccswitch_db`。
7. 前端：设置弹窗加「cc-switch 导入」区块（路径输入 + 扫描 + 分组勾选 + 导入）。
8. gemini 条目在列表显示但禁用，注「需写 .env，受限」。

## 命令（均为只读或本地文件/测试）
- 读取 DB：`.venv/bin/python`（`mode=ro`）— 只读，不改 cc-switch。
- 写文件：仅改本仓库源码 + `~/.aac/*`（AAC 自身）。
- 验证：`make check`；重启本机 8600 实例（`lsof -ti :8600 | xargs kill`）。
- 不写 `.env`、不改 cc-switch DB。

## 验证
- `make check` 全绿（新增 ccswitch 单测 + openclaw 适配器单测）。
- 实测 `GET /api/ccswitch/scan` 返回 23 条；导入后 `~/.aac/providers.json` 出现对应条目，且 cc-switch DB 未被修改（mtime 不变）。

## 状态
- [x] 实现（ccswitch.py / openclaw 适配器 / 设置页入口 / 路由 / 测试）
- [x] 验证：`make check` 112 通过；实测 scan 23 条、import 成功且 cc-switch.db mtime 不变

## 已知限制
- providers 存储的 load/modify/save 无文件锁：极端并发（同时 GET 面板 + POST 导入/启用）可能丢失更新（本地单用户低风险）。
- Gemini 未支持（须写 .env，R3）。

## v0.2.0（本轮）
- 供应商面板：编辑即时生效（`_absorb_native` 改为仅新建写入，不再覆盖已编辑字段）；列表不再直显密钥（改为「编辑」页查看/复制）；线条风 SVG 图标；编辑表单就地展开；导入后清空勾选。
- 获取模型：`providers.fetch_models` + 复用的 `_models_request_target`（Claude `/v1/models`、其余 `/models`），前端下拉选择（替换被输入框文字过滤的 datalist）；`_MAX_MODELS=500`。
- cc-switch：`_sqlite_uri` 兼容 Windows 反斜杠 / 含空格用户名。
- 前端背景：白天草地 / 花 / 吃草的羊、黑夜无规律闪烁星空 + 半透明面板；主题切换圆形揭幕（纯 CSS `vt-reveal`，0.8s）+ 揭幕期间暂停重动画防卡。
- 版本：0.1.0 → 0.2.0（pyproject.toml / app/main.py）。
- 验证：`make check` 116 通过；`node --check` JS OK；Chromium 零报错。

## CI 修复：windows-latest（本轮）
- 根因 1：`Path.read_text()` / `open()` 未显式 `encoding="utf-8"`。Windows 默认 cp1252，读 AAC 自己写的含中文 JSON 会解码失败（Mac/Linux 默认 UTF-8，本地不可见）。
  - 修复：backup / bundle / categories / cloud / configview / patchwork 的文本读写统一显式 UTF-8；测试侧同步。
- 根因 2（修完编码后暴露）：测试对 Windows 路径语义的假设错误。
  - `Path.home()` 在 Windows 只认 `USERPROFILE`，不认 `HOME` → provider/ccswitch 夹具写到真实用户目录。修复：测试同时 patch 两者。
  - bundle 导出测试夹具是 posix 布局，需显式伪装本机为 macOS，否则 Windows 上 `from_os == to_os` 不做布局映射。
  - 分隔符断言写死 `/` → 改为 `os.path.join` / `Path` 规范化。
- CI：测试失败时通过 `::error` 注解输出失败摘要（公开可读，无需 token）。
- 验证：`make check` 116 OK；C locale 模拟 116 OK；GitHub Actions ubuntu/macos/windows 三平台全绿（c3831a6）。
- 教训：不要用 `>` 写入可能是指向真实文件的符号链接；写临时脚本前先 `ls -l` 确认不是链接（本轮曾穿透覆盖 Kimi 内置 python3.12，已用官方 3.2.16 更新包原样恢复并校验哈希）。
