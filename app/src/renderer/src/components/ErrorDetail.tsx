/* 错误三级呈现（FR-13 / 07 章 §4，产品差异化卖点）：
 *   第一级「人话文案」——用户一眼知道发生了什么，不出现错误码；
 *   第二级「建议操作」——明确下一步该做什么；
 *   第三级「技术详情」——错误码/栈摘要/相关日志，默认折叠，只在反馈问题时展开。
 * 另含「阶段时间线」：把「卡在哪一步」变成可见事实（07 章用户流 B），
 * 因为非技术用户最常见的困惑不是「为什么失败」而是「它到底做到哪了」。
 *
 * 本文件只做展示，不发请求：日志、任务详情由调用页取好后传入，
 * 这样同一组件能同时服务工作台抽屉、文档库预览和日志页。
 */

import { useState } from "react";
import type { LogRow } from "../types";
import { errorText } from "../types";
import { copyText, fmtTime } from "../util";

// ---------------- 阶段时间线 ----------------

export type StepState = "done" | "active" | "failed" | "pending" | "skipped";

export interface StageStep {
  key: string;
  label: string;
  state: StepState;
  hint?: string;
}

/* 解析链的用户视角步骤。引擎 stage 枚举只有 convert/parse/ocr/chunk/export，
 * 这里额外单列「读取」：E01/E02（损坏/加密）确实发生在打开文件的那一刻，
 * 用户心智里它也是独立一步，合并进「转换」会让失败定位变模糊。 */
const PARSE_STEPS: { key: string; label: string }[] = [
  { key: "read", label: "读取" },
  { key: "convert", label: "转换" },
  { key: "parse", label: "版面分析" },
  { key: "ocr", label: "OCR" },
  { key: "chunk", label: "切片" },
];

/* 导出/数据集类任务没有版面分析与 OCR，时间线相应缩短 */
const EXPORT_STEPS: { key: string; label: string }[] = [
  { key: "read", label: "读取" },
  { key: "chunk", label: "切片" },
  { key: "export", label: "导出" },
];

export interface TimelineTaskLike {
  type: string;
  status: string;
  stage?: string | null;
  error_code?: string | null;
}

/* 由任务行推导时间线：以 tasks.stage 为游标，之前的算完成、之后的算未开始。
 * 引擎不会为每一步单独落库时间戳（那会让 tasks 表膨胀），这种推导足够支撑
 * 「标出失败步骤」的核心诉求，且任何缺字段的情况都能降级成合理展示。 */
export function buildStageSteps(task: TimelineTaskLike): StageStep[] {
  const defs = task.type === "export" || task.type === "dataset_build" ? EXPORT_STEPS : PARSE_STEPS;
  /* 排队中还没有任何步骤开始；已开始但引擎未上报 stage 时，至少「读取」已发生 */
  const cursorKey = task.stage || (task.status === "queued" ? null : "read");
  const found = cursorKey ? defs.findIndex((d) => d.key === cursorKey) : -1;
  const cursor = found >= 0 ? found : task.status === "queued" ? -1 : 0;

  return defs.map((d, i) => {
    let state: StepState;
    if (task.status === "done") {
      state = "done";
    } else if (task.status === "queued") {
      state = "pending";
    } else if (task.status === "failed") {
      state = i < cursor ? "done" : i === cursor ? "failed" : "skipped";
    } else if (task.status === "canceled" || task.status === "interrupted") {
      state = i < cursor ? "done" : "skipped";
    } else {
      state = i < cursor ? "done" : i === cursor ? "active" : "pending";
    }
    return { key: d.key, label: d.label, state };
  });
}

const STEP_MARK: Record<StepState, string> = {
  done: "✓",
  active: "…",
  failed: "!",
  pending: "",
  skipped: "–",
};

export function StageTimeline({ steps }: { steps: StageStep[] }) {
  return (
    <ol className="timeline" aria-label="阶段时间线">
      {steps.map((s, i) => (
        <li key={s.key} className={`timeline-step timeline-${s.state}`}>
          {i > 0 && <span className="timeline-line" aria-hidden="true" />}
          <span className="timeline-dot">{STEP_MARK[s.state]}</span>
          <span className="timeline-label" title={s.hint}>{s.label}</span>
        </li>
      ))}
    </ol>
  );
}

// ---------------- 错误三级呈现 ----------------

export interface ErrorDetailProps {
  /* 引擎错误码 E01~E07；为空时按「未知错误」兜底 */
  code: string | null | undefined;
  /* 技术详情：引擎返回的 detail / 栈摘要，只在折叠区出现 */
  detail?: string | null;
  taskId?: string | null;
  docId?: string | null;
  fileName?: string | null;
  /* 相关日志（07 章约定最多 50 行，由调用页裁好） */
  logs?: LogRow[];
  /* 「在日志中查看」——跳日志页并带上 task_id 过滤 */
  onViewInLogs?: () => void;
  /* 「重试」——由调用页决定重建何种任务 */
  onRetry?: () => void;
  /* 附加的阶段时间线（任务失败抽屉用） */
  steps?: StageStep[];
}

export function ErrorDetail({
  code,
  detail,
  taskId,
  docId,
  fileName,
  logs,
  onViewInLogs,
  onRetry,
  steps,
}: ErrorDetailProps) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const text = errorText(code);

  /* 诊断信息：一次性把定位问题需要的字段拼成纯文本，方便用户贴给支持人员。
   * 只含本机已有信息，不做任何上报（离线约束）。 */
  const diagnostics = (): string => {
    const lines = [
      `DocFactory 诊断信息`,
      `错误码：${code ?? "（无）"}`,
      `说明：${text.user_message}`,
      `建议：${text.suggestion}`,
      fileName ? `文件：${fileName}` : "",
      taskId ? `任务：${taskId}` : "",
      docId ? `文档：${docId}` : "",
      detail ? `详情：${detail}` : "",
    ].filter(Boolean);
    if (logs && logs.length) {
      lines.push("相关日志：");
      for (const l of logs.slice(0, 50)) {
        lines.push(`  [${l.level}] ${fmtTime(l.ts)} ${l.code ?? ""} ${l.message}`);
      }
    }
    return lines.join("\n");
  };

  return (
    <div className="errdetail">
      {steps && steps.length > 0 && <StageTimeline steps={steps} />}

      <div className="errdetail-head">
        <span className="errdetail-icon" aria-hidden="true">
          <svg viewBox="0 0 20 20" width="18" height="18">
            <circle cx="10" cy="10" r="8.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
            <path d="M10 5.6v5.2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            <circle cx="10" cy="14.1" r="1.05" fill="currentColor" />
          </svg>
        </span>
        <div className="errdetail-msg">{text.user_message}</div>
      </div>

      <div className="errdetail-sugg">
        <span className="errdetail-sugg-tag">建议</span>
        {text.suggestion}
      </div>

      <div className="errdetail-actions">
        {onRetry && (
          <button className="btn btn-sm btn-primary" onClick={onRetry}>
            重试
          </button>
        )}
        <button
          className="btn btn-sm"
          onClick={() => {
            void copyText(diagnostics()).then((ok) => {
              setCopied(ok);
              window.setTimeout(() => setCopied(false), 2000);
            });
          }}
        >
          {copied ? "已复制" : "复制诊断信息"}
        </button>
        {onViewInLogs && (
          <button className="btn btn-sm" onClick={onViewInLogs}>
            在日志中查看
          </button>
        )}
      </div>

      <button
        className="errdetail-toggle"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className={`caret ${open ? "caret-open" : ""}`} aria-hidden="true" />
        查看技术详情
      </button>

      {open && (
        <div className="errdetail-tech">
          <dl className="kv">
            <dt>错误码</dt>
            <dd>{code ?? "—"}</dd>
            {fileName && (
              <>
                <dt>文件</dt>
                <dd className="ellipsis" title={fileName}>{fileName}</dd>
              </>
            )}
            {taskId && (
              <>
                <dt>任务 ID</dt>
                <dd className="mono ellipsis" title={taskId}>{taskId}</dd>
              </>
            )}
            {docId && (
              <>
                <dt>文档 ID</dt>
                <dd className="mono ellipsis" title={docId}>{docId}</dd>
              </>
            )}
          </dl>
          {detail && <pre className="errdetail-stack">{detail}</pre>}
          {logs && logs.length > 0 && (
            <div className="errdetail-logs">
              <div className="errdetail-logs-title">相关日志（最近 {Math.min(logs.length, 50)} 条）</div>
              {logs.slice(0, 50).map((l) => (
                <div key={l.id} className={`errdetail-log log-${l.level}`}>
                  <span className="mono">{fmtTime(l.ts)}</span>
                  {l.code && <span className="errdetail-log-code">{l.code}</span>}
                  <span>{l.message}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
