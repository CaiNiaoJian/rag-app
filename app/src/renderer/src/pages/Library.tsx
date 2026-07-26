/* 文档库（07 章 §1 第 2 页）：列表 + 解析预览三栏（产品核心卖点的载体）。
 *
 * 设计取舍：
 * - **大结果走磁盘**：/documents/{id}/ir 与 /chunks 按 02 章 §2.2 返回文件路径，
 *   这里经 window.df.files.readText 读盘，绝不让 MB 级 JSON 过 HTTP。
 *   端点若直接内联返回也能吃下（并行开发期的双形态兼容）。
 * - **三栏的意义是「对照」**：左结构树回答「解析出了什么」，中页快照回答「原文长什么样」，
 *   右渲染结果回答「导出会是什么」。中右按滚动比例联动——精确的节点级映射需要
 *   bbox↔渲染坐标的双向标定，收益远小于成本，比例联动已足够支撑抽查。
 * - **低置信/切片边界**：右栏「切片边界」页签把每个切片画成卡片，命中低置信节点的整块标黄，
 *   比在 Markdown 正文里插标记更不破坏阅读。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../appctx";
import { DocStatusBadge, FmtIcon, LevelBadge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { ErrorDetail } from "../components/ErrorDetail";
import { MdView } from "../components/MdView";
import { ConfirmModal } from "../components/Modal";
import { Pagination } from "../components/Pagination";
import { Tree, type TreeItem } from "../components/Tree";
import type { ChunkRow, DocStatus, DocumentRow, IRDocument, IRNode } from "../types";
import { SUPPORTED_EXTS } from "../types";
import { fmtBytes, fmtPct, fmtTime, parseJsonSafe, truncate } from "../util";

const PAGE_SIZE = 50;
/* 低于该置信度的节点在树上打黄点、在切片卡片上标黄（07 章「低置信区域黄色高亮」） */
const LOW_CONF = 0.6;

function unwrapItems<T>(resp: unknown): { items: T[]; total: number } {
  if (Array.isArray(resp)) return { items: resp as T[], total: resp.length };
  if (resp && typeof resp === "object") {
    const r = resp as Record<string, unknown>;
    for (const key of ["items", "documents", "chunks", "results"]) {
      const v = r[key];
      if (Array.isArray(v)) {
        return { items: v as T[], total: typeof r.total === "number" ? r.total : v.length };
      }
    }
  }
  return { items: [], total: 0 };
}

/* 端点可能只回一个磁盘路径（大结果落盘契约），从常见键名里挑出来 */
function pathField(resp: unknown): string | null {
  if (typeof resp === "string") return resp;
  if (resp && typeof resp === "object") {
    const r = resp as Record<string, unknown>;
    for (const key of ["path", "ir_path", "file", "chunks_path", "md_path"]) {
      const v = r[key];
      if (typeof v === "string" && v) return v;
    }
  }
  return null;
}

/* 相对路径解析（doc.md 里的 ../assets/x.png）。util.joinPath 不处理 ..，这里补上 */
function resolveRelative(baseDir: string, rel: string): string {
  const sep = baseDir.includes("\\") ? "\\" : "/";
  const parts = baseDir.split(/[\\/]/);
  for (const seg of rel.split(/[\\/]/)) {
    if (!seg || seg === ".") continue;
    if (seg === "..") parts.pop();
    else parts.push(seg);
  }
  return parts.join(sep);
}

function parentDir(p: string): string {
  return p.replace(/[\\/][^\\/]*$/, "");
}

// ---------------- IR → 结构树 / 兜底 Markdown ----------------

const TYPE_ICON: Record<string, { icon: string; cls: string }> = {
  heading: { icon: "章", cls: "ti-h" },
  title: { icon: "章", cls: "ti-h" },
  section: { icon: "章", cls: "ti-h" },
  paragraph: { icon: "段", cls: "ti-p" },
  text: { icon: "段", cls: "ti-p" },
  table: { icon: "表", cls: "ti-t" },
  figure: { icon: "图", cls: "ti-i" },
  image: { icon: "图", cls: "ti-i" },
  picture: { icon: "图", cls: "ti-i" },
  list: { icon: "列", cls: "ti-l" },
  slide: { icon: "页", cls: "ti-s" },
  sheet: { icon: "表", cls: "ti-t" },
  sheet_region: { icon: "区", cls: "ti-t" },
  caption: { icon: "注", cls: "ti-o" },
  footnote: { icon: "注", cls: "ti-o" },
};

function nodeText(n: IRNode): string {
  const c = n.content ?? {};
  return (c.title || c.text || c.caption || c.name || c.ocr_text || "").trim();
}

function nodePage(n: IRNode): number | undefined {
  const p = n.prov && n.prov.length ? n.prov[0]?.page : undefined;
  return typeof p === "number" ? p : undefined;
}

interface NodeMeta {
  page?: number;
  text: string;
  low: boolean;
}

function buildTree(ir: IRDocument): { items: TreeItem[]; index: Record<string, NodeMeta> } {
  const byId = new Map<string, IRNode>();
  for (const n of ir.nodes) byId.set(n.id, n);
  /* 以 parent 指针为准建父子表：children 字段可能缺失或与 parent 不一致，
   * parent 是解析器一定会写的字段 */
  const kids = new Map<string, string[]>();
  const roots: string[] = [];
  for (const n of ir.nodes) {
    const p = n.parent && byId.has(n.parent) ? n.parent : null;
    if (p) {
      const arr = kids.get(p);
      if (arr) arr.push(n.id);
      else kids.set(p, [n.id]);
    } else {
      roots.push(n.id);
    }
  }
  const index: Record<string, NodeMeta> = {};
  const seen = new Set<string>();

  const make = (id: string): TreeItem | null => {
    if (seen.has(id)) return null; // 环形引用保护（异常 IR 不应拖垮 UI）
    seen.add(id);
    const n = byId.get(id);
    if (!n) return null;
    const t = nodeText(n);
    const page = nodePage(n);
    const low = typeof n.confidence === "number" && n.confidence < LOW_CONF;
    index[id] = { page, text: t, low };
    const style = TYPE_ICON[n.type] ?? { icon: "·", cls: "ti-o" };
    return {
      id,
      label: t ? truncate(t, 42) : `（${n.type}）`,
      icon: style.icon,
      iconClass: style.cls,
      lowConfidence: low,
      meta: page ? `p${page}` : undefined,
      children: (kids.get(id) ?? []).map(make).filter((x): x is TreeItem => x !== null),
    };
  };

  return { items: roots.map(make).filter((x): x is TreeItem => x !== null), index };
}

/* doc.md 读不到时的兜底：直接由 IR 渲染一份等价 Markdown。
 * 只覆盖展示需要的节点类型，不追求与导出层逐字一致（导出以引擎产物为准）。 */
function irToMarkdown(ir: IRDocument): string {
  const out: string[] = [];
  for (const n of ir.nodes) {
    const c = n.content ?? {};
    switch (n.type) {
      case "heading":
      case "title":
      case "section": {
        const level = Math.min(Math.max(n.level ?? 1, 1), 6);
        out.push(`${"#".repeat(level)} ${nodeText(n)}`);
        break;
      }
      case "table": {
        const cells = c.table?.cells ?? [];
        if (!cells.length) break;
        const maxR = Math.max(...cells.map((x) => x.r));
        const maxC = Math.max(...cells.map((x) => x.c));
        const grid: string[][] = [];
        for (let r = 0; r <= maxR; r++) grid.push(new Array<string>(maxC + 1).fill(""));
        for (const cell of cells) {
          const row = grid[cell.r];
          if (row) row[cell.c] = (cell.text ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ");
        }
        const [head, ...rest] = grid;
        if (!head) break;
        out.push(`| ${head.join(" | ")} |`);
        out.push(`| ${head.map(() => "---").join(" | ")} |`);
        for (const row of rest) out.push(`| ${row.join(" | ")} |`);
        break;
      }
      case "figure":
      case "image":
      case "picture":
        if (c.image_ref) out.push(`![${c.caption ?? ""}](${c.image_ref})`);
        else if (c.caption) out.push(`> ${c.caption}`);
        break;
      default: {
        const t = nodeText(n);
        if (t) out.push(t);
      }
    }
    out.push("");
  }
  return out.join("\n");
}

// ---------------- 页快照 ----------------

/* 页快照直接用 /documents/{id} 给出的 preview_dir 拼路径（零 HTTP 往返）；
 * 目录未知或该页文件缺失时再退回 /documents/{id}/preview/{page} 端点问一次。 */
function PageImage({ docId, page, previewDir }: { docId: string; page: number; previewDir: string | null }) {
  const { client } = useApp();
  const [src, setSrc] = useState(() =>
    previewDir ? window.df.files.fileUrl(resolveRelative(previewDir, `p${page}.png`)) : "",
  );
  const [failed, setFailed] = useState(false);
  const asked = useRef(false);

  const askEngine = useCallback(async () => {
    if (asked.current) {
      setFailed(true);
      return;
    }
    asked.current = true;
    try {
      const resp = await client.getJson<unknown>(`/documents/${encodeURIComponent(docId)}/preview/${page}`);
      const p = pathField(resp);
      if (p) setSrc(window.df.files.fileUrl(p));
      else setFailed(true);
    } catch {
      setFailed(true);
    }
  }, [client, docId, page]);

  useEffect(() => {
    if (!src) void askEngine();
  }, [src, askEngine]);

  return (
    <figure className="page-shot" data-page={page} id={`page-${page}`}>
      {failed || !src ? (
        <div className="page-shot-missing">第 {page} 页暂无快照</div>
      ) : (
        <img src={src} alt={`第 ${page} 页`} loading="lazy" onError={() => void askEngine()} />
      )}
      <figcaption>第 {page} 页</figcaption>
    </figure>
  );
}

// ---------------- 主页面 ----------------

/* GET /documents/{id} 在表行之外还给出各产物的绝对路径与存在标志，
 * 预览三栏的取数全部基于它，避免前端自己拼 workspace 目录结构 */
interface DocDetail extends DocumentRow {
  ir_path?: string;
  ir_exists?: boolean;
  md_path?: string;
  md_exists?: boolean;
  preview_dir?: string;
  preview_pages?: number;
  chunk_count?: number;
}

interface PreviewState {
  doc: DocumentRow;
  ir: IRDocument | null;
  parsedDir: string | null;
  previewDir: string | null;
  previewPages: number;
  md: string;
  chunks: ChunkRow[];
  tree: TreeItem[];
  index: Record<string, NodeMeta>;
  error: string | null;
  /* 与 error 配套的错误码：错误三级呈现的第一级文案由它决定（07 章 §4），
   * 「IR 文件损坏」和「压根还没解析」必须给不同的码，否则建议操作会驴唇不对马嘴 */
  errorCode: string | null;
  /* 载入期先不渲染页快照：此时还没拿到 workspace 目录，
   * 提前渲染会让每一页都白跑一次 preview 端点 */
  loading: boolean;
}

export function Library() {
  const { client, nav, navigate, toast } = useApp();

  const [rows, setRows] = useState<DocumentRow[]>([]);
  const [total, setTotal] = useState(0);
  const [pageNo, setPageNo] = useState(1);
  const [q, setQ] = useState("");
  const [fmt, setFmt] = useState("");
  const [status, setStatus] = useState("");
  const [since, setSince] = useState("");
  const [loading, setLoading] = useState(false);
  const [toDelete, setToDelete] = useState<DocumentRow | null>(null);

  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [rightTab, setRightTab] = useState<"md" | "chunks">("md");
  const [syncScroll, setSyncScroll] = useState(true);

  const midRef = useRef<HTMLDivElement | null>(null);
  const rightRef = useRef<HTMLDivElement | null>(null);
  const syncing = useRef(false);
  /* 载入令牌：用户在加载途中返回列表或换了文档时，旧的异步结果必须丢弃 */
  const previewToken = useRef(0);

  // ---------------- 列表 ----------------

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(pageNo) });
      if (q.trim()) params.set("q", q.trim());
      if (fmt) params.set("fmt", fmt);
      if (status) params.set("status", status);
      const resp = await client.getJson<unknown>(`/documents?${params.toString()}`);
      const { items, total: n } = unwrapItems<DocumentRow>(resp);
      setRows(items);
      setTotal(n);
    } catch {
      setRows([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [client, pageNo, q, fmt, status]);

  /* 输入搜索词时逐字请求没有意义，统一延后 200ms 合并 */
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 200);
    return () => window.clearTimeout(timer);
  }, [load]);

  /* 时间筛选在前端做：引擎列表端点只认 status/fmt/q，
   * 再为「近 7 天」加一个查询参数会让契约变复杂，收益不大 */
  const visible = useMemo(() => {
    if (!since) return rows;
    const days = Number(since);
    const from = Date.now() - days * 86400000;
    return rows.filter((r) => {
      const t = new Date(r.created_at).getTime();
      return isFinite(t) ? t >= from : true;
    });
  }, [rows, since]);

  // ---------------- 预览 ----------------

  /* 切片端点单页上限 200，而「切片边界」要看全量；这里循环取完，
   * 但封顶 10 页（2000 块）——再多的话人工抽查也看不过来，先保 UI 不卡 */
  const loadAllChunks = useCallback(
    async (docId: string): Promise<ChunkRow[]> => {
      const out: ChunkRow[] = [];
      try {
        for (let p = 1; p <= 10; p++) {
          const resp = await client.getJson<unknown>(
            `/documents/${encodeURIComponent(docId)}/chunks?page=${p}&page_size=200`,
          );
          const { items, total } = unwrapItems<ChunkRow>(resp);
          out.push(...items);
          if (items.length < 200 || out.length >= total) break;
        }
      } catch {
        /* 尚未切片：右栏给空态提示，不算错误 */
      }
      return out;
    },
    [client],
  );

  const openPreview = useCallback(
    async (doc: DocumentRow) => {
      const token = ++previewToken.current;
      setPreview({
        doc,
        ir: null,
        parsedDir: null,
        previewDir: null,
        previewPages: 0,
        md: "",
        chunks: [],
        tree: [],
        index: {},
        error: null,
        errorCode: null,
        loading: true,
      });
      setSelected(null);
      let ir: IRDocument | null = null;
      let parsedDir: string | null = null;
      let error: string | null = null;
      let errorCode: string | null = null;

      /* 先取文档详情：它一并给出 ir/md/preview 的绝对路径与存在标志，
       * 后面就不用为「文件在不在」再往返一次 */
      let detail: DocDetail = doc;
      try {
        detail = await client.getJson<DocDetail>(`/documents/${encodeURIComponent(doc.id)}`);
      } catch {
        /* 详情取不到时退回按端点逐个问 */
      }

      let irPath = detail.ir_exists === false ? null : detail.ir_path ?? null;
      if (!irPath) {
        try {
          irPath = pathField(await client.getJson<unknown>(`/documents/${encodeURIComponent(doc.id)}/ir`));
        } catch {
          irPath = null;
        }
      }
      if (irPath) {
        parsedDir = parentDir(irPath);
        try {
          ir = JSON.parse(await window.df.files.readText(irPath)) as IRDocument;
        } catch {
          error = "解析结果文件读取失败，可能已被清理或损坏。";
          errorCode = "E01";
        }
      } else {
        error = "该文档还没有解析结果，先解析后再来预览。";
        errorCode = "E05";
      }

      let md = "";
      const mdPath = detail.md_exists === false ? null : detail.md_path ?? (parsedDir ? resolveRelative(parsedDir, "doc.md") : null);
      if (mdPath) {
        try {
          md = await window.df.files.readText(mdPath);
        } catch {
          md = "";
        }
      }
      /* doc.md 缺失（解析期渲染失败）时由 IR 现场生成一份等价视图，保证右栏不空 */
      if (!md && ir) md = irToMarkdown(ir);

      const chunks = await loadAllChunks(doc.id);

      if (token !== previewToken.current) return;
      const built = ir ? buildTree(ir) : { items: [] as TreeItem[], index: {} as Record<string, NodeMeta> };
      setPreview({
        doc: { ...doc, ...detail },
        ir,
        parsedDir,
        previewDir: detail.preview_dir ?? (parsedDir ? resolveRelative(parentDir(parsedDir), "preview") : null),
        previewPages: detail.preview_pages ?? 0,
        md,
        chunks,
        tree: built.items,
        index: built.index,
        error,
        errorCode,
        loading: false,
      });
    },
    [client, loadAllChunks],
  );

  /* 跨页跳转：工作台/日志页可以带 doc_id 直接进预览，带 q 则只改搜索条件 */
  useEffect(() => {
    if (nav.page !== "library") return;
    const nq = nav.params["q"];
    if (nq !== undefined) {
      setPreview(null);
      setQ(nq);
      setPageNo(1);
    }
    const docId = nav.params["doc_id"];
    if (docId) {
      void (async () => {
        try {
          const doc = await client.getJson<DocumentRow>(`/documents/${encodeURIComponent(docId)}`);
          if (doc && doc.id) await openPreview(doc);
        } catch {
          toast("打开文档失败", "err");
        }
      })();
    }
  }, [nav, client, openPreview, toast]);

  const reparse = async (doc: DocumentRow) => {
    try {
      await client.postJson<{ task_id: string }>("/tasks", { type: "parse", payload: { doc_id: doc.id } });
      toast("已加入解析队列", "ok", { label: "去工作台", onClick: () => navigate("workbench") });
    } catch {
      toast("创建解析任务失败", "err");
    }
  };

  const doDelete = async (doc: DocumentRow) => {
    try {
      await client.del(`/documents/${encodeURIComponent(doc.id)}`);
      toast(`已删除「${doc.name}」`, "ok");
      if (preview?.doc.id === doc.id) setPreview(null);
      void load();
    } catch {
      toast("删除失败", "err");
    }
  };

  // ---------------- 滚动联动 ----------------

  const onSyncScroll = (from: "mid" | "right") => {
    if (!syncScroll || syncing.current) return;
    const a = from === "mid" ? midRef.current : rightRef.current;
    const b = from === "mid" ? rightRef.current : midRef.current;
    if (!a || !b) return;
    const aRange = a.scrollHeight - a.clientHeight;
    const bRange = b.scrollHeight - b.clientHeight;
    if (aRange <= 0 || bRange <= 0) return;
    syncing.current = true;
    b.scrollTop = (a.scrollTop / aRange) * bRange;
    /* 用一帧的窗口屏蔽被动触发的 scroll 事件，避免两栏互相追赶 */
    window.requestAnimationFrame(() => {
      syncing.current = false;
    });
  };

  const onSelectNode = (item: TreeItem) => {
    setSelected(item.id);
    const meta = preview?.index[item.id];
    if (!meta) return;
    if (meta.page && midRef.current) {
      const el = midRef.current.querySelector(`#page-${meta.page}`);
      if (el) el.scrollIntoView({ block: "start" });
    }
    if (rightTab === "chunks") {
      const hit = preview?.chunks.find((c) => parseJsonSafe<string[]>(c.node_ids, []).includes(item.id));
      if (hit && rightRef.current) {
        const el = rightRef.current.querySelector(`#chunk-${cssId(hit.id)}`);
        if (el) el.scrollIntoView({ block: "center" });
      }
      return;
    }
    /* Markdown 栏没有节点锚点，用文本片段做一次就近匹配——够用且零成本 */
    const snippet = meta.text.slice(0, 12);
    if (snippet && rightRef.current) {
      const nodes = rightRef.current.querySelectorAll(".md-view > *");
      for (const el of Array.from(nodes)) {
        if ((el.textContent ?? "").includes(snippet)) {
          el.scrollIntoView({ block: "center" });
          break;
        }
      }
    }
  };

  const resolveAsset = useCallback(
    (rel: string) => (preview?.parsedDir ? window.df.files.fileUrl(resolveRelative(preview.parsedDir, rel)) : rel),
    [preview?.parsedDir],
  );

  const lowChunkIds = useMemo(() => {
    if (!preview) return new Set<string>();
    const lowNodes = new Set(Object.keys(preview.index).filter((k) => preview.index[k]?.low));
    const set = new Set<string>();
    for (const c of preview.chunks) {
      const ids = parseJsonSafe<string[]>(c.node_ids, []);
      if (ids.some((id) => lowNodes.has(id))) set.add(c.id);
    }
    return set;
  }, [preview]);

  // ---------------- 渲染：预览三栏 ----------------

  if (preview) {
    const d = preview.doc;
    /* 页数以实际生成的快照数为准；没生成快照时退回文档页数（图片会各自显示占位） */
    const pageCount = preview.previewPages || d.page_cnt || 0;
    const pages = pageCount > 0 ? Array.from({ length: pageCount }, (_, i) => i + 1) : [];
    return (
      <div className="page page-preview">
        <header className="preview-head">
          <button
            className="btn btn-sm"
            onClick={() => {
              previewToken.current += 1; // 作废可能仍在途的载入
              setPreview(null);
            }}
          >
            ← 返回列表
          </button>
          <FmtIcon ext={d.fmt} />
          <span className="preview-name ellipsis" title={d.name}>{d.name}</span>
          <DocStatusBadge status={d.status} />
          <div className="metrics-bar">
            <Metric label="文本覆盖率" value={fmtPct(d.text_coverage)} tone={toneOf(d.text_coverage, 0.9, 0.7)} />
            <Metric label="表格置信度" value={fmtPct(d.table_confidence)} tone={toneOf(d.table_confidence, 0.85, 0.6)} />
            <Metric label="OCR 置信度" value={fmtPct(d.ocr_confidence)} tone={toneOf(d.ocr_confidence, 0.85, 0.6)} />
            <div className="metric">
              <span className="metric-label">解析级别</span>
              <span className="metric-value"><LevelBadge level={d.parse_level} /></span>
            </div>
            <Metric label="降级页" value={String(d.degraded_pages ?? 0)} tone={(d.degraded_pages ?? 0) > 0 ? "warn" : "ok"} />
            <Metric label="切片数" value={String(preview.chunks.length)} tone="none" />
          </div>
          <label className="switch">
            <input type="checkbox" checked={syncScroll} onChange={(e) => setSyncScroll(e.target.checked)} />
            滚动联动
          </label>
        </header>

        {preview.error && (
          <div className="preview-error">
            {d.status === "imported" || d.status === "parsing" ? (
              /* 还没解析不是错误，别用红色错误框吓人 */
              <div className="banner banner-warn">
                {d.status === "parsing" ? "正在解析中，完成后即可查看结构与切片。" : "该文档还没有解析。"}
                <button className="btn btn-sm" onClick={() => void reparse(d)}>立即解析</button>
              </div>
            ) : (
              <ErrorDetail
                code={preview.errorCode ?? "E05"}
                detail={preview.error}
                docId={d.id}
                fileName={d.name}
                onRetry={() => void reparse(d)}
              />
            )}
          </div>
        )}

        <div className="preview-body">
          <aside className="preview-col preview-tree">
            <div className="col-head">结构</div>
            <div className="col-scroll">
              {preview.tree.length ? (
                <Tree items={preview.tree} selectedId={selected} onSelect={onSelectNode} />
              ) : (
                <div className="col-empty">暂无结构信息</div>
              )}
            </div>
          </aside>

          <section className="preview-col preview-pages">
            <div className="col-head">原文</div>
            <div className="col-scroll" ref={midRef} onScroll={() => onSyncScroll("mid")}>
              {preview.loading ? (
                <div className="col-empty">正在载入…</div>
              ) : pages.length ? (
                pages.map((p) => <PageImage key={p} docId={d.id} page={p} previewDir={preview.previewDir} />)
              ) : (
                <div className="col-empty">没有页快照（该格式或该文档未生成预览图）</div>
              )}
            </div>
          </section>

          <section className="preview-col preview-md">
            <div className="col-head col-head-tabs">
              <button className={`tab ${rightTab === "md" ? "tab-active" : ""}`} onClick={() => setRightTab("md")}>
                解析结果
              </button>
              <button className={`tab ${rightTab === "chunks" ? "tab-active" : ""}`} onClick={() => setRightTab("chunks")}>
                切片边界（{preview.chunks.length}）
              </button>
            </div>
            <div className="col-scroll" ref={rightRef} onScroll={() => onSyncScroll("right")}>
              {rightTab === "md" ? (
                preview.md ? (
                  <MdView source={preview.md} resolveUrl={resolveAsset} />
                ) : (
                  <div className="col-empty">暂无渲染结果</div>
                )
              ) : preview.chunks.length ? (
                <div className="chunk-list">
                  {preview.chunks.map((c) => (
                    <article
                      key={c.id}
                      id={`chunk-${cssId(c.id)}`}
                      className={`chunk-card ${lowChunkIds.has(c.id) ? "chunk-low" : ""}`}
                    >
                      <header className="chunk-head">
                        <span className="chunk-seq">#{c.seq}</span>
                        <span className="chunk-path ellipsis" title={c.heading_path ?? ""}>
                          {c.heading_path || "（无标题路径）"}
                        </span>
                        <span className="chunk-len" title={`内核 token 数：${c.token_count}`}>
                          {c.char_count} 字
                        </span>
                      </header>
                      <div className="chunk-text">{c.text}</div>
                      {lowChunkIds.has(c.id) && <div className="chunk-low-tag">该区域识别置信度较低，建议人工核对</div>}
                    </article>
                  ))}
                </div>
              ) : (
                <div className="col-empty">暂无切片（可在导出中心调整切片参数后重切）</div>
              )}
            </div>
          </section>
        </div>
      </div>
    );
  }

  // ---------------- 渲染：列表 ----------------

  return (
    <div className="page page-library">
      <div className="filter-bar">
        <input
          className="input"
          type="search"
          value={q}
          placeholder="搜索文件名"
          aria-label="搜索文件名"
          onChange={(e) => {
            setQ(e.target.value);
            setPageNo(1);
          }}
        />
        <select className="select" value={fmt} aria-label="按类型筛选" onChange={(e) => { setFmt(e.target.value); setPageNo(1); }}>
          <option value="">全部类型</option>
          {SUPPORTED_EXTS.map((x) => (
            <option key={x} value={x}>{x.toUpperCase()}</option>
          ))}
        </select>
        <select className="select" value={status} aria-label="按状态筛选" onChange={(e) => { setStatus(e.target.value); setPageNo(1); }}>
          <option value="">全部状态</option>
          <option value="imported">已导入</option>
          <option value="parsing">解析中</option>
          <option value="ok">成功</option>
          <option value="warning">警告</option>
          <option value="failed">失败</option>
        </select>
        <select className="select" value={since} aria-label="按时间筛选" onChange={(e) => setSince(e.target.value)}>
          <option value="">全部时间</option>
          <option value="1">最近 1 天</option>
          <option value="7">最近 7 天</option>
          <option value="30">最近 30 天</option>
        </select>
        <button className="btn btn-sm" onClick={() => void load()}>刷新</button>
      </div>

      {visible.length === 0 ? (
        <EmptyState
          title={loading ? "正在读取文档库…" : "还没有文档"}
          hint="把文件拖到这里，或到工作台导入"
        >
          <button className="btn btn-primary" onClick={() => navigate("workbench")}>去工作台导入</button>
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th className="col-name">文件名</th>
                <th>状态</th>
                <th>级别</th>
                <th>页数</th>
                <th>覆盖率</th>
                <th>大小</th>
                <th>导入时间</th>
                <th className="col-ops">操作</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((d) => (
                <tr
                  key={d.id}
                  onDoubleClick={() => void openPreview(d)}
                  title="双击查看解析预览"
                  className={d.status === "failed" ? "row-failed" : ""}
                >
                  <td className="col-name">
                    <FmtIcon ext={d.fmt} />
                    <span className="ellipsis" title={d.name}>{d.name}</span>
                  </td>
                  <td><DocStatusBadge status={d.status as DocStatus} /></td>
                  <td><LevelBadge level={d.parse_level} /></td>
                  <td>{d.page_cnt ?? "—"}</td>
                  <td>{fmtPct(d.text_coverage)}</td>
                  <td>{fmtBytes(d.size)}</td>
                  <td>{fmtTime(d.created_at)}</td>
                  <td className="col-ops">
                    <button className="btn btn-sm" onClick={() => void openPreview(d)}>预览</button>
                    <button className="btn btn-sm" onClick={() => void reparse(d)}>重新解析</button>
                    <button className="btn btn-sm btn-ghost" onClick={() => navigate("export", { doc_id: d.id })}>导出</button>
                    <button className="btn btn-sm btn-danger-ghost" onClick={() => setToDelete(d)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Pagination page={pageNo} total={total} pageSize={PAGE_SIZE} onChange={setPageNo} />

      <ConfirmModal
        open={toDelete !== null}
        title="删除文档"
        danger
        confirmText="删除"
        message={`将删除「${toDelete?.name ?? ""}」及其解析产物、切片与导出文件，此操作不可撤销。`}
        onConfirm={() => {
          if (toDelete) void doDelete(toDelete);
        }}
        onClose={() => setToDelete(null)}
      />
    </div>
  );
}

/* CSS 选择器里不能直接用带特殊字符的 id，做一次保守转义 */
function cssId(id: string): string {
  return id.replace(/[^A-Za-z0-9_-]/g, "_");
}

function toneOf(v: number | null | undefined, good: number, warn: number): "ok" | "warn" | "err" | "none" {
  if (v == null || !isFinite(v)) return "none";
  const x = v <= 1 ? v : v / 100;
  if (x >= good) return "ok";
  if (x >= warn) return "warn";
  return "err";
}

function Metric({ label, value, tone }: { label: string; value: string; tone: "ok" | "warn" | "err" | "none" }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className={`metric-value metric-${tone}`}>{value}</span>
    </div>
  );
}
