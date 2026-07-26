/* 状态徽章：颜色克制（绿/黄/红仅用于状态），其余中性灰与品牌蓝。 */

import type { DocStatus, ParseLevel, TaskStatus } from "../types";
import { DOC_STATUS_LABEL, TASK_STATUS_LABEL } from "../types";

export type BadgeKind = "neutral" | "brand" | "ok" | "warn" | "err" | "run";

export function Badge({ kind = "neutral", text }: { kind?: BadgeKind; text: string }) {
  return <span className={`badge badge-${kind}`}>{text}</span>;
}

const TASK_KIND: Record<TaskStatus, BadgeKind> = {
  queued: "neutral",
  running: "run",
  done: "ok",
  failed: "err",
  canceled: "neutral",
  interrupted: "warn",
};

/* 任务状态徽章；解析完成但文档是警告态时展示「警告」（07 章用户流 A） */
export function TaskStatusBadge({ status, docWarning, runningLabel }: {
  status: TaskStatus;
  docWarning?: boolean;
  runningLabel?: string;
}) {
  if (status === "done" && docWarning) return <Badge kind="warn" text="警告" />;
  const text = status === "running" && runningLabel ? runningLabel : TASK_STATUS_LABEL[status] ?? status;
  return <Badge kind={TASK_KIND[status] ?? "neutral"} text={text} />;
}

const DOC_KIND: Record<DocStatus, BadgeKind> = {
  imported: "neutral",
  parsing: "run",
  ok: "ok",
  warning: "warn",
  failed: "err",
};

export function DocStatusBadge({ status }: { status: DocStatus }) {
  return <Badge kind={DOC_KIND[status] ?? "neutral"} text={DOC_STATUS_LABEL[status] ?? status} />;
}

/* 解析分级徽章：L0 深度解析（品牌蓝）、L1 基础（黄）、L2 兜底（红） */
export function LevelBadge({ level }: { level: ParseLevel | null | undefined }) {
  if (!level) return <span className="text-dim">—</span>;
  const kind: BadgeKind = level === "L0" ? "brand" : level === "L1" ? "warn" : "err";
  return <Badge kind={kind} text={level} />;
}

/* 文件类型小图标：CSS 徽标（无图片资源，避免外部依赖） */
export function FmtIcon({ ext }: { ext: string }) {
  const e = ext.toLowerCase().replace(/^\./, "");
  const cls =
    e === "pdf" ? "fmt-pdf"
    : e === "doc" || e === "docx" ? "fmt-doc"
    : e === "ppt" || e === "pptx" ? "fmt-ppt"
    : e === "xls" || e === "xlsx" ? "fmt-xls"
    : e === "kmod" ? "fmt-kmod"
    : "fmt-other";
  return <span className={`fmt-icon ${cls}`}>{e ? e.slice(0, 4) : "?"}</span>;
}
