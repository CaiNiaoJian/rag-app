/* 与引擎冻结契约对齐的前端类型与文案映射。
 * 数据来源：02 章 SQLite schema、engine/src/docfactory/{config,errors,ir,taskspec}.py。
 * 约束：此处的枚举值与错误码注册表必须与引擎保持一致，禁止私自扩展。
 */

// ---------------- 基础枚举（与 schema 冻结值一致） ----------------

export type DocStatus = "imported" | "parsing" | "ok" | "warning" | "failed";
export type TaskStatus = "queued" | "running" | "done" | "failed" | "canceled" | "interrupted";
export type TaskType = "parse" | "export" | "rechunk" | "module_install" | "qa_generate" | "dataset_build";
export type Stage = "convert" | "parse" | "ocr" | "chunk" | "export";
export type ParseLevel = "L0" | "L1" | "L2";

// ---------------- 引擎表行结构 ----------------

export interface DocumentRow {
  id: string;
  name: string;
  src_path: string;
  fmt: string;
  size: number | null;
  hash: string | null;
  status: DocStatus;
  page_cnt: number | null;
  parse_level: ParseLevel | null;
  text_coverage: number | null;
  table_confidence: number | null;
  ocr_confidence: number | null;
  degraded_pages: number | null;
  ir_version: string | null;
  created_at: string;
  parsed_at: string | null;
}

export interface TaskRow {
  id: string;
  doc_id: string | null;
  type: TaskType;
  status: TaskStatus;
  progress: number | null;
  stage: Stage | null;
  error_code: string | null;
  payload_json: string | null;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  /* 部分端点可能附带的扩展字段（任务详情/结果摘要），按需读取 */
  result?: Record<string, unknown> | null;
  result_json?: string | null;
}

export interface LogRow {
  id: number;
  task_id: string | null;
  doc_id: string | null;
  level: "info" | "warning" | "error";
  code: string | null;
  stage: string | null;
  page: number | null;
  message: string;
  detail_json: string | null;
  ts: string;
}

export interface ModuleRow {
  id: string;
  name: string;
  type: string; // parser|ocr|converter|llm-runtime|llm-model
  version: string;
  enabled: number;
  prev_version: string | null;
  manifest_json: string | null;
  installed_at: string | null;
}

export interface ChunkRow {
  id: string;
  doc_id: string;
  seq: number;
  parent_id: string | null;
  kind: "child" | "parent";
  type: string;
  text: string;
  token_count: number;
  char_count: number;
  heading_path: string | null;
  pages: string | null;
  node_ids: string | null;
  meta_json: string | null;
  hash: string | null;
}

export interface Paged<T> {
  items: T[];
  total: number;
}

export interface HealthInfo {
  status: string;
  engine_version: string;
  api_version: string;
}

// ---------------- 用户设置（config.py Settings 镜像） ----------------

export interface ChunkSettings {
  target_tokens: number;
  max_tokens: number;
  overlap: number; // 0~1 比例，UI 展示为百分比
  split_by_heading: boolean;
  table_atomic: boolean;
  drop_header_footer: boolean;
  footnote_to_end: boolean;
}

export interface PdfExportSettings {
  font_size: number;
  header_footer: boolean;
}

export interface DatasetSettings {
  format: "alpaca" | "sharegpt";
  file_format: "json" | "csv";
  mode: "blank" | "rule";
  per_chunk: number;
}

export interface EngineSettings {
  ocr_mode: "on" | "off" | "high";
  degrade_policy: "auto" | "strict";
  page_timeout_s: number;
  parallel_tasks: number;
  output_dir: string | null;
  chunk: ChunkSettings;
  pdf_export: PdfExportSettings;
  dataset: DatasetSettings;
}

// ---------------- IR v1.0（ir.py 镜像，渲染进程只读消费） ----------------

export interface IRProv {
  page: number;
  bbox?: number[] | null;
  charspan?: number[] | null;
}

export interface IRTableCell {
  r: number;
  c: number;
  rowspan?: number;
  colspan?: number;
  text?: string;
  is_header?: boolean;
}

export interface IRNodeContent {
  text?: string | null;
  table?: { cells?: IRTableCell[] } | null;
  image_ref?: string | null;
  caption?: string | null;
  ocr_text?: string | null;
  ocr?: boolean | null;
  title?: string | null;
  notes?: string | null;
  name?: string | null;
  range?: string | null;
  ordered?: boolean | null;
}

export interface IRNode {
  id: string;
  type: string;
  parent?: string | null;
  children?: string[];
  level?: number | null;
  content?: IRNodeContent;
  prov?: IRProv[];
  confidence?: number;
}

export interface IRDocument {
  ir_version: string;
  doc: {
    id: string;
    source_file: string;
    source_format: string;
    convert_chain?: string[];
    parse_level?: ParseLevel;
    engine_version?: string;
    metrics?: {
      text_coverage?: number | null;
      table_confidence?: number | null;
      ocr_confidence?: number | null;
      disordered_pages?: number;
      degraded_pages?: number;
    };
  };
  nodes: IRNode[];
}

// ---------------- 仪表盘聚合（GET /stats/dashboard，字段缺失时 UI 占位） ----------------

export interface DashboardStats {
  cards?: Record<string, number | null>;
  fmt_dist?: unknown;
  status_dist?: unknown;
  level_dist?: unknown;
  chunk_hist?: unknown;
  fail_top?: unknown;
  duration?: unknown;
  trend?: unknown;
}

// ---------------- 错误码注册表镜像（errors.py，07 章 §4 冻结） ----------------

export interface ErrorText {
  user_message: string;
  suggestion: string;
}

export const ERROR_TEXT: Record<string, ErrorText> = {
  E01: { user_message: "文件似乎已损坏，无法读取", suggestion: "用原程序打开确认后重新导入" },
  E02: { user_message: "文件受密码保护", suggestion: "先在原程序中解除密码再导入" },
  E03: { user_message: "暂不支持此文件格式（或旧格式转换失败）", suggestion: "查看支持格式清单；旧格式可另存为新格式后导入" },
  E04: { user_message: "扫描质量较低，部分文字可能识别不准", suggestion: "预览中检查黄色高亮区域；可尝试高精度 OCR 模组" },
  E05: { user_message: "未能提取到有效内容（或超大表已截断）", suggestion: "确认文件内容；超大表建议拆分" },
  E06: { user_message: "处理超时或引擎异常，已自动重试", suggestion: "重试；反复出现请导出诊断包反馈" },
  E07: { user_message: "磁盘空间不足，无法继续", suggestion: "清理磁盘或在设置中迁移数据目录" },
};

export function errorText(code: string | null | undefined): ErrorText {
  if (code && ERROR_TEXT[code]) return ERROR_TEXT[code];
  return { user_message: "发生未知错误", suggestion: "重试；反复出现请导出诊断包反馈" };
}

// ---------------- 文案映射（全部简体中文，面向非技术用户） ----------------

export const STAGE_LABEL: Record<string, string> = {
  convert: "转换",
  parse: "解析",
  ocr: "OCR",
  chunk: "切片",
  export: "导出",
};

export const DOC_STATUS_LABEL: Record<DocStatus, string> = {
  imported: "已导入",
  parsing: "解析中",
  ok: "成功",
  warning: "警告",
  failed: "失败",
};

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  queued: "排队",
  running: "进行中",
  done: "成功",
  failed: "失败",
  canceled: "已取消",
  interrupted: "中断",
};

export const TASK_TYPE_LABEL: Record<TaskType, string> = {
  parse: "解析",
  export: "导出",
  rechunk: "重切",
  module_install: "模组安装",
  qa_generate: "问答生成",
  dataset_build: "数据集构建",
};

export const MODULE_TYPE_LABEL: Record<string, string> = {
  parser: "解析器",
  ocr: "OCR 模型",
  converter: "格式转换",
  "llm-runtime": "模型运行时",
  "llm-model": "本地模型",
};

/* 支持导入的七种格式（01 章需求范围） */
export const SUPPORTED_EXTS = ["doc", "docx", "pdf", "ppt", "pptx", "xls", "xlsx"] as const;

/* SSE 事件名（taskspec.py 冻结） */
export const SSE_EVENTS = {
  progress: "progress",
  stageChange: "stage_change",
  degrade: "degrade",
  done: "done",
  failed: "failed",
} as const;
