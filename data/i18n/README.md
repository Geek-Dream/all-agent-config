# 界面汉化词表

这里放的是「把工具界面上的英文换成中文」的词表，由 `app/localize.py` 使用。
每个工具的 `app/agents.yaml` 里有一段 `i18n:` 声明用哪个词表、怎么定位程序。

## 两种驱动

| kind | 适用对象 | 做法 |
| --- | --- | --- |
| `text` | 普通 npm 包（Pi：`dist/**/*.js`） | 替换 `description: "..."` 字段，随便多长 |
| `binary` | 单文件原生/打包程序（Codex） | **等字节替换**，改完重新 `codesign` |

`binary` 的硬约束：中文一个汉字 3 字节，英文一个字母 1 字节，
所以译文必须 `len(utf8(中文)) <= len(ascii(英文))`，不足的补空格。
超长直接打不进去，`tests/test_localize.py` 会拦下来。

## 改坏了怎么办（三道保险）

1. **禁改名单 `<词表>.risky.json`**：同一段英文可能既是界面文案、又是程序内建脚本里的
   标识符，改了会导致语法错乱。列进名单的词一律跳过。
   （实例：Claude Code 的 `Reject`，改了以后程序直接报 `Invalid character` 起不来。）
2. **上机前预检**：补丁先写到 `~/.aac/i18n/<id>/` 的临时副本，签名后跑一次
   `<程序> --version`；报 `Unexpected error` / `Invalid character` 之类就整个放弃，
   系统目录里一个字节都不动。
3. **装完复检**：真装上之后再跑一次，失败自动还原英文原版（`.aac-orig`）。

原版程序自己就起不来时（假文件、异常安装）自检没法做，会注明「跳过自检」。

## 各工具现状

### codex —— 可用

- 62 条，斜杠命令描述、菜单、按钮、权限确认、状态提示
- 原生 Rust 程序，字符串是 UTF-8，等字节替换后中文显示正常（真机验证过）
- 备份：`<binary>.aac-orig`，`python -m app.localize codex --revert` 还原

### pi —— 可用

- 23 条，走 `text` 驱动，只改 `description:` 字段，不碰代码

### claude-code —— 不做（技术上做不了）

- 它是 Bun 打包的单文件（271 MB），字符串以 **Latin-1** 存在常量表里。
  往里塞 UTF-8 中文，渲染时按单字节解码 → 全是乱码。
  实测 `/mcp` 那条：`管理 MCP 服务` 显示成 `ç®¡ç\ufffd MCP æå¡`。
- 转义写法（`\u7ba1`）也没用：字符串在打包时就编译进常量表了，运行时不解析转义。
- 因此 `claude-code` 的 `i18n:` 声明已移除，本机二进制已还原英文原版。
- 留档：`claude.json`（78 条，逐条校验过长度和存在性）+ `claude.risky.json`。
  哪天 Claude Code 换成文本型安装（或官方出多语言），这批词表可以直接复用。

## 命令

```bash
python -m app.localize              # 看所有工具状态
python -m app.localize codex --apply
python -m app.localize codex --revert
```

界面里对应设置页「🀄 界面汉化」。安装 / 更新 / 启动前会自动重打补丁（可关）。
打补丁要改系统目录，会弹一次系统授权框；**不保存任何密码**，失败也不阻塞安装和启动。
