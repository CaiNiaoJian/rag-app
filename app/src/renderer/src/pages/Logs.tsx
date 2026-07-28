/* 日志（07 章 §1 第 5 页）：时间倒序 + 级别筛选 + 搜索 + 关联任务定位 + 导出诊断包。
 *
 * 设计取舍：
 * - **面向非技术用户的第一列是「说明」而不是「代码」**：错误码收在右侧小标签里，
 *   真正要排查时点开右侧抽屉看错误三级呈现。
 * - **诊断包走主进程**：window.df.diagnostics.exportZip() 由 Electron 侧选保存位置并落盘，
 *   渲染进程不碰文件系统写操作（沙箱约束，02 章 §7）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useApp } from "../appctx";
import { Badge, TaskStatusBadge } from "../components/Badge";
import { Drawer } from "../components/Drawer";
import { EmptyState } from "../components/EmptyState";
import { ErrorDetail, StageTimeline, buildStageSteps } from "../components/ErrorDetail";
import { Pagination } from "../components/Pagination";
import type { LogRow, TaskRow } from "../types";
import { STAGE_LABEL, TASK_TYPE_LABEL } from "../types";
import { fmtDuration, fmtTime } from "../util";

const PAGE_SIZE = 100;

const LEVEL_LABEL: Record<string, string> = { info: "信息", warning: "警告", error: "错误" };

function unwrapItems<T>(resp: unknown): { items: T[]; total: number } {
  if (Array.isArray(resp)) return { items: resp as T[], total: resp.length };
  if (resp && typeof resp === "object") {
    const r = resp as Record<string, unknown>;
    for (const key of ["items", "logs", "events", "results"]) {
      const v = r[key];
      if (Array.isArray(v)) {
        return { items: v as T[], total: typeof r.total === "number" ? r.total : v.length };
      }
    }
  }
  return { items: [], total: 0 };
}

export function Logs() {
  const { client, page: activePage, nav, navigate, toast } = useApp();

  const [rows, setRows] = useState<LogRow[]>([]);
  const [total, setTotal] = useState(0);
  const [pageNo, setPageNo] = useState(1);
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const [taskFilter, setTaskFilter] = useState("");
  const [codeFilter, setCodeFilter] = useState("");
  const [busy, setBusy] = useState(false);

  const [task, setTask] = useState<TaskRow | null>(null);
  const [taskLogs, setTaskLogs] = useState<LogRow[]>([]);

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams({ page: String(pageNo) });
      if (level) params.set("level", level);
      if (taskFilter) params.set("task_id", taskFilter);
      /* 错误码借道 q 下发：/logs 没有独立的 code 参数，但它的 q 落到 SQL 是
       * `message LIKE %q% OR code LIKE %q%`（db.query_events），传 code 就能让筛选发生在库里。
       * 这一步是必须的——只在前端 filter 的话，用户从仪表盘「失败原因 TOP」下钻进来，
       * 看到的只是当前这 100 条里碰巧含该码的几条，往后翻页也永远补不齐。
       * q 只有一个坑位：code 与用户关键词同时存在时优先下发 code（选择性高得多），
       * 关键词退回本地收敛（见下方 visible），免得静默丢掉其中一个条件。*/
      if (codeFilter) params.set("q", codeFilter);
      else if (q.trim()) params.set("q", q.trim());
      const resp = await client.getJson<unknown>(`/logs?${params.toString()}`);
      const { items, total: n } = unwrapItems<LogRow>(resp);
      setRows(items);
      setTotal(n);
    } catch {
      setRows([]);
      setTotal(0);
    }
  }, [client, pageNo, level, q, taskFilter, codeFilter]);

  /* 与文档库一致：搜索词变化延后 200ms 合并请求 */
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 200);
    return () => window.clearTimeout(timer);
  }, [load]);

  /* 日志页常驻挂载：只在可见时定时刷新，避免后台空转 */
  useEffect(() => {
    if (activePage !== "logs") return;
    const timer = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(timer);
  }, [activePage, load]);

  /* 接收跨页跳转参数：任务详情的「在日志中查看」、仪表盘失败 TOP 的下钻 */
  useEffect(() => {
    if (nav.page !== "logs") return;
    const t = nav.params["task_id"];
    const lv = nav.params["level"];
    const code = nav.params["code"];
    const kw = nav.params["q"];
    setPageNo(1);
    setTaskFilter(t ?? "");
    setCodeFilter(code ?? "");
    if (lv) setLevel(lv);
    if (kw !== undefined) setQ(kw);
  }, [nav]);

  /* 服务端按 code 下发的是超集：q 也会 LIKE 到 message 里恰好提到该码的行，这里收敛成精确相等；
   * 顺手补上被 code 挤掉坑位的关键词，让「下钻 + 再输关键词」两个条件都还在。
   * 已知残留：Pagination 用的 total 是服务端超集计数，页数可能虚高（最多多出几行的量）。
   * 要彻底对齐需引擎侧给 GET /logs 补精确的 code 参数（routes_logs.list_logs 加一个 Query，
   * db.query_events 里加一条 `code=?`），那是引擎的活，本次不动。*/
  const visible = useMemo(() => {
    if (!codeFilter) return rows;
    const kw = q.trim().toLowerCase();
    return rows.filter(
      (r) => (r.code ?? "") === codeFilter && (!kw || r.message.toLowerCase().includes(kw)),
    );
  }, [rows, codeFilter, q]);

  const openTask = async (taskId: string) => {
    try {
      const t = await client.getJson<TaskRow>(`/tasks/${encodeURIComponent(taskId)}`);
      setTask(t);
      const resp = await client.getJson<unknown>(`/logs?task_id=${encodeURIComponent(taskId)}&page=1`);
      setTaskLogs(unwrapItems<LogRow>(resp).items.slice(0, 50));
    } catch {
      toast("读取关联任务失败", "err");
    }
  };

  const exportDiagnostics = async () => {
    setBusy(true);
    try {
      /* 优先让引擎打包（它更清楚哪些文件属于诊断范围）；
       * preload 的 exportZip 负责选保存位置与落盘 */
      const p = await window.df.diagnostics.exportZip();
      if (p) {
        toast("诊断包已导出", "ok", { label: "打开所在位置", onClick: () => void window.df.shell.showItemInFolder(p) });
      }
    } catch {
      toast("导出诊断包失败", "err");
    } finally {
      setBusy(false);
    }
  };

  const clearFilters = () => {
    setLevel("");
    setQ("");
    setTaskFilter("");
    setCodeFilter("");
    setPageNo(1);
  };

  const filtered = Boolean(taskFilter || codeFilter || level || q.trim());

  return (
    <div className="page page-logs">
      <div className="filter-bar">
        <input
          className="input"
          type="search"
          value={q}
          placeholder="搜索文件名 / 消息内容"
          aria-label="搜索日志"
          onChange={(e) => {
            setQ(e.target.value);
            setPageNo(1);
          }}
        />
        <select
          className="select"
          value={level}
          aria-label="按级别筛选"
          onChange={(e) => {
            setLevel(e.target.value);
            setPageNo(1);
          }}
        >
          <option value="">全部级别</option>
          <option value="info">信息</option>
          <option value="warning">警告</option>
          <option value="error">错误</option>
        </select>
        {filtered && (
          <button className="btn btn-sm" onClick={clearFilters}>清除筛选</button>
        )}
        <span className="flex-spacer" />
        <button className="btn btn-sm" onClick={() => void load()}>刷新</button>
        <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => void exportDiagnostics()}>
          {busy ? "打包中…" : "导出诊断包"}
        </button>
      </div>

      {(taskFilter || codeFilter) && (
        <div className="banner">
          正在查看
          {taskFilter && <> 任务 <code className="mono">{taskFilter}</code></>}
          {codeFilter && <> 错误码 <b>{codeFilter}</b></>}
          {" "}的日志。
          <button className="btn btn-sm btn-ghost" onClick={clearFilters}>查看全部</button>
        </div>
      )}

      {visible.length === 0 ? (
        <EmptyState title="没有日志记录" hint="处理文件时产生的事件会出现在这里" />
      ) : (
        <div className="table-wrap">
          <table className="table table-logs">
            <thead>
              <tr>
                <th className="col-time">时间</th>
                <th className="col-level">级别</th>
                <th>说明</th>
                <th className="col-stage">阶段</th>
                <th className="col-ops">关联</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((l) => (
                <tr key={l.id} className={`log-${l.level}`}>
                  <td className="col-time mono">{fmtTime(l.ts)}</td>
                  <td className="col-level">
                    <Badge
                      kind={l.level === "error" ? "err" : l.level === "warning" ? "warn" : "neutral"}
                      text={LEVEL_LABEL[l.level] ?? l.level}
                    />
                  </td>
                  <td>
                    <span className="ellipsis" title={l.message}>{l.message}</span>
                    {l.code && <span className="log-code">{l.code}</span>}
                    {l.page !== null && l.page !== undefined && <span className="log-page">第 {l.page} 页</span>}
                  </td>
                  <td className="col-stage">{l.stage ? STAGE_LABEL[l.stage] ?? l.stage : "—"}</td>
                  <td className="col-ops">
                    {l.task_id ? (
                      <button className="btn btn-sm" onClick={() => void openTask(l.task_id as string)}>查看任务</button>
                    ) : l.doc_id ? (
                      <button className="btn btn-sm" onClick={() => navigate("library", { doc_id: l.doc_id as string })}>
                        查看文档
                      </button>
                    ) : (
                      <span className="text-dim">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Pagination page={pageNo} total={total} pageSize={PAGE_SIZE} onChange={setPageNo} />

      <Drawer
        open={task !== null}
        title={task ? `${TASK_TYPE_LABEL[task.type] ?? task.type}任务` : ""}
        onClose={() => setTask(null)}
        width={460}
        footer={
          task && (
            <>
              <button className="btn" onClick={() => setTask(null)}>关闭</button>
              {task.doc_id && (
                <button
                  className="btn"
                  onClick={() => {
                    navigate("library", { doc_id: task.doc_id as string });
                    setTask(null);
                  }}
                >
                  查看文档
                </button>
              )}
              <button
                className="btn btn-primary"
                onClick={() => {
                  setTaskFilter(task.id);
                  setPageNo(1);
                  setTask(null);
                }}
              >
                只看该任务日志
              </button>
            </>
          )
        }
      >
        {task && (
          <div className="drawer-section">
            <dl className="kv">
              <dt>状态</dt>
              <dd><TaskStatusBadge status={task.status} /></dd>
              <dt>创建</dt>
              <dd>{fmtTime(task.created_at)}</dd>
              <dt>耗时</dt>
              <dd>{fmtDuration(task.started_at ?? task.created_at, task.ended_at)}</dd>
            </dl>
            {task.status === "failed" || task.error_code ? (
              <ErrorDetail
                code={task.error_code}
                detail={taskLogs.find((l) => l.level === "error")?.detail_json ?? undefined}
                taskId={task.id}
                docId={task.doc_id}
                logs={taskLogs}
                steps={buildStageSteps(task)}
              />
            ) : (
              <>
                <div className="drawer-subtitle">处理阶段</div>
                <StageTimeline steps={buildStageSteps(task)} />
              </>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}
