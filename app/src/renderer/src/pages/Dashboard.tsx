/* 仪表盘（07 章 §3 全部指标）。
 *
 * 设计取舍：
 * - **不引第三方图表库**：依赖清单冻结，且这些图表都是静态展示型，
 *   手写 SVG/CSS 反而更可控（尺寸、配色、无障碍文案全部对齐设计令牌），
 *   也不用为 Recharts 之类多背 200KB 与一堆默认样式。
 * - **对后端字段极度宽容**：/stats/dashboard 的聚合形状可能是
 *   {key: count} 也可能是 [{key,count}]，这里统一归一化；缺字段就少画一块，
 *   绝不因为某个指标没算出来而白屏。
 * - 颜色与 styles.css 的设计令牌保持同值（SVG 的 fill 不能用 CSS 变量做渐变叠加，
 *   这里直接写十六进制，改动时两边同步）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useApp } from "../appctx";
import { EmptyState } from "../components/EmptyState";
import type { DashboardStats } from "../types";
import { fmtMs, fmtPct } from "../util";

const BRAND = "#2467E5";
const OK = "#1E9E5A";
const WARN = "#D98B12";
const ERR = "#D0453B";
const NEUTRAL = ["#2467E5", "#5B8DEF", "#8FB2F5", "#B9CDF9", "#95A3B8", "#C3CBD6"];

interface Pair {
  /* 业务键（E01 / L0 / pdf …）：跳转与取数用 */
  key: string;
  value: number;
  /* 服务端给的图例文案（「E01 文件似乎已损坏」）：展示优先用它 */
  label?: string;
}

/* 引擎的切片直方图按 token 分桶；面向非技术用户要换成字符口径（07 章：token 收进技术详情）。
 * 换算比例与导出中心/设置页一致（1 token ≈ 1.7 字符），取整到 50 便于阅读。 */
const CHARS_PER_TOKEN = 1.7;

function toChars(tokens: number): number {
  return Math.round((tokens * CHARS_PER_TOKEN) / 50) * 50;
}

function charBucketLabel(tokenLabel: string): string {
  const open = tokenLabel.match(/^(\d+)\+$/);
  if (open) return `${toChars(Number(open[1]))}+`;
  const range = tokenLabel.match(/^(\d+)\s*[-–~]\s*(\d+)$/);
  if (range) return `${toChars(Number(range[1]))}-${toChars(Number(range[2]))}`;
  return tokenLabel;
}

function asNum(v: unknown, dflt = 0): number {
  return typeof v === "number" && isFinite(v) ? v : dflt;
}

/* 把「对象计数表」与「对象数组」两种形状统一成 Pair[] */
function toPairs(v: unknown): Pair[] {
  if (!v) return [];
  if (Array.isArray(v)) {
    const out: Pair[] = [];
    for (const item of v) {
      if (!item || typeof item !== "object") continue;
      const r = item as Record<string, unknown>;
      let key = "";
      for (const k of ["key", "code", "fmt", "status", "level", "type", "bucket", "name", "label", "day"]) {
        if (typeof r[k] === "string") {
          key = r[k] as string;
          break;
        }
        if (typeof r[k] === "number") {
          key = String(r[k]);
          break;
        }
      }
      let value = 0;
      for (const k of ["count", "value", "n", "total", "cnt"]) {
        if (typeof r[k] === "number") {
          value = r[k] as number;
          break;
        }
      }
      if (key) out.push({ key, value, label: typeof r["label"] === "string" ? (r["label"] as string) : undefined });
    }
    return out;
  }
  if (typeof v === "object") {
    return Object.entries(v as Record<string, unknown>)
      .filter(([, val]) => typeof val === "number")
      .map(([key, val]) => ({ key, value: val as number }));
  }
  return [];
}

function pick(pairs: Pair[], ...keys: string[]): number {
  for (const k of keys) {
    const hit = pairs.find((p) => p.key.toLowerCase() === k.toLowerCase());
    if (hit) return hit.value;
  }
  return 0;
}

function pickCard(cards: Record<string, number | null> | undefined, ...keys: string[]): number | null {
  if (!cards) return null;
  for (const k of keys) {
    const v = cards[k];
    if (typeof v === "number" && isFinite(v)) return v;
  }
  return null;
}

// ---------------- 手写图表 ----------------

function Donut({ segments, centerValue, centerLabel }: {
  segments: { label: string; value: number; color: string }[];
  centerValue: string;
  centerLabel: string;
}) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  const r = 46;
  const c = 2 * Math.PI * r;
  let acc = 0;
  return (
    <div className="chart-donut">
      <svg viewBox="0 0 120 120" width="132" height="132" role="img" aria-label={centerLabel}>
        <circle cx="60" cy="60" r={r} fill="none" stroke="#EDF1F6" strokeWidth="14" />
        {total > 0 &&
          segments.map((s) => {
            const len = (s.value / total) * c;
            const el = (
              <circle
                key={s.label}
                cx="60"
                cy="60"
                r={r}
                fill="none"
                stroke={s.color}
                strokeWidth="14"
                strokeDasharray={`${len} ${c - len}`}
                strokeDashoffset={-acc}
                transform="rotate(-90 60 60)"
              />
            );
            acc += len;
            return el;
          })}
        <text x="60" y="56" textAnchor="middle" className="donut-value">{centerValue}</text>
        <text x="60" y="74" textAnchor="middle" className="donut-label">{centerLabel}</text>
      </svg>
      <ul className="legend">
        {segments.map((s) => (
          <li key={s.label}>
            <span className="legend-dot" style={{ background: s.color }} />
            {s.label}
            <b>{s.value}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* 饼图：小数据量，直接算扇形路径 */
function Pie({ items }: { items: Pair[] }) {
  const total = items.reduce((s, x) => s + x.value, 0);
  if (total <= 0) return <div className="chart-empty">暂无数据</div>;
  let angle = -Math.PI / 2;
  const paths = items.map((it, i) => {
    const sweep = (it.value / total) * Math.PI * 2;
    const x1 = 60 + 50 * Math.cos(angle);
    const y1 = 60 + 50 * Math.sin(angle);
    angle += sweep;
    const x2 = 60 + 50 * Math.cos(angle);
    const y2 = 60 + 50 * Math.sin(angle);
    const large = sweep > Math.PI ? 1 : 0;
    return (
      <path
        key={it.key}
        d={`M60 60 L${x1.toFixed(2)} ${y1.toFixed(2)} A50 50 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)} Z`}
        fill={NEUTRAL[i % NEUTRAL.length]}
      />
    );
  });
  return (
    <div className="chart-donut">
      <svg viewBox="0 0 120 120" width="122" height="122" role="img" aria-label="类型分布">
        {paths}
      </svg>
      <ul className="legend">
        {items.map((it, i) => (
          <li key={it.key}>
            <span className="legend-dot" style={{ background: NEUTRAL[i % NEUTRAL.length] }} />
            {it.label ?? it.key.toUpperCase()}
            <b>{it.value}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function StackedBar({ items }: { items: { label: string; value: number; color: string }[] }) {
  const total = items.reduce((s, x) => s + x.value, 0);
  if (total <= 0) return <div className="chart-empty">暂无数据</div>;
  return (
    <div className="chart-stack">
      <div className="stack-track">
        {items.map((it) =>
          it.value > 0 ? (
            <span
              key={it.label}
              className="stack-seg"
              style={{ width: `${(it.value / total) * 100}%`, background: it.color }}
              title={`${it.label}：${it.value}`}
            />
          ) : null,
        )}
      </div>
      <ul className="legend legend-row">
        {items.map((it) => (
          <li key={it.label}>
            <span className="legend-dot" style={{ background: it.color }} />
            {it.label}
            <b>{fmtPct(total ? it.value / total : 0)}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Histogram({ items }: { items: Pair[] }) {
  if (!items.length) return <div className="chart-empty">暂无数据</div>;
  const max = Math.max(...items.map((x) => x.value), 1);
  const w = 300;
  const h = 96;
  const bw = w / items.length;
  return (
    <div className="chart-hist">
      <svg viewBox={`0 0 ${w} ${h + 18}`} width="100%" height={h + 18} role="img" aria-label="切片长度分布">
        {items.map((it, i) => {
          const bh = Math.max(2, (it.value / max) * h);
          const chars = charBucketLabel(it.key);
          return (
            <g key={it.key}>
              <rect
                x={i * bw + 2}
                y={h - bh}
                width={Math.max(2, bw - 4)}
                height={bh}
                rx="2"
                fill={BRAND}
                opacity={0.85}
              >
                {/* 悬停提示里保留 token 口径，方便技术用户对齐引擎参数 */}
                <title>{`约 ${chars} 字符（${it.key} tokens）：${it.value} 个切片`}</title>
              </rect>
              {items.length <= 12 && (
                <text x={i * bw + bw / 2} y={h + 13} textAnchor="middle" className="axis-text">
                  {chars}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function TrendLine({ series, labels }: {
  series: { name: string; color: string; points: number[] }[];
  labels: string[];
}) {
  const usable = series.filter((s) => s.points.length > 1);
  if (!usable.length) return <div className="chart-empty">暂无趋势数据</div>;
  const w = 320;
  const h = 96;
  const max = Math.max(1, ...usable.flatMap((s) => s.points));
  const n = Math.max(...usable.map((s) => s.points.length));
  const xy = (i: number, v: number) => [
    (i / Math.max(1, n - 1)) * (w - 8) + 4,
    h - (v / max) * (h - 12) - 4,
  ];
  return (
    <div className="chart-trend">
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label="趋势">
        <line x1="0" y1={h - 4} x2={w} y2={h - 4} stroke="#E6EAF0" />
        {usable.map((s) => (
          <polyline
            key={s.name}
            fill="none"
            stroke={s.color}
            strokeWidth="1.8"
            strokeLinejoin="round"
            strokeLinecap="round"
            points={s.points.map((v, i) => xy(i, v).map((z) => z.toFixed(1)).join(",")).join(" ")}
          />
        ))}
      </svg>
      <div className="trend-foot">
        <ul className="legend legend-row">
          {usable.map((s) => (
            <li key={s.name}>
              <span className="legend-dot" style={{ background: s.color }} />
              {s.name}
            </li>
          ))}
        </ul>
        {labels.length > 0 && (
          <span className="hint-dim">{labels[0]} → {labels[labels.length - 1]}</span>
        )}
      </div>
    </div>
  );
}

function BarList({ items, onPick, valueFmt }: {
  items: Pair[];
  onPick?: (key: string) => void;
  valueFmt?: (v: number) => string;
}) {
  if (!items.length) return <div className="chart-empty">暂无数据</div>;
  const max = Math.max(...items.map((x) => x.value), 1);
  return (
    <ul className="barlist">
      {items.map((it) => (
        <li key={it.key}>
          <button
            className="barlist-row"
            onClick={() => onPick?.(it.key)}
            disabled={!onPick}
            title={onPick ? "在日志中查看该原因" : it.label ?? it.key}
          >
            <span className="barlist-label ellipsis" title={it.label ?? it.key}>{it.label ?? it.key}</span>
            <span className="barlist-track">
              <span className="barlist-fill" style={{ width: `${(it.value / max) * 100}%` }} />
            </span>
            <span className="barlist-value">{valueFmt ? valueFmt(it.value) : it.value}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function Card({ title, value, sub, tone }: { title: string; value: string; sub?: string; tone?: "ok" | "warn" | "err" }) {
  return (
    <div className="stat-card">
      <div className="stat-title">{title}</div>
      <div className={`stat-value ${tone ? `stat-${tone}` : ""}`}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

// ---------------- 页面 ----------------

export function Dashboard() {
  const { client, page, navigate } = useApp();
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    try {
      setStats(await client.getJson<DashboardStats>("/stats/dashboard"));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [client]);

  /* 仪表盘是概览页：只在可见时取数并按 30s 刷新，切走后完全静默 */
  useEffect(() => {
    if (page !== "dashboard") return;
    void load();
    const timer = window.setInterval(() => void load(), 30000);
    return () => window.clearInterval(timer);
  }, [load, page]);

  const view = useMemo(() => {
    const statusPairs = toPairs(stats?.status_dist);
    const levelPairs = toPairs(stats?.level_dist);
    const fmtPairs = toPairs(stats?.fmt_dist);
    const histPairs = toPairs(stats?.chunk_hist);
    const failPairs = toPairs(stats?.fail_top).sort((a, b) => b.value - a.value).slice(0, 5);
    const durPairs = toPairs(stats?.duration);

    const ok = pick(statusPairs, "ok", "success");
    const warn = pick(statusPairs, "warning", "warn");
    const fail = pick(statusPairs, "failed", "fail", "error");
    const parsed = ok + warn + fail;

    const trendRows = Array.isArray(stats?.trend) ? (stats?.trend as unknown[]) : [];
    const days: string[] = [];
    const imported: number[] = [];
    const parsedOk: number[] = [];
    const coverage: number[] = [];
    for (const row of trendRows) {
      if (!row || typeof row !== "object") continue;
      const r = row as Record<string, unknown>;
      days.push(typeof r["day"] === "string" ? (r["day"] as string) : "");
      /* docs_created 是实时统计，比 metrics_daily 的 imported 更不容易漏记 */
      imported.push(Math.max(asNum(r["imported"]), asNum(r["docs_created"])));
      parsedOk.push(asNum(r["parsed_ok"]));
      const cov = r["text_coverage"] ?? r["avg_coverage"] ?? r["coverage"];
      if (typeof cov === "number") coverage.push(cov <= 1 ? cov * 100 : cov);
    }

    return { statusPairs, levelPairs, fmtPairs, histPairs, failPairs, durPairs, ok, warn, fail, parsed, days, imported, parsedOk, coverage };
  }, [stats]);

  const cards = stats?.cards;
  const importedTotal = pickCard(cards, "imported", "documents", "doc_total", "total_docs") ?? view.fmtPairs.reduce((s, x) => s + x.value, 0);
  const avgCoverage = pickCard(cards, "avg_text_coverage", "text_coverage_avg", "avg_coverage");
  const chunkTotal = pickCard(cards, "chunk_cnt", "chunks", "chunk_total") ?? view.histPairs.reduce((s, x) => s + x.value, 0);
  const ocrRatio = pickCard(cards, "ocr_ratio", "ocr_pages_ratio");
  /* 成功率口径以引擎为准：E04 是警告不是失败，warning 计入成功（07 章 §4） */
  const successRate =
    pickCard(cards, "success_rate") ?? (view.parsed ? (view.ok + view.warn) / view.parsed : null);
  const ocrPages = pickCard(cards, "ocr_pages");
  const imported7d = pickCard(cards, "imported_7d", "imported_last7");

  const hasAnything =
    view.parsed > 0 || importedTotal > 0 || view.fmtPairs.length > 0 || view.histPairs.length > 0;

  if (!stats && failed) {
    return (
      <div className="page page-dashboard">
        <EmptyState title="暂时读不到统计数据" hint="本地引擎可能还在启动，稍后会自动重试">
          <button className="btn btn-primary" onClick={() => void load()}>立即重试</button>
        </EmptyState>
      </div>
    );
  }

  if (!hasAnything) {
    return (
      <div className="page page-dashboard">
        <EmptyState title="还没有可统计的数据" hint="导入并解析一些文件后，这里会显示成功率、解析级别与切片分布">
          <button className="btn btn-primary" onClick={() => navigate("workbench")}>去工作台导入</button>
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="page page-dashboard">
      <div className="dash-cards">
        <Card title="导入文件数" value={String(importedTotal)} sub={imported7d !== null ? `近 7 日 ${imported7d}` : undefined} />
        <Card
          title="解析成功率"
          value={successRate !== null ? fmtPct(successRate) : "—"}
          sub={`成功 ${view.ok} · 警告 ${view.warn} · 失败 ${view.fail}`}
          tone={successRate !== null && successRate >= 0.9 ? "ok" : view.fail > 0 ? "warn" : undefined}
        />
        <Card
          title="平均文本覆盖率"
          value={avgCoverage !== null ? fmtPct(avgCoverage, 1) : "—"}
          sub="解析出的文字占原文的比例"
        />
        <Card title="切片总数" value={String(chunkTotal)} sub="可直接用于检索的语义块" />
        <Card
          title="OCR 触发占比"
          value={ocrRatio !== null ? fmtPct(ocrRatio) : ocrPages !== null ? `${ocrPages} 页` : "—"}
          sub="需要图片识别的页面比例"
        />
      </div>

      <div className="dash-grid">
        <section className="panel">
          <h3 className="panel-title">解析结果分布</h3>
          <Donut
            segments={[
              { label: "成功", value: view.ok, color: OK },
              { label: "警告", value: view.warn, color: WARN },
              { label: "失败", value: view.fail, color: ERR },
            ]}
            centerValue={successRate !== null ? fmtPct(successRate) : "—"}
            centerLabel="成功率"
          />
        </section>

        <section className="panel">
          <h3 className="panel-title">解析分级占比</h3>
          <StackedBar
            items={[
              { label: "L0 深度解析", value: pick(view.levelPairs, "L0"), color: BRAND },
              { label: "L1 基础解析", value: pick(view.levelPairs, "L1"), color: WARN },
              { label: "L2 兜底提取", value: pick(view.levelPairs, "L2"), color: ERR },
            ]}
          />
          <p className="panel-note">L0 越多说明版面还原越完整；L2 只保底提取文字。</p>
        </section>

        <section className="panel">
          <h3 className="panel-title">文件类型分布</h3>
          <Pie items={view.fmtPairs} />
        </section>

        <section className="panel">
          <h3 className="panel-title">切片长度分布</h3>
          <Histogram items={view.histPairs} />
          <p className="panel-note">横轴为切片长度区间（约合字符数），纵轴为切片数量。</p>
        </section>

        <section className="panel panel-wide">
          <h3 className="panel-title">近期趋势</h3>
          <TrendLine
            series={[
              { name: "导入", color: BRAND, points: view.imported },
              { name: "解析成功", color: OK, points: view.parsedOk },
            ]}
            labels={view.days}
          />
          {view.coverage.length > 1 && (
            <>
              <div className="panel-subtitle">平均文本覆盖率（%）</div>
              <TrendLine series={[{ name: "覆盖率", color: WARN, points: view.coverage }]} labels={view.days} />
            </>
          )}
        </section>

        <section className="panel">
          <h3 className="panel-title">失败原因 TOP5</h3>
          <BarList items={view.failPairs} onPick={(code) => navigate("logs", { code, level: "error" })} />
          <p className="panel-note">点击任意一项可在日志中查看相关记录。</p>
        </section>

        <section className="panel">
          <h3 className="panel-title">处理耗时</h3>
          <BarList items={view.durPairs} valueFmt={(v) => fmtMs(v)} />
          <p className="panel-note">按任务类型统计的平均耗时。</p>
        </section>
      </div>

      <div className="dash-foot">
        <button className="btn btn-sm" onClick={() => void load()}>刷新数据</button>
        {failed && <span className="hint-dim">上次刷新失败，显示的是此前的数据</span>}
      </div>
    </div>
  );
}
