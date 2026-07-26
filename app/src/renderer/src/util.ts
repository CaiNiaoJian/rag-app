/* 纯函数工具集：时间/字节/路径格式化与剪贴板。无任何网络与状态。 */

export function basename(p: string): string {
  const m = p.replace(/[\\/]+$/, "").match(/[^\\/]+$/);
  return m ? m[0] : p;
}

export function dirname(p: string): string {
  return p.replace(/[\\/][^\\/]*$/, "");
}

export function extOf(name: string): string {
  const m = name.toLowerCase().match(/\.([a-z0-9]+)$/);
  return m ? m[1] : "";
}

/* 目录拼接：保持与原路径一致的分隔符风格（Windows 反斜杠） */
export function joinPath(dir: string, rel: string): string {
  const sep = dir.includes("\\") ? "\\" : "/";
  const cleanRel = rel.replace(/^\.\//, "").replace(/[/]/g, sep);
  return dir.replace(/[\\/]+$/, "") + sep + cleanRel;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/* ISO 时间 → "MM-DD HH:mm"（本年内省略年份） */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (x: number) => String(x).padStart(2, "0");
  const base = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.getFullYear() === new Date().getFullYear() ? base : `${d.getFullYear()}-${base}`;
}

/* 任务耗时：起止时间差；未结束用当前时间 */
export function fmtDuration(startIso: string | null | undefined, endIso?: string | null): string {
  if (!startIso) return "—";
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  if (isNaN(start) || isNaN(end) || end < start) return "—";
  const ms = end - start;
  if (ms < 1000) return "<1s";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms == null || !isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

/* 0~1 比例 → 百分数文本；已是百分数（>1）时原样取整 */
export function fmtPct(v: number | null | undefined, digits = 0): string {
  if (v == null || !isFinite(v)) return "—";
  const pct = v <= 1 ? v * 100 : v;
  return `${pct.toFixed(digits)}%`;
}

/* 进度归一：兼容 0~1 与 0~100 两种写法，输出 0~100 */
export function normProgress(v: number | null | undefined): number {
  if (v == null || !isFinite(v)) return 0;
  const p = v <= 1 ? v * 100 : v;
  return Math.max(0, Math.min(100, p));
}

export function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

export function parseJsonSafe<T>(text: string | null | undefined, fallback: T): T {
  if (!text) return fallback;
  try {
    return JSON.parse(text) as T;
  } catch {
    return fallback;
  }
}

/* 复制文本：优先 Clipboard API，非安全上下文回退隐藏 textarea */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}
