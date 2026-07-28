/* 工作台（默认页，07 章 §1）：上半常驻拖拽导入区，下半任务队列。
 *
 * 设计取舍：
 * - **导入即建任务不跳页**：用户流 A 的主线是「拖一堆文件进来 → 看着它们跑完」，
 *   任何跳转都会打断这个观察过程，所以导入结果直接落到下半区的队列里。
 * - **SSE 优先、轮询兜底**：running 任务逐个订阅 /tasks/{id}/events 拿页粒度进度；
 *   断流（引擎重启/网络层异常）时 api.ts 会静默结束，这里把节奏降级为 5s 轮询并明示，
 *   绝不因为进度流断了就弹错——任务本身还在引擎里跑。
 * - **队列暂停是引擎侧的持久开关**（POST /queue/pause，meta 表持久化）：排队任务
 *   原地保留不取消不重建（task_id 稳定，追溯链不断），正在处理的任务跑完为止；
 *   刷新页面、重开窗口、引擎重启都不会丢暂停态。此前「取消再重建」的模拟已废弃。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { ApiError } from "../api";
import { useApp } from "../appctx";
import { FmtIcon, TaskStatusBadge } from "../components/Badge";
import { Drawer } from "../components/Drawer";
import { EmptyState } from "../components/EmptyState";
import { ErrorDetail, StageTimeline, buildStageSteps } from "../components/ErrorDetail";
import { Modal } from "../components/Modal";
import { Pagination } from "../components/Pagination";
import { ProgressBar } from "../components/ProgressBar";
import type { DocumentRow, LogRow, TaskRow } from "../types";
import { STAGE_LABEL, TASK_TYPE_LABEL } from "../types";
import { extOf, fmtBytes, fmtDuration, fmtTime, normProgress, parseJsonSafe } from "../util";

const PAGE_SIZE = 50;

interface ImportResult {
  doc_id?: string;
  name?: string;
  duplicate_of?: string | null;
  task_id?: string;
}

/* 引擎侧被跳过的文件（文件夹/模组包/不支持/复制失败），带人话原因 */
interface ImportSkip {
  path?: string;
  name?: string;
  reason?: string;
  is_kmod?: boolean;
}

/* 引擎列表端点统一 {items,total}；这里做一层宽容解包，
 * 免得某个端点换了外层键名就整页空白（并行开发期的现实防御）。 */
function unwrapItems<T>(resp: unknown): { items: T[]; total: number } {
  if (Array.isArray(resp)) return { items: resp as T[], total: resp.length };
  if (resp && typeof resp === "object") {
    const r = resp as Record<string, unknown>;
    for (const key of ["items", "documents", "tasks", "results", "logs"]) {
      const v = r[key];
      if (Array.isArray(v)) {
        return { items: v as T[], total: typeof r.total === "number" ? r.total : v.length };
      }
    }
  }
  return { items: [], total: 0 };
}

/* SSE 数据是 Record<string, unknown>，取数一律走这两个窄化函数 */
function num(v: unknown): number | undefined {
  return typeof v === "number" && isFinite(v) ? v : undefined;
}
function str(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined;
}

interface LiveInfo {
  progress?: number;
  stage?: string;
  page?: number;
  total?: number;
  degraded?: string;
}

interface TaskDetail extends TaskRow {
  events?: LogRow[];
}

export function Workbench({ incoming }: { incoming: { seq: number; entries: DfPathEntry[] } }) {
  const { client, page: activePage, navigate, toast } = useApp();

  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [total, setTotal] = useState(0);
  const [pageNo, setPageNo] = useState(1);
  const [docs, setDocs] = useState<Record<string, DocumentRow>>({});
  const [live, setLive] = useState<Record<string, LiveInfo>>({});
  const [streaming, setStreaming] = useState<string[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);

  const [confirmList, setConfirmList] = useState<DfPathEntry[] | null>(null);
  const [importing, setImporting] = useState(false);
  const [importErr, setImportErr] = useState<{ code: string | null; detail: string } | null>(null);

  /* 暂停态的唯一事实来源是引擎（GET /queue）；本地 state 只是它的镜像 */
  const [paused, setPaused] = useState(false);
  const [queuedCnt, setQueuedCnt] = useState(0);

  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [detailLogs, setDetailLogs] = useState<LogRow[]>([]);

  const lastIncoming = useRef(0);
  /* 抽屉载入令牌：详情要两次往返才补全，中途用户关掉抽屉或点开另一个任务时，
   * 旧请求的结果必须丢弃——否则已关闭的抽屉会被迟到的响应重新弹开 */
  const detailToken = useRef(0);

  // ---------------- 数据拉取 ----------------

  const refresh = useCallback(async () => {
    try {
      const resp = await client.getJson<unknown>(`/tasks?page=${pageNo}`);
      const { items, total: n } = unwrapItems<TaskRow>(resp);
      setTasks(items);
      setTotal(n);
      setLoadFailed(false);
      /* 文档名要从 documents 取（tasks 表只有 doc_id）；取第一页够覆盖近期任务 */
      const docResp = await client.getJson<unknown>("/documents?page=1");
      const map: Record<string, DocumentRow> = {};
      for (const d of unwrapItems<DocumentRow>(docResp).items) map[d.id] = d;
      setDocs(map);
      /* 暂停态跟着引擎走：别的窗口切过开关、或引擎重启恢复了暂停态，这里都会对齐 */
      const queue = await client.getJson<{ paused?: boolean; queued?: number }>("/queue");
      setPaused(queue.paused === true);
      setQueuedCnt(typeof queue.queued === "number" ? queue.queued : 0);
    } catch {
      /* 引擎未就绪时静默重试（顶栏状态灯已经在提示），不打断用户 */
      setLoadFailed(true);
    }
  }, [client, pageNo]);

  useEffect(() => {
    void refresh();
    /* 本页常驻挂载：不可见时把节奏放慢，避免后台空转 */
    const period = activePage === "workbench" ? 5000 : 20000;
    const timer = window.setInterval(() => void refresh(), period);
    return () => window.clearInterval(timer);
  }, [refresh, activePage]);

  const runningIds = useMemo(
    () => tasks.filter((t) => t.status === "running").map((t) => t.id).sort().join(","),
    [tasks],
  );

  /* 活跃任务的进度流；组件卸载或运行集合变化时必须全部取消 */
  useEffect(() => {
    const ids = runningIds ? runningIds.split(",") : [];
    if (!ids.length) {
      setStreaming([]);
      return;
    }
    const patch = (id: string, p: LiveInfo) =>
      setLive((prev) => ({ ...prev, [id]: { ...prev[id], ...p } }));

    setStreaming(ids);
    /* 本轮订阅是否已被清理：abort 触发的 onEnd 是异步的，会晚于下一轮 effect 的
     * setStreaming(ids) 才执行。不加这道门闩，旧流的收尾会把新流刚登记的 id 抹掉，
     * streaming 变空后 UI 就会误报「进度已降级为定时刷新」且再也恢复不了。 */
    let disposed = false;
    const offs = ids.map((id) =>
      client.sse(id, {
        progress: (d) => {
          const pg = num(d.page);
          const tt = num(d.total);
          const explicit = num(d.progress);
          patch(id, {
            page: pg,
            total: tt,
            stage: str(d.stage),
            progress:
              explicit !== undefined
                ? normProgress(explicit)
                : pg !== undefined && tt
                  ? Math.min(100, (pg / tt) * 100)
                  : undefined,
          });
        },
        stage_change: (d) => patch(id, { stage: str(d.stage) }),
        degrade: (d) => patch(id, { degraded: str(d.level) ?? "L1" }),
        done: () => void refresh(),
        failed: () => void refresh(),
        onEnd: () => {
          if (!disposed) setStreaming((prev) => prev.filter((x) => x !== id));
        },
      }),
    );
    return () => {
      disposed = true;
      offs.forEach((off) => off());
    };
  }, [runningIds, client, refresh]);

  const degraded = runningIds.length > 0 && streaming.length === 0;

  // ---------------- 导入 ----------------

  useEffect(() => {
    if (incoming.seq === 0 || incoming.seq === lastIncoming.current) return;
    lastIncoming.current = incoming.seq;
    setImportErr(null);
    setConfirmList(incoming.entries);
  }, [incoming]);

  const pickFiles = async () => {
    try {
      const paths = await window.df.dialog.pickFiles();
      if (!paths.length) return;
      const entries = await window.df.files.expandPaths(paths);
      const docsOnly = entries.filter((e) => !e.isKmod);
      if (!docsOnly.length) {
        toast("没有找到可导入的文件", "err");
        return;
      }
      setImportErr(null);
      setConfirmList(docsOnly);
    } catch {
      toast("选择文件失败，请重试", "err");
    }
  };

  const onDrop = async (e: DragEvent) => {
    e.preventDefault();
    /* 阻止冒泡到 App 的全局接管，否则同一批文件会被处理两次 */
    e.stopPropagation();
    const files = Array.from(e.dataTransfer.files ?? []);
    if (!files.length) return;
    try {
      const paths = files.map((f) => window.df.files.pathForFile(f)).filter(Boolean);
      const entries = await window.df.files.expandPaths(paths);
      const docsOnly = entries.filter((e2) => !e2.isKmod);
      if (!docsOnly.length) {
        toast("拖入的内容里没有可导入的文件", "err");
        return;
      }
      setImportErr(null);
      setConfirmList(docsOnly);
    } catch {
      toast("读取拖入的文件失败，请重试", "err");
    }
  };

  /* 导入端点只有批量形态（routes_documents.import_documents）：
   * 请求 {paths}，响应 {imported, skipped, items(=imported), total}。
   * 这里不为「单文件形态」留退路——请求体键名写错时引擎会照常回 200 且 imported 为空，
   * 退路只会把「一个都没导进去」粉饰成「已导入 N 个文件」，失败还不如直接抛出来。 */
  const importPaths = async (
    paths: string[],
  ): Promise<{ items: ImportResult[]; skipped: ImportSkip[] }> => {
    const resp = await client.postJson<unknown>("/documents/import", { paths });
    const { items } = unwrapItems<ImportResult>(resp);
    const bag = (resp ?? {}) as Record<string, unknown>;
    const skipped = Array.isArray(bag["skipped"]) ? (bag["skipped"] as ImportSkip[]) : [];
    return { items, skipped };
  };

  const startImport = async () => {
    const list = confirmList ?? [];
    const usable = list.filter((e) => e.supported);
    if (!usable.length) {
      setConfirmList(null);
      return;
    }
    setImporting(true);
    setImportErr(null);
    try {
      const { items: results, skipped: engineSkipped } = await importPaths(usable.map((e) => e.path));
      let dup = 0;
      let queued = 0;
      for (const r of results) {
        if (r.duplicate_of) dup += 1;
        if (!r.doc_id) continue;
        if (r.task_id) {
          queued += 1; // 引擎已自动建解析任务；队列暂停时它会原地等待，无需取消重建
          continue;
        }
        await client.postJson<{ task_id: string }>("/tasks", { type: "parse", payload: { doc_id: r.doc_id } });
        queued += 1;
      }
      setConfirmList(null);
      const skipped = list.length - usable.length + engineSkipped.length;
      const parts = [`已导入 ${results.length} 个文件`];
      if (queued) parts.push(paused ? `${queued} 个解析任务已进队列（暂停中，等待恢复派发）` : `${queued} 个解析任务已进队列`);
      if (dup) parts.push(`${dup} 个与已有文档内容相同`);
      if (skipped) parts.push(`${skipped} 个已跳过`);
      toast(parts.join("，"), results.length ? "ok" : "err");
      /* 引擎侧的跳过原因比前端探测更准（含复制失败等），逐条提示一次 */
      for (const s of engineSkipped.slice(0, 3)) {
        if (s.reason) toast(`${s.name ?? "文件"}：${s.reason}`, "info");
      }
      void refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        setImportErr({ code: err.errorCode, detail: String(err.body?.["detail"] ?? err.message) });
      } else {
        setImportErr({ code: null, detail: "导入过程发生未知错误" });
      }
    } finally {
      setImporting(false);
    }
  };

  // ---------------- 队列操作 ----------------

  /* 暂停/恢复走引擎的持久开关（POST /queue/pause）：排队任务原地保留，
   * 正在处理的任务不打断；刷新页面、重启引擎都不会丢这个状态 */
  const setQueuePaused = async (next: boolean) => {
    try {
      const resp = await client.postJson<{ paused?: boolean }>("/queue/pause", { paused: next });
      setPaused(resp.paused === true);
      toast(
        next
          ? "已暂停派发：排队任务原地等待，正在处理的会跑完"
          : queuedCnt
            ? `已恢复派发，${queuedCnt} 个排队任务继续`
            : "已恢复派发",
        next ? "info" : "ok",
      );
    } catch {
      toast(next ? "暂停失败，请稍后再试" : "恢复失败，请稍后再试", "err");
    }
    void refresh();
  };

  const retryTask = async (t: TaskRow) => {
    try {
      const payload = parseJsonSafe<Record<string, unknown>>(t.payload_json, t.doc_id ? { doc_id: t.doc_id } : {});
      await client.postJson<{ task_id: string }>("/tasks", { type: t.type, payload });
      toast("已重新加入队列", "ok");
      closeDetail();
      void refresh();
    } catch {
      toast("重试失败，请稍后再试", "err");
    }
  };

  const cancelTask = async (t: TaskRow) => {
    try {
      await client.postJson(`/tasks/${encodeURIComponent(t.id)}/cancel`);
      toast("已请求取消，正在收尾", "info");
      void refresh();
    } catch {
      toast("取消失败，请稍后再试", "err");
    }
  };

  /* 关抽屉同时作废在途请求，令牌比对才拦得住「关掉后又被弹开」 */
  const closeDetail = () => {
    detailToken.current += 1;
    setDetail(null);
  };

  const openDetail = async (t: TaskRow) => {
    const token = ++detailToken.current;
    setDetail(t);
    setDetailLogs([]);
    try {
      const full = await client.getJson<TaskDetail>(`/tasks/${encodeURIComponent(t.id)}`);
      if (token !== detailToken.current) return;
      setDetail({ ...t, ...full });
      if (Array.isArray(full.events)) setDetailLogs(full.events.slice(0, 50));
    } catch {
      /* 详情取不到就用列表行的字段渲染，不影响主要信息 */
    }
    try {
      const logs = await client.getJson<unknown>(`/logs?task_id=${encodeURIComponent(t.id)}&page=1`);
      if (token !== detailToken.current) return;
      const { items } = unwrapItems<LogRow>(logs);
      if (items.length) setDetailLogs(items.slice(0, 50));
    } catch {
      /* 日志端点不可用时静默 */
    }
  };

  // ---------------- 渲染 ----------------

  const summary = useMemo(() => {
    let ok = 0;
    let warn = 0;
    let fail = 0;
    let active = 0;
    for (const t of tasks) {
      const d = t.doc_id ? docs[t.doc_id] : undefined;
      if (t.status === "running" || t.status === "queued") active += 1;
      else if (t.status === "failed") fail += 1;
      else if (t.status === "done") (d?.status === "warning" ? warn++ : ok++);
    }
    return { ok, warn, fail, active };
  }, [tasks, docs]);

  const unsupported = (confirmList ?? []).filter((e) => !e.supported);
  const supported = (confirmList ?? []).filter((e) => e.supported);

  return (
    <div className="page page-workbench">
      <section
        className="dropzone"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => void onDrop(e)}
        onClick={() => void pickFiles()}
        onKeyDown={(e) => {
          /* 内部「选择文件」按钮的回车/空格留给按钮自己，否则会连开两次选择框 */
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") void pickFiles();
        }}
        role="button"
        tabIndex={0}
        aria-label="拖拽或点击选择文件导入"
      >
        <span className="dropzone-icon">
          <svg viewBox="0 0 28 28" width="26" height="26" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
            <path d="M14 17.5V5.5m0 0l-4.5 4.5M14 5.5l4.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M4.5 17v3.2a2.3 2.3 0 002.3 2.3h14.4a2.3 2.3 0 002.3-2.3V17" strokeLinecap="round" />
          </svg>
        </span>
        <div className="dropzone-title">把文件拖进来，导入即开始解析</div>
        <div className="dropzone-hint">支持 Word（doc/docx）、PDF、PPT（ppt/pptx）、Excel（xls/xlsx）；可整个文件夹拖入</div>
        <div className="dropzone-actions">
          <button
            className="btn btn-primary"
            onClick={(e) => {
              e.stopPropagation();
              void pickFiles();
            }}
          >
            选择文件…
          </button>
        </div>
      </section>

      <section className="queue">
        <header className="queue-head">
          <h2 className="section-title">任务队列</h2>
          <div className="queue-summary">
            {summary.active > 0 && <span className="qs qs-run">{summary.active} 进行中</span>}
            {summary.ok > 0 && <span className="qs qs-ok">{summary.ok} 成功</span>}
            {summary.warn > 0 && <span className="qs qs-warn">{summary.warn} 警告</span>}
            {summary.fail > 0 && <span className="qs qs-err">{summary.fail} 失败</span>}
          </div>
          <div className="queue-actions">
            {degraded && <span className="hint-dim" title="进度流已断开，改用 5 秒刷新">进度已降级为定时刷新</span>}
            {paused ? (
              <button className="btn btn-sm btn-primary" onClick={() => void setQueuePaused(false)}>
                继续派发{queuedCnt ? `（${queuedCnt}）` : ""}
              </button>
            ) : (
              <button
                className="btn btn-sm"
                onClick={() => void setQueuePaused(true)}
                title="排队中的任务原地等待，正在处理的会跑完；重启应用暂停状态也会保留"
              >
                暂停队列
              </button>
            )}
            <button className="btn btn-sm" onClick={() => void refresh()}>刷新</button>
          </div>
        </header>

        {paused && (
          <div className="banner banner-warn">
            队列已暂停：{queuedCnt ? `${queuedCnt} 个排队任务原地等待，` : ""}新导入的文件也会先排队等着；
            正在处理的任务仍会跑完。暂停状态在重启后依然保留，点「继续派发」恢复。
          </div>
        )}
        {loadFailed && (
          <div className="banner banner-warn">暂时读不到任务列表，正在等待本地引擎就绪…</div>
        )}

        {tasks.length === 0 ? (
          <EmptyState title="还没有任务" hint="把文件拖到上面的区域，导入后会自动开始解析" />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th className="col-name">文件</th>
                  <th className="col-type">任务</th>
                  <th className="col-status">状态</th>
                  <th className="col-progress">进度</th>
                  <th className="col-time">耗时</th>
                  <th className="col-ops">操作</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => {
                  const doc = t.doc_id ? docs[t.doc_id] : undefined;
                  const name = doc?.name ?? taskLabel(t);
                  const info = live[t.id];
                  const pct =
                    t.status === "done" ? 100
                    : info?.progress !== undefined ? info.progress
                    : normProgress(t.progress);
                  const stage = info?.stage ?? t.stage ?? undefined;
                  const running = t.status === "running";
                  return (
                    <tr
                      key={t.id}
                      onClick={() => void openDetail(t)}
                      /* 整行点开详情的键盘等价物。行内还有「取消/重试/详情」按钮，它们的
                       * 回车/空格必须留给按钮自己——所以只认焦点确实落在行本身上的按键，
                       * 否则用键盘按「取消」会连带弹出详情抽屉。
                       * 不给 role="button"：tr 的隐式 role="row" 是表格结构的一部分，
                       * 改掉它整行就从表格的无障碍树里掉出去了，列头对应关系全丢。
                       * 焦点环走 styles.css 里统一的 :focus-visible，不在这儿另写。 */
                      onKeyDown={(e) => {
                        if (e.target !== e.currentTarget) return;
                        if (e.key !== "Enter" && e.key !== " ") return;
                        e.preventDefault(); // 空格默认会滚动 table-wrap
                        void openDetail(t);
                      }}
                      tabIndex={0}
                      className={t.status === "failed" ? "row-failed" : ""}
                    >
                      <td className="col-name">
                        <FmtIcon ext={doc?.fmt ?? extOf(name)} />
                        <span className="ellipsis" title={name}>{name}</span>
                      </td>
                      <td className="col-type">{TASK_TYPE_LABEL[t.type] ?? t.type}</td>
                      <td className="col-status">
                        <TaskStatusBadge
                          status={t.status}
                          docWarning={t.status === "done" && doc?.status === "warning"}
                          runningLabel={stage ? STAGE_LABEL[stage] : undefined}
                        />
                        {info?.degraded && <span className="tag-degrade" title="该文档部分页使用了降级解析">降级 {info.degraded}</span>}
                      </td>
                      <td className="col-progress">
                        <ProgressBar
                          value={pct}
                          state={t.status === "failed" ? "err" : t.status === "done" ? "ok" : "run"}
                          slim
                        />
                        {running && info?.total ? (
                          <span className="progress-note">第 {info.page ?? 0}/{info.total} 页</span>
                        ) : null}
                      </td>
                      <td className="col-time">{fmtDuration(t.started_at ?? t.created_at, t.ended_at)}</td>
                      <td className="col-ops" onClick={(e) => e.stopPropagation()}>
                        {(t.status === "queued" || t.status === "running") && (
                          <button className="btn btn-sm" onClick={() => void cancelTask(t)}>取消</button>
                        )}
                        {(t.status === "failed" || t.status === "canceled" || t.status === "interrupted") && (
                          <button className="btn btn-sm" onClick={() => void retryTask(t)}>重试</button>
                        )}
                        <button className="btn btn-sm btn-ghost" onClick={() => void openDetail(t)}>详情</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <Pagination page={pageNo} total={total} pageSize={PAGE_SIZE} onChange={setPageNo} />
      </section>

      {/* 导入确认清单（07 章：拖入时标出不支持项） */}
      <Modal
        open={confirmList !== null}
        title="确认导入"
        onClose={() => setConfirmList(null)}
        width={620}
        footer={
          <>
            <button className="btn" onClick={() => setConfirmList(null)}>取消</button>
            <button
              className="btn btn-primary"
              disabled={importing || supported.length === 0}
              onClick={() => void startImport()}
            >
              {importing ? "导入中…" : `导入并解析（${supported.length}）`}
            </button>
          </>
        }
      >
        {importErr ? (
          <ErrorDetail code={importErr.code} detail={importErr.detail} />
        ) : (
          <>
            <p className="modal-lead">
              共 {(confirmList ?? []).length} 项，其中 {supported.length} 项可以处理
              {unsupported.length > 0 ? `，${unsupported.length} 项暂不支持` : ""}。
            </p>
            <ul className="file-list file-list-scroll">
              {(confirmList ?? []).map((e) => (
                <li key={e.path} className={`file-row ${e.supported ? "" : "file-row-bad"}`}>
                  <FmtIcon ext={e.ext} />
                  <span className="file-name ellipsis" title={e.path}>{e.name}</span>
                  <span className="file-size">{fmtBytes(e.size)}</span>
                  {!e.supported && <span className="file-bad-tag">不支持</span>}
                </li>
              ))}
            </ul>
            {unsupported.length > 0 && (
              <p className="modal-note">不支持的文件会被跳过；旧格式可先用原程序另存为新格式再导入。</p>
            )}
          </>
        )}
      </Modal>

      {/* 任务详情抽屉（失败时展示阶段时间线 + 错误三级呈现） */}
      <Drawer
        open={detail !== null}
        title={detail ? `${TASK_TYPE_LABEL[detail.type] ?? detail.type}任务详情` : ""}
        onClose={closeDetail}
        width={460}
        footer={
          detail && (
            <>
              <button className="btn" onClick={closeDetail}>关闭</button>
              {detail.doc_id && (
                <button
                  className="btn"
                  onClick={() => {
                    navigate("library", { doc_id: detail.doc_id ?? "" });
                    closeDetail();
                  }}
                >
                  查看文档
                </button>
              )}
              {(detail.status === "failed" || detail.status === "canceled" || detail.status === "interrupted") && (
                <button className="btn btn-primary" onClick={() => void retryTask(detail)}>重试</button>
              )}
            </>
          )
        }
      >
        {detail && (
          <div className="drawer-section">
            <dl className="kv">
              <dt>文件</dt>
              <dd className="ellipsis">{(detail.doc_id && docs[detail.doc_id]?.name) || taskLabel(detail)}</dd>
              <dt>状态</dt>
              <dd><TaskStatusBadge status={detail.status} /></dd>
              <dt>创建</dt>
              <dd>{fmtTime(detail.created_at)}</dd>
              <dt>耗时</dt>
              <dd>{fmtDuration(detail.started_at ?? detail.created_at, detail.ended_at)}</dd>
            </dl>

            {detail.status === "failed" || detail.error_code ? (
              <ErrorDetail
                code={detail.error_code}
                detail={detailLogs.find((l) => l.level === "error")?.detail_json ?? undefined}
                taskId={detail.id}
                docId={detail.doc_id}
                fileName={detail.doc_id ? docs[detail.doc_id]?.name : undefined}
                logs={detailLogs}
                steps={buildStageSteps(detail)}
                onRetry={() => void retryTask(detail)}
                onViewInLogs={() => {
                  navigate("logs", { task_id: detail.id });
                  closeDetail();
                }}
              />
            ) : (
              <>
                <div className="drawer-subtitle">处理阶段</div>
                <StageTimeline steps={buildStageSteps(detail)} />
                {detailLogs.length > 0 && (
                  <>
                    <div className="drawer-subtitle">最近事件</div>
                    <div className="mini-logs">
                      {detailLogs.slice(0, 20).map((l) => (
                        <div key={l.id} className={`mini-log log-${l.level}`}>
                          <span className="mono">{fmtTime(l.ts)}</span>
                          {l.code && <span className="mini-log-code">{l.code}</span>}
                          <span className="ellipsis" title={l.message}>{l.message}</span>
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}

/* 任务行的兜底名字：文档尚未入表（如模组安装）时从 payload 里取一个能认人的字段 */
function taskLabel(t: TaskRow): string {
  const payload = parseJsonSafe<Record<string, unknown>>(t.payload_json, {});
  for (const key of ["name", "kmod_path", "out_dir"]) {
    const v = payload[key];
    if (typeof v === "string" && v) return v.replace(/^.*[\\/]/, "");
  }
  const ids = payload["doc_ids"];
  if (Array.isArray(ids)) return `${ids.length} 个文档`;
  return t.doc_id ?? t.id;
}
