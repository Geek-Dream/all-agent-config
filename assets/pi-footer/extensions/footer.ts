/**
 * aac-footer —— tag 化状态栏（三行可自定义）
 *
 * 为什么单独做一个扩展：原来 footer 的每一段都写死在代码里，想删一段就得改源码。
 * 这里每一段都是一个 tag，显示哪些、什么顺序、用什么分隔符，全读
 * `~/.aac/footer.json`（跟 aac 设置页是同一份配置，能被 .aac 包带到别的电脑）。
 *
 * 配置文件长得像这样：
 *   {
 *     "version": 1,
 *     "enabled": true,
 *     "separator": "bar",            // bar │ / space 空格 / slash / / dot ·
 *     "lines": [["model","thinking"], ["mem","cpu","cache","speed"],
 *               ["tokens","cache_time"]],
 *     "agents": { "pi": { ... } }     // 可选：只改 pi 的
 *   }
 *
 * 配置读不出来 / 写坏了 → 直接用默认三行模板，绝不白屏、绝不报错。
 */
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import type { AssistantMessage } from "@mariozechner/pi-ai";
import { truncateToWidth } from "@mariozechner/pi-tui";

// ---------------------------------------------------------------- 配置读取

const CONFIG_PATH = process.env.AAC_FOOTER_CONFIG ?? path.join(os.homedir(), ".aac", "footer.json");
const SEPARATORS: Record<string, string> = { bar: "│", space: " ", slash: "/", dot: "·" };
const DEFAULT_LINES: string[][] = [
  ["model", "thinking"],
  ["mem", "cpu", "cache", "speed"],
  ["tokens", "cache_time"],
];
const MAX_LINES = 3;
const BAR_WIDTH = 10;

/** 上下文占用阶段：规划 → 编码 → 拥挤 → 告急 */
function zoneOf(pct: number): { label: string; color: ZoneColor } {
  if (pct < 40) return { label: "规划", color: "success" };
  if (pct < 70) return { label: "编码", color: "accent" };
  if (pct < 80) return { label: "拥挤", color: "warning" };
  return { label: "告急", color: "error" };
}

interface FooterConfig {
  enabled: boolean;
  separator: string;
  lines: string[][];
  cnyPerUsd: number;
}

function cleanLines(raw: unknown): string[][] {
  if (!Array.isArray(raw)) return DEFAULT_LINES;
  const out: string[][] = [];
  for (const line of raw) {
    if (!Array.isArray(line)) continue;
    const tags = line.filter((t): t is string => typeof t === "string");
    if (tags.length) out.push(tags.slice(0, 8));
    if (out.length >= MAX_LINES) break;
  }
  return out.length ? out : DEFAULT_LINES;
}

function normalize(raw: unknown, agentId = "pi"): FooterConfig {
  const cfg = (raw ?? {}) as Record<string, unknown>;
  const own = (cfg.agents as Record<string, unknown> | undefined)?.[agentId] as
    | Record<string, unknown>
    | undefined;
  const src = { ...cfg, ...(own ?? {}) };
  const sep = typeof src.separator === "string" && src.separator in SEPARATORS ? src.separator : "bar";
  const rawRate = Number(src.cny_per_usd);
  const cnyPerUsd = Number.isFinite(rawRate) && rawRate >= 1 && rawRate <= 20 ? rawRate : RATE_DEFAULT;
  return { enabled: src.enabled !== false, separator: sep, lines: cleanLines(src.lines), cnyPerUsd };
}

let cachedConfig: FooterConfig | null = null;
let cachedMtime = -1;
let cachedAt = 0;

function loadConfig(): FooterConfig {
  const now = Date.now();
  if (cachedConfig && now - cachedAt < 1500) return cachedConfig;
  cachedAt = now;
  try {
    const stat = fs.statSync(CONFIG_PATH);
    if (cachedConfig && stat.mtimeMs === cachedMtime) return cachedConfig;
    cachedMtime = stat.mtimeMs;
    cachedConfig = normalize(JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")));
  } catch {
    // 文件不存在 / 坏了：回默认模板（不抛异常，状态栏照常显示）
    cachedConfig = normalize(null);
    cachedMtime = -1;
  }
  return cachedConfig;
}

// ---------------------------------------------------------------- 采样

const RATE_DEFAULT = 7.2;
const CACHE_TTL_MS = 5 * 60 * 1000;
const CIRCLE_ANIM = ["◉", "⊙", "◌"];
const MEM_SAMPLE_MS = 2000;

type ZoneColor = "success" | "accent" | "warning" | "error" | "dim" | "text" | "muted";

let cpuPrev: { idle: number; total: number } | null = null;
let cpuPercent: number | null = null;
let memPercent: number | null = null;
let memSampledAt = 0;

function sumCpuTimes(): { idle: number; total: number } {
  let idle = 0;
  let total = 0;
  for (const cpu of os.cpus()) {
    const t = cpu.times;
    idle += t.idle;
    total += t.user + t.nice + t.sys + t.idle + t.irq;
  }
  return { idle, total };
}

/** CPU 占用 = 两次采样的时间差（loadavg 是 1 分钟平均负载，不是占用率）。 */
function sampleCpu(): number {
  const cur = sumCpuTimes();
  if (cpuPrev) {
    const idleDelta = cur.idle - cpuPrev.idle;
    const totalDelta = cur.total - cpuPrev.total;
    if (totalDelta > 0) {
      const raw = ((totalDelta - idleDelta) / totalDelta) * 100;
      cpuPercent = cpuPercent == null ? raw : cpuPercent * 0.6 + raw * 0.4;
    }
  }
  cpuPrev = cur;
  return cpuPercent ?? 0;
}

/** macOS 用 vm_stat（os.freemem 会一直报 99% 空闲），其它平台退回 freemem。 */
function sampleMem(now: number): number {
  if (memPercent != null && now - memSampledAt < MEM_SAMPLE_MS) return memPercent;
  memSampledAt = now;
  const total = os.totalmem();
  if (total <= 0) return 0;
  if (process.platform === "darwin") {
    try {
      const out = execFileSync("vm_stat", { encoding: "utf8", timeout: 1000 });
      const pageSize = Number(/page size of (\d+) bytes/.exec(out)?.[1] ?? 0);
      const pages = (label: string) =>
        Number(new RegExp(`^${label}:\\s+(\\d+)\\.`, "m").exec(out)?.[1] ?? NaN);
      const usedPages =
        pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor");
      if (pageSize > 0 && Number.isFinite(usedPages) && usedPages > 0) {
        memPercent = Math.min(100, Math.max(0, ((usedPages * pageSize) / total) * 100));
        return memPercent;
      }
    } catch {
      // 落到下面的兜底公式
    }
  }
  memPercent = Math.min(100, Math.max(0, ((total - os.freemem()) / total) * 100));
  return memPercent;
}

function loadColor(pct: number): ZoneColor {
  if (pct >= 85) return "error";
  if (pct >= 60) return "warning";
  return "success";
}

const THINKING_COLORS: Record<string, string> = {
  off: "thinkingOff",
  minimal: "thinkingMinimal",
  low: "thinkingLow",
  medium: "thinkingMedium",
  high: "thinkingHigh",
  xhigh: "thinkingXhigh",
  max: "thinkingMax",
};

function fmtCountdown(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}

function gitBranch(ctx: ExtensionContext): string {
  try {
    const out = execFileSync("git", ["rev-parse", "--abbrev-ref", "HEAD"], {
      cwd: ctx.cwd ?? process.cwd(),
      encoding: "utf8",
      timeout: 800,
      stdio: ["ignore", "pipe", "ignore"],
    });
    return out.trim();
  } catch {
    return "";
  }
}

interface SessionStats {
  cost: number;
  tps: number | null;      // null = 拿不到可信耗时，别显示 0 或 "?"
  hitRate: number | null;  // null = 这个会话还没有缓存样本
  lastCacheTs: number;
  tokens: number | null;
  ctxPct: number | null;
}

function collectStats(ctx: ExtensionContext): SessionStats {
  let cost = 0;
  let lastCacheTs = 0;
  const samples: { output: number; durMs: number }[] = [];
  const recent: { input: number; cacheRead: number }[] = [];
  for (const e of ctx.sessionManager.getBranch()) {
    if (e.type === "message" && e.message.role === "assistant") {
      const m = e.message as AssistantMessage & { timestamp?: number; durationMs?: number };
      const u = m.usage;
      cost += u.cost.total;
      recent.push({ input: u.input, cacheRead: u.cacheRead });
      const ts = (m as any).timestamp ?? 0;
      // 只收「有输出 + 有耗时」的样本：缺一个都算不出速度，宁可不算
      if (u.output > 0 && (m.durationMs ?? 0) > 0) {
        samples.push({ output: u.output, durMs: m.durationMs as number });
      }
      if ((u.cacheRead > 0 || u.cacheWrite > 0) && ts > lastCacheTs) lastCacheTs = ts;
    }
  }
  // 最近 5 条平均，单条容易被首字延迟拉低
  const tail = samples.slice(-5);
  let tps: number | null = null;
  if (tail.length) {
    const out = tail.reduce((a, b) => a + b.output, 0);
    const ms = tail.reduce((a, b) => a + b.durMs, 0);
    if (out > 0 && ms >= 300) tps = out / (ms / 1000);
  }
  const win = recent.slice(-10);
  let winInput = 0;
  let winCache = 0;
  for (const r of win) {
    winInput += r.input;
    winCache += r.cacheRead;
  }
  const hitRate = winInput + winCache > 0 ? (winCache / (winInput + winCache)) * 100 : null;
  const usage = ctx.getContextUsage?.();
  const tokens = typeof usage?.tokens === "number" && usage.tokens > 0 ? usage.tokens : null;
  const window = usage?.contextWindow ?? ctx.model?.contextWindow ?? 0;
  const ctxPct =
    tokens != null && window > 0 ? Math.min(100, (tokens / window) * 100) : null;
  return { cost, tps, hitRate, lastCacheTs, tokens, ctxPct };
}

// ---------------------------------------------------------------- 渲染

type Theme = Parameters<Parameters<ExtensionContext["ui"]["setFooter"]>[0]>[1];

function renderTag(
  tag: string,
  s: SessionStats,
  ctx: ExtensionContext,
  theme: Theme,
  branch: string,
  frame: number,
  cfgRate: number,
): string | null {
  switch (tag) {
    case "model":
      return theme.fg("muted", ctx.model?.id ?? "no-model");
    case "thinking": {
      const level = ctx.thinkingLevel ?? "off";
      const color = (THINKING_COLORS[level] ?? "muted") as any;
      return theme.fg("dim", "[") + theme.fg(color, level) + theme.fg("dim", "]");
    }
    case "mem": {
      const mem = Math.min(999, Math.round(sampleMem(Date.now())));
      return theme.fg("dim", "内存 ") + theme.fg(loadColor(mem), `${mem}%`);
    }
    case "cpu": {
      const cpu = Math.min(999, Math.round(sampleCpu()));
      return theme.fg("dim", "CPU ") + theme.fg(loadColor(cpu), `${cpu}%`);
    }
    case "cache": {
      if (s.hitRate == null) return null;   // 没有缓存样本就不显示 0%
      const rate = s.hitRate;
      const color: ZoneColor = rate >= 80 ? "success" : rate >= 50 ? "warning" : "dim";
      return (
        theme.fg(color, CIRCLE_ANIM[frame % CIRCLE_ANIM.length]) +
        theme.fg("dim", " ") +
        theme.fg(color, `${rate.toFixed(0)}%`)
      );
    }
    case "context": {
      const pct = s.ctxPct;
      const zone = pct == null ? { label: "未知", color: "dim" as ZoneColor } : zoneOf(pct);
      const filled = pct == null ? 0 : Math.round((pct / 100) * BAR_WIDTH);
      const bar =
        theme.fg(zone.color, "▰".repeat(filled)) +
        theme.fg("dim", "▱".repeat(BAR_WIDTH - filled));
      return (
        bar +
        theme.fg("dim", " ") +
        theme.fg(zone.color, pct == null ? "?" : `${pct.toFixed(1)}%`) +
        theme.fg("dim", " ") +
        theme.fg(zone.color, zone.label)
      );
    }
    case "speed":
      return s.tps != null && s.tps > 0
        ? theme.fg("text", s.tps.toFixed(1)) + theme.fg("dim", " 字/秒")
        : null;                            // 算不出来就整段不出现
    case "tokens":
      return s.tokens == null
        ? null
        : theme.fg("dim", "已用 tokens：") + theme.fg("muted", s.tokens.toLocaleString("en-US"));
    case "cache_time": {
      if (s.lastCacheTs <= 0) return null;
      const remain = CACHE_TTL_MS - (Date.now() - s.lastCacheTs);
      return remain > 0
        ? theme.fg(remain < 60_000 ? "warning" : "text", `⏳ 缓存 ${fmtCountdown(remain)}`)
        : theme.fg("dim", "⏳ 缓存已过期");
    }
    case "cwd": {
      const dir = path.basename(ctx.cwd ?? process.cwd());
      return theme.bold(theme.fg("accent", `📂 ${dir}`));
    }
    case "git":
      return branch ? theme.fg("success", `🌿 ${branch}`) : null;
    case "session": {
      const name = (ctx.sessionManager as any).getSessionName?.() ?? "";
      return name ? theme.fg("dim", `会话 ${name}`) : null;
    }
    case "time":
      return theme.fg("dim", new Date().toLocaleTimeString("zh-CN", { hour12: false }).slice(0, 5));
    case "cost":
      // ≈ 表示按汇率估算，不是账单金额；没单价时如实写未计价
      return s.cost > 0
        ? theme.bold(theme.fg("accent", `≈¥${(s.cost * cfgRate).toFixed(2)}`))
        : theme.fg("dim", "¥— 未计价");
    default:
      return null;
  }
}

// ---------------------------------------------------------------- 挂载

export default function aacFooter(pi: ExtensionAPI) {
  let mounted = false;

  const mount = (ctx: ExtensionContext) => {
    ctx.ui.setFooter((tui, theme, footerData) => {
      const unsubs = [
        footerData.onBranchChange(() => tui.requestRender()),
        pi.on("message_end", () => tui.requestRender()),
        pi.on("turn_end", () => tui.requestRender()),
        pi.on("model_select", () => tui.requestRender()),
        pi.on("thinking_level_select", () => tui.requestRender()),
      ];
      let frame = 0;
      const timer = setInterval(() => {
        frame = (frame + 1) % CIRCLE_ANIM.length;
        tui.requestRender();
      }, 250);
      mounted = true;
      return {
        dispose() {
          clearInterval(timer);
          for (const u of unsubs) u();
          mounted = false;
        },
        invalidate() {},
        render(width: number): string[] {
          const cfg = loadConfig();
          if (!cfg.enabled) return [];
          const sep = theme.fg("borderMuted", ` ${SEPARATORS[cfg.separator] ?? "│"} `);
          const s = collectStats(ctx);
          const branch = cfg.lines.some((l) => l.includes("git")) ? gitBranch(ctx) : "";
          const out: string[] = [];
          for (const line of cfg.lines) {
            const parts = line
              .map((tag) => renderTag(tag, s, ctx, theme, branch, frame, cfg.cnyPerUsd))
              .filter((x): x is string => !!x);
            if (parts.length) out.push(truncateToWidth(` ${parts.join(sep)}`, width));
          }
          return out;
        },
      };
    });
  };

  pi.on("session_start", async (_event, ctx) => {
    try {
      mount(ctx);
    } catch {
      // 状态栏挂了也不能影响 pi 本身
    }
  });

  pi.registerCommand("footer", {
    description: "状态栏：开关 / 看当前配置（改配置去 aac 设置页「📊 状态栏编辑器」）",
    handler: async (args, ctx) => {
      const cmd = (args ?? "").trim();
      if (cmd === "off" || cmd === "on") {
        try {
          const raw = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
          raw.enabled = cmd === "on";
          fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
          fs.writeFileSync(CONFIG_PATH, JSON.stringify(raw, null, 2) + "\n", "utf8");
          cachedAt = 0; // 让下次渲染立刻读到新配置
          cmd === "on" && !mounted ? mount(ctx) : undefined;
          if (cmd === "off") ctx.ui.setFooter(undefined);
          ctx.ui.notify(`状态栏已${cmd === "on" ? "开启" : "关闭"}（已写进 ${CONFIG_PATH}）`, "info");
        } catch {
          ctx.ui.notify(`改配置失败：${CONFIG_PATH} 读不了`, "error");
        }
        return;
      }
      const cfg = loadConfig();
      const lines = cfg.lines.map((l) => l.join(` ${SEPARATORS[cfg.separator]} `)).join("\n");
      ctx.ui.notify(
        `状态栏 ${cfg.enabled ? "开启" : "关闭"}｜分隔符 ${SEPARATORS[cfg.separator]}\n${lines}\n` +
          `用法：/footer on、/footer off`,
        "info",
      );
    },
  });
}
