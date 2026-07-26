/* 导出中心（07 章 §1 第 3 页 / 05 章）：左范围 → 中格式 → 右参数的三栏漏斗。
 *
 * 设计取舍：
 * - **切片参数改动 → 先重切再导出**：切片是导出的上游产物，直接拿旧切片导出会让
 *   用户以为参数没生效。这里改过参数就先给每个选中文档派 rechunk 任务，
 *   等它们结束再派 export，全过程只需用户点一次「开始导出」。
 * - **面向非技术用户的单位**：切片长度用「字符数」呈现，token 数只出现在技术详情里
 *   （引擎内核仍按 token 计，换算比例见 CHARS_PER_TOKEN 注释）。
 * - **PDF 的最后一公里在 Electron**：引擎只产出打印用 HTML（05 章），
 *   真正的 PDF 由主进程 printToPDF 渲染，所以导出任务完成后这里还要补一步。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../appctx";
import { DocStatusBadge, FmtIcon } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { ErrorDetail } from "../components/ErrorDetail";
import { ProgressBar } from "../components/ProgressBar";
import type { DocumentRow, EngineSettings, TaskRow } from "../types";
import { basename, dirname, fmtTime, normProgress } from "../util";

/* 中英混排语料的经验值：1 token ≈ 1.7 字符。仅用于 UI 展示与反算，
 * 引擎侧一切计算仍以 token 为准（04 章 §3.2），所以这里的取整误差无害。 */
const CHARS_PER_TOKEN = 1.7;

const FORMATS: { key: string; name: string; desc: string }[] = [
  { key: "md", name: "Markdown", desc: "结构完整还原，含图片资源目录，最通用" },
  { key: "json", name: "切片 JSON", desc: "带元数据的切片数组，喂给检索库最省事" },
  { key: "csv", name: "切片 CSV", desc: "扁平表格，Excel 可直接打开（UTF-8 BOM）" },
  { key: "alpaca", name: "Alpaca 数据集", desc: "微调格式，默认留空模板供人工标注" },
  { key: "sharegpt", name: "ShareGPT 数据集", desc: "对话式微调格式" },
  { key: "pdf", name: "PDF", desc: "保留逻辑结构的排版稿，非原版面复刻" },
];

interface ExportResult {
  out_dir?: string;
  files?: unknown;
  /* 引擎产出的打印用 HTML → 由本进程渲染成 PDF */
  pdf_html?: unknown;
  /* 部分文档/格式失败（整任务仍算完成）：{doc_id, format, error_code, message} */
  failed?: unknown;
}

interface TaskDetail extends TaskRow {
  result?: Record<string, unknown> | null;
}

function unwrapItems<T>(resp: unknown): T[] {
  if (Array.isArray(resp)) return resp as T[];
  if (resp && typeof resp === "object") {
    const r = resp as Record<string, unknown>;
    for (const key of ["items", "documents", "results"]) {
      if (Array.isArray(r[key])) return r[key] as T[];
    }
  }
  return [];
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

export function ExportCenter() {
  const { client, nav, navigate, toast } = useApp();

  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [picked, setPicked] = useState<Set<string>>(() => new Set());
  const [onlyOk, setOnlyOk] = useState(true);
  const [q, setQ] = useState("");

  const [formats, setFormats] = useState<Set<string>>(() => new Set(["md"]));
  const [settings, setSettings] = useState<EngineSettings | null>(null);
  const [chunkDirty, setChunkDirty] = useState(false);
  const [merge, setMerge] = useState(false);
  const [outDir, setOutDir] = useState<string>("");

  const [running, setRunning] = useState<TaskDetail | null>(null);
  const [doneInfo, setDoneInfo] = useState<{ dir: string | null; count: number } | null>(null);
  const [failInfo, setFailInfo] = useState<{ code: string | null; detail: string; taskId: string } | null>(null);
  const pollRef = useRef<number | null>(null);

  // ---------------- 初始数据 ----------------

  const loadDocs = useCallback(async () => {
    try {
      const resp = await client.getJson<unknown>("/documents?page=1");
      setDocs(unwrapItems<DocumentRow>(resp));
    } catch {
      setDocs([]);
    }
  }, [client]);

  useEffect(() => {
    void loadDocs();
    void client
      .getJson<EngineSettings>("/settings")
      .then((s) => {
        setSettings(s);
        setOutDir(s.output_dir ?? "");
      })
      .catch(() => undefined);
  }, [client, loadDocs]);

  /* 文档库「导出」按钮跳过来时带 doc_id：直接勾上并刷新一次列表 */
  useEffect(() => {
    if (nav.page !== "export") return;
    const docId = nav.params["doc_id"];
    if (!docId) return;
    setPicked(new Set([docId]));
    void loadDocs();
  }, [nav, loadDocs]);

  const visible = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return docs.filter((d) => {
      if (onlyOk && d.status !== "ok") return false;
      if (kw && !d.name.toLowerCase().includes(kw)) return false;
      return true;
    });
  }, [docs, onlyOk, q]);

  const toggleDoc = (id: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleFormat = (key: string) => {
    setFormats((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // ---------------- 参数编辑（只改本地副本，导出时下发） ----------------

  const patchChunk = (patch: Partial<EngineSettings["chunk"]>) => {
    setSettings((prev) => (prev ? { ...prev, chunk: { ...prev.chunk, ...patch } } : prev));
    setChunkDirty(true);
  };
  const patchPdf = (patch: Partial<EngineSettings["pdf_export"]>) => {
    setSettings((prev) => (prev ? { ...prev, pdf_export: { ...prev.pdf_export, ...patch } } : prev));
  };
  const patchDataset = (patch: Partial<EngineSettings["dataset"]>) => {
    setSettings((prev) => (prev ? { ...prev, dataset: { ...prev.dataset, ...patch } } : prev));
  };

  // ---------------- 导出流程 ----------------

  const waitTask = useCallback(
    async (taskId: string): Promise<TaskDetail> =>
      new Promise((resolve) => {
        const tick = async () => {
          try {
            const t = await client.getJson<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}`);
            setRunning(t);
            if (t.status === "done" || t.status === "failed" || t.status === "canceled" || t.status === "interrupted") {
              resolve(t);
              return;
            }
          } catch {
            /* 引擎短暂不可达：继续轮询，任务本身不受影响 */
          }
          pollRef.current = window.setTimeout(() => void tick(), 1500);
        };
        void tick();
      }),
    [client],
  );

  useEffect(
    () => () => {
      if (pollRef.current !== null) window.clearTimeout(pollRef.current);
    },
    [],
  );

  /* 导出产物清单只出现在 SSE 的 done 事件里——tasks 表没有结果列（02 章 §2.2：
   * 大结果一律落盘，HTTP 只回摘要）。所以这里必须订阅进度流，轮询只判终态。 */
  const runTaskWithResult = useCallback(
    async (taskId: string): Promise<{ task: TaskDetail; result: ExportResult }> => {
      const box: { result: ExportResult | null } = { result: null };
      const off = client.sse(taskId, {
        progress: (d) => {
          const page = typeof d["page"] === "number" ? d["page"] : null;
          const total = typeof d["total"] === "number" ? d["total"] : null;
          if (page === null || !total) return;
          setRunning((prev) => (prev ? { ...prev, progress: Math.min(100, (page / total) * 100) } : prev));
        },
        done: (d) => {
          const r = d["result"];
          if (r && typeof r === "object") box.result = r as ExportResult;
        },
      });
      try {
        const task = await waitTask(taskId);
        /* 引擎先落库再发 done，两者之间有毫秒级竞态：轮询可能先看到终态，
         * 这里给进度流最多 1.5s 把 result 送达，超时就走兜底路径 */
        for (let i = 0; i < 15 && box.result === null && task.status === "done"; i += 1) {
          await new Promise((r) => window.setTimeout(r, 100));
        }
        return { task, result: box.result ?? ((task.result ?? {}) as ExportResult) };
      } finally {
        off();
      }
    },
    [client, waitTask],
  );

  /* 引擎产出打印 HTML 后，由 Electron 主进程离屏渲染成 PDF（05 章 §1） */
  const renderPdfs = async (result: ExportResult): Promise<number> => {
    const jobs: { html: string; pdf: string }[] = [];
    if (Array.isArray(result.pdf_html)) {
      for (const item of result.pdf_html) {
        if (typeof item === "string") jobs.push({ html: item, pdf: item.replace(/\.html?$/i, ".pdf") });
        else if (item && typeof item === "object") {
          const o = item as Record<string, unknown>;
          if (typeof o["html"] === "string") {
            const html = o["html"];
            const pdf = typeof o["pdf"] === "string" ? o["pdf"] : html.replace(/\.html?$/i, ".pdf");
            jobs.push({ html, pdf });
          }
        }
      }
    } else {
      for (const f of strList(result.files)) {
        if (/\.html?$/i.test(f)) jobs.push({ html: f, pdf: f.replace(/\.html?$/i, ".pdf") });
      }
    }
    let ok = 0;
    for (const j of jobs) {
      try {
        await window.df.pdf.printHtmlToPdf(j.html, j.pdf);
        ok += 1;
      } catch {
        toast(`PDF 渲染失败：${basename(j.pdf)}`, "err");
      }
    }
    return ok;
  };

  /* 结果里没带目录时的兜底：用户指定过就用它，否则问文档详情要 exports 目录 */
  const fallbackDir = async (docIds: string[]): Promise<string | null> => {
    if (outDir) return outDir;
    const first = docIds[0];
    if (!first) return null;
    try {
      const d = await client.getJson<{ exports_dir?: string }>(`/documents/${encodeURIComponent(first)}`);
      return d.exports_dir ?? null;
    } catch {
      return null;
    }
  };

  const startExport = async () => {
    const docIds = Array.from(picked);
    if (!docIds.length || !formats.size || !settings) return;
    setDoneInfo(null);
    setFailInfo(null);
    try {
      /* 切片参数被改过 → 先重切，保证导出用的是新参数下的切片 */
      if (chunkDirty) {
        for (const id of docIds) {
          const r = await client.postJson<{ task_id: string }>("/tasks", {
            type: "rechunk",
            payload: { doc_id: id, chunk: settings.chunk },
          });
          const t = await waitTask(r.task_id);
          if (t.status !== "done") {
            setFailInfo({ code: t.error_code, detail: "重新切片未能完成，导出已中止。", taskId: t.id });
            setRunning(null);
            return;
          }
        }
        setChunkDirty(false);
      }

      const resp = await client.postJson<{ task_id: string }>("/tasks", {
        type: "export",
        payload: {
          doc_ids: docIds,
          formats: Array.from(formats),
          out_dir: outDir || null,
          merge,
          /* 三份参数随任务下发（05 章 §5）：右栏的滑杆必须对本次导出立即生效，
           * 不写回全局设置——导出中心的调整是「这一次这么导」而不是改默认值 */
          chunk: settings.chunk,
          dataset: settings.dataset,
          pdf_export: settings.pdf_export,
        },
      });
      const { task: t, result } = await runTaskWithResult(resp.task_id);
      setRunning(null);
      if (t.status !== "done") {
        setFailInfo({ code: t.error_code, detail: "导出任务未能完成。", taskId: t.id });
        return;
      }
      const files = strList(result.files);
      let count = files.length;
      if (formats.has("pdf")) count += await renderPdfs(result);
      const dir = result.out_dir ?? (files[0] ? dirname(files[0]) : null) ?? (await fallbackDir(docIds));
      setDoneInfo({ dir, count });
      /* 整任务完成但个别文档/格式失败：如实提示，不让用户以为全都导出了 */
      const partial = Array.isArray(result.failed) ? result.failed.length : 0;
      if (partial) {
        const first = (result.failed as Record<string, unknown>[])[0];
        setFailInfo({
          code: typeof first?.["error_code"] === "string" ? (first["error_code"] as string) : null,
          detail: `有 ${partial} 项未能导出：${String(first?.["message"] ?? "")}`,
          taskId: t.id,
        });
      }
      toast(`导出完成${count ? `，共 ${count} 个文件` : ""}`, "ok", {
        label: "打开所在文件夹",
        onClick: () => {
          if (dir) void window.df.shell.openPath(dir);
        },
      });
    } catch {
      setRunning(null);
      setFailInfo({ code: "E06", detail: "创建导出任务失败，请确认引擎状态后重试。", taskId: "" });
    }
  };

  // ---------------- 渲染 ----------------

  const chunkChars = settings ? Math.round((settings.chunk.target_tokens * CHARS_PER_TOKEN) / 50) * 50 : 0;
  const busy = running !== null;

  return (
    <div className="page page-export">
      <div className="export-cols">
        {/* 左：范围 */}
        <section className="export-col">
          <header className="col-head">
            <span>① 选择范围</span>
            <span className="col-head-note">{picked.size} / {visible.length}</span>
          </header>
          <div className="export-scope-bar">
            <input
              className="input input-sm"
              type="search"
              value={q}
              placeholder="搜索文件名"
              aria-label="搜索文件名"
              onChange={(e) => setQ(e.target.value)}
            />
            <label className="switch">
              <input type="checkbox" checked={onlyOk} onChange={(e) => setOnlyOk(e.target.checked)} />
              仅成功
            </label>
            <button
              className="btn btn-sm"
              onClick={() =>
                setPicked((prev) => (prev.size === visible.length ? new Set() : new Set(visible.map((d) => d.id))))
              }
            >
              {picked.size === visible.length && visible.length > 0 ? "取消全选" : "全选"}
            </button>
          </div>
          <div className="col-scroll">
            {visible.length === 0 ? (
              <EmptyState title="没有可导出的文档" hint="先在工作台导入并解析文件">
                <button className="btn btn-primary" onClick={() => navigate("workbench")}>去工作台</button>
              </EmptyState>
            ) : (
              <ul className="pick-list">
                {visible.map((d) => (
                  <li key={d.id} className={`pick-row ${picked.has(d.id) ? "pick-row-on" : ""}`}>
                    <label>
                      <input type="checkbox" checked={picked.has(d.id)} onChange={() => toggleDoc(d.id)} />
                      <FmtIcon ext={d.fmt} />
                      <span className="ellipsis" title={d.name}>{d.name}</span>
                      <DocStatusBadge status={d.status} />
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>

        {/* 中：格式 */}
        <section className="export-col">
          <header className="col-head">② 选择格式</header>
          <div className="col-scroll">
            <ul className="fmt-list">
              {FORMATS.map((f) => (
                <li key={f.key} className={`fmt-row ${formats.has(f.key) ? "fmt-row-on" : ""}`}>
                  <label>
                    <input type="checkbox" checked={formats.has(f.key)} onChange={() => toggleFormat(f.key)} />
                    <span className="fmt-name">{f.name}</span>
                    <span className="fmt-desc">{f.desc}</span>
                  </label>
                </li>
              ))}
            </ul>
            <label className="switch switch-block">
              <input type="checkbox" checked={merge} onChange={(e) => setMerge(e.target.checked)} />
              合并为单个文件（多文档共用一份输出）
            </label>
          </div>
        </section>

        {/* 右：参数 */}
        <section className="export-col">
          <header className="col-head">③ 参数</header>
          <div className="col-scroll">
            {!settings ? (
              <div className="col-empty">正在读取设置…</div>
            ) : (
              <>
                <div className="param-group">
                  <div className="param-group-title">
                    切片
                    {chunkDirty && <span className="tag-dirty">导出前会重新切片</span>}
                  </div>
                  <label className="field">
                    <span className="field-label">切片长度（约 {chunkChars} 字符）</span>
                    <input
                      type="range"
                      min={256}
                      max={2048}
                      step={64}
                      value={settings.chunk.target_tokens}
                      onChange={(e) => patchChunk({ target_tokens: Number(e.target.value) })}
                    />
                    <span className="field-note" title={`内核 token 数：${settings.chunk.target_tokens}`}>
                      更长的切片保留更多上下文，更短的检索更精准
                    </span>
                  </label>
                  <label className="field">
                    <span className="field-label">相邻切片重叠 {Math.round(settings.chunk.overlap * 100)}%</span>
                    <input
                      type="range"
                      min={0}
                      max={40}
                      step={2}
                      value={Math.round(settings.chunk.overlap * 100)}
                      onChange={(e) => patchChunk({ overlap: Number(e.target.value) / 100 })}
                    />
                  </label>
                  <label className="switch switch-block">
                    <input
                      type="checkbox"
                      checked={settings.chunk.split_by_heading}
                      onChange={(e) => patchChunk({ split_by_heading: e.target.checked })}
                    />
                    按标题切分（推荐）
                  </label>
                  <label className="switch switch-block">
                    <input
                      type="checkbox"
                      checked={settings.chunk.table_atomic}
                      onChange={(e) => patchChunk({ table_atomic: e.target.checked })}
                    />
                    表格不拆开
                  </label>
                  <label className="switch switch-block">
                    <input
                      type="checkbox"
                      checked={settings.chunk.drop_header_footer}
                      onChange={(e) => patchChunk({ drop_header_footer: e.target.checked })}
                    />
                    剔除页眉页脚
                  </label>
                  <label className="switch switch-block">
                    <input
                      type="checkbox"
                      checked={settings.chunk.footnote_to_end}
                      onChange={(e) => patchChunk({ footnote_to_end: e.target.checked })}
                    />
                    脚注归到文末
                  </label>
                </div>

                <div className={`param-group ${formats.has("pdf") ? "" : "param-group-off"}`}>
                  <div className="param-group-title">PDF</div>
                  <label className="field">
                    <span className="field-label">正文字号 {settings.pdf_export.font_size}pt</span>
                    <input
                      type="range"
                      min={9}
                      max={16}
                      step={1}
                      disabled={!formats.has("pdf")}
                      value={settings.pdf_export.font_size}
                      onChange={(e) => patchPdf({ font_size: Number(e.target.value) })}
                    />
                  </label>
                  <label className="switch switch-block">
                    <input
                      type="checkbox"
                      disabled={!formats.has("pdf")}
                      checked={settings.pdf_export.header_footer}
                      onChange={(e) => patchPdf({ header_footer: e.target.checked })}
                    />
                    使用内置页眉页脚
                  </label>
                </div>

                <div className={`param-group ${formats.has("alpaca") || formats.has("sharegpt") ? "" : "param-group-off"}`}>
                  <div className="param-group-title">数据集</div>
                  <label className="field">
                    <span className="field-label">文件格式</span>
                    <select
                      className="select"
                      value={settings.dataset.file_format}
                      onChange={(e) => patchDataset({ file_format: e.target.value as "json" | "csv" })}
                    >
                      <option value="json">JSON</option>
                      <option value="csv">CSV</option>
                    </select>
                  </label>
                  <label className="field">
                    <span className="field-label">生成方式</span>
                    <select
                      className="select"
                      value={settings.dataset.mode}
                      onChange={(e) => patchDataset({ mode: e.target.value as "blank" | "rule" })}
                    >
                      <option value="blank">留空模板（推荐，供人工标注）</option>
                      <option value="rule">规则生成（实验性，质量有限）</option>
                    </select>
                  </label>
                  <label className="field">
                    <span className="field-label">每个切片生成条数</span>
                    <input
                      className="input input-sm"
                      type="number"
                      min={1}
                      max={5}
                      value={settings.dataset.per_chunk}
                      onChange={(e) => patchDataset({ per_chunk: Math.max(1, Number(e.target.value) || 1) })}
                    />
                  </label>
                </div>

                <div className="param-group">
                  <div className="param-group-title">输出位置</div>
                  <div className="path-row">
                    <span className="path-text ellipsis" title={outDir || "默认：各文档的 exports 目录"}>
                      {outDir || "默认：各文档的 exports 目录"}
                    </span>
                    <button
                      className="btn btn-sm"
                      onClick={() => {
                        void window.df.dialog.pickDirectory().then((p) => {
                          if (p) setOutDir(p);
                        });
                      }}
                    >
                      选择…
                    </button>
                    {outDir && (
                      <button className="btn btn-sm btn-ghost" onClick={() => setOutDir("")}>还原默认</button>
                    )}
                  </div>
                </div>
              </>
            )}
          </div>
        </section>
      </div>

      <footer className="export-foot">
        {running && (
          <div className="export-progress">
            <ProgressBar value={normProgress(running.progress)} />
            <span className="hint-dim">正在导出…（{fmtTime(running.created_at)} 创建）</span>
          </div>
        )}
        {doneInfo && !running && (
          <div className="export-done">
            <span className="ok-mark">✓</span>
            导出完成{doneInfo.count ? `，共 ${doneInfo.count} 个文件` : ""}。
            {doneInfo.dir && (
              <button className="btn btn-sm" onClick={() => void window.df.shell.openPath(doneInfo.dir as string)}>
                打开所在文件夹
              </button>
            )}
          </div>
        )}
        {failInfo && !running && (
          <div className="export-fail">
            <ErrorDetail
              code={failInfo.code}
              detail={failInfo.detail}
              taskId={failInfo.taskId || undefined}
              onViewInLogs={failInfo.taskId ? () => navigate("logs", { task_id: failInfo.taskId }) : undefined}
            />
          </div>
        )}
        <div className="export-actions">
          <span className="hint-dim">
            已选 {picked.size} 个文档 · {formats.size} 种格式
          </span>
          <button
            className="btn btn-primary"
            disabled={busy || picked.size === 0 || formats.size === 0 || !settings}
            onClick={() => void startExport()}
          >
            {busy ? "导出中…" : "开始导出"}
          </button>
        </div>
      </footer>
    </div>
  );
}
