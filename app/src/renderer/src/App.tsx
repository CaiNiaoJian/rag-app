/* 应用外壳：左侧图标导航 + 顶部窄工具条 + 六页常驻挂载（07 章 §1）。
 *
 * 几个刻意的设计取舍：
 * - **无路由库**：桌面工具只有六个平级页面，URL 概念对用户毫无意义；用 PageId 切换即可，
 *   也省掉一个依赖（package.json 冻结）。
 * - **访问过即保活**：页面首次访问后一直挂载，只用 CSS 隐藏。文档库的三栏预览、
 *   导出中心的勾选、日志的滚动位置在切走再切回时不该丢失；代价是常驻组件必须
 *   自己在不可见时降低轮询频率（各页用 useApp().page 判断）。
 * - **拖拽全局接管**：07 章用户流 C 要求「.kmod 拖到任意界面」都能识别，所以监听 window，
 *   工作台的拖拽区在自己的 drop 里 stopPropagation 以免重复处理。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, EngineClient } from "./api";
import { AppContext, PAGE_TITLES, type NavRequest, type PageId, type ToastAction } from "./appctx";
import { Modal } from "./components/Modal";
import { Workbench } from "./pages/Workbench";
import { Library } from "./pages/Library";
import { ExportCenter } from "./pages/ExportCenter";
import { Dashboard } from "./pages/Dashboard";
import { Logs } from "./pages/Logs";
import { Settings } from "./pages/Settings";
import { basename, fmtBytes } from "./util";

// ---------------- 左侧导航定义 ----------------

interface NavItem {
  id: PageId;
  icon: ReactNode;
}

/* 图标一律内联 SVG：离线应用不引图标字体，也避免额外资源请求 */
const NAV_ITEMS: NavItem[] = [
  {
    id: "workbench",
    icon: (
      <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path d="M10 2.6v9.2m0 0L6.4 8.2M10 11.8l3.6-3.6" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M3.2 13.4v2.2a1.8 1.8 0 001.8 1.8h10a1.8 1.8 0 001.8-1.8v-2.2" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    id: "library",
    icon: (
      <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="3" y="2.8" width="5" height="14.4" rx="1.2" />
        <rect x="10" y="2.8" width="7" height="14.4" rx="1.2" />
        <path d="M12 6.4h3M12 9h3" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    id: "export",
    icon: (
      <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path d="M10 12.6V3.4m0 0L6.6 6.8M10 3.4l3.4 3.4" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M3.4 12.2v3a1.6 1.6 0 001.6 1.6h10a1.6 1.6 0 001.6-1.6v-3" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    id: "dashboard",
    icon: (
      <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path d="M3.2 16.4h13.6" strokeLinecap="round" />
        <rect x="4.4" y="9.6" width="3" height="5" rx="0.8" />
        <rect x="8.6" y="5.6" width="3" height="9" rx="0.8" />
        <rect x="12.8" y="7.8" width="3" height="6.8" rx="0.8" />
      </svg>
    ),
  },
  {
    id: "logs",
    icon: (
      <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="3.6" y="2.8" width="12.8" height="14.4" rx="1.6" />
        <path d="M6.6 6.6h6.8M6.6 9.6h6.8M6.6 12.6h4.2" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    id: "settings",
    icon: (
      <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5">
        <circle cx="10" cy="10" r="2.6" />
        <path d="M10 2.6v1.8M10 15.6v1.8M17.4 10h-1.8M4.4 10H2.6M15.2 4.8l-1.3 1.3M6.1 13.9l-1.3 1.3M15.2 15.2l-1.3-1.3M6.1 6.1L4.8 4.8" strokeLinecap="round" />
      </svg>
    ),
  },
];

// ---------------- 轻提示 ----------------

interface ToastItem {
  id: number;
  msg: string;
  kind: "info" | "ok" | "err";
  action?: ToastAction;
}

// ---------------- 拖拽落地结果 ----------------

interface DropEntries {
  docs: DfPathEntry[];
  kmods: DfPathEntry[];
}

export function App() {
  const [client] = useState(() => new EngineClient());
  const [engine, setEngine] = useState<DfEngineInfo | null>(null);
  const [page, setPage] = useState<PageId>("workbench");
  const [visited, setVisited] = useState<Set<PageId>>(() => new Set<PageId>(["workbench"]));
  const [nav, setNav] = useState<NavRequest>({ seq: 0, page: "workbench", params: {} });
  const [expanded, setExpanded] = useState(false);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [search, setSearch] = useState("");
  const [dragging, setDragging] = useState(false);
  const [incoming, setIncoming] = useState<{ seq: number; entries: DfPathEntry[] }>({ seq: 0, entries: [] });
  const [kmodDrop, setKmodDrop] = useState<DfPathEntry[] | null>(null);
  const toastSeq = useRef(0);
  const navSeq = useRef(0);
  const dragDepth = useRef(0);

  const toast = useCallback((msg: string, kind: "info" | "ok" | "err" = "info", action?: ToastAction) => {
    const id = ++toastSeq.current;
    setToasts((prev) => [...prev, { id, msg, kind, action }]);
    /* 带操作按钮的提示留久一点，用户来得及点 */
    window.setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), action ? 8000 : 4000);
  }, []);

  const navigate = useCallback((target: PageId, params?: Record<string, string>) => {
    setPage(target);
    setVisited((prev) => (prev.has(target) ? prev : new Set(prev).add(target)));
    setNav({ seq: ++navSeq.current, page: target, params: params ?? {} });
  }, []);

  /* 引擎状态订阅：重启会换端口与 token，必须让 HTTP 客户端作废缓存 */
  useEffect(() => {
    if (typeof window.df === "undefined") return;
    let alive = true;
    void window.df.engine
      .getInfo()
      .then((info) => {
        if (alive) setEngine(info);
      })
      .catch(() => undefined);
    const off = window.df.engine.onStatusChange((info) => {
      client.invalidate();
      setEngine(info);
    });
    return () => {
      alive = false;
      off();
    };
  }, [client]);

  /* 全局拖拽：整窗接管，避免用户把文件丢在导航栏上什么也没发生 */
  useEffect(() => {
    if (typeof window.df === "undefined") return;

    const hasFiles = (e: DragEvent) =>
      Array.from(e.dataTransfer?.types ?? []).includes("Files");

    const onEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      dragDepth.current += 1;
      setDragging(true);
    };
    const onOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const onLeave = () => {
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) setDragging(false);
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (!files.length) return;
      void routeDrop(files);
    };
    /* 捕获阶段单独收拖拽视觉状态：工作台的拖拽区会 stopPropagation，
     * 冒泡阶段的 drop 收不到，不在这里清就会留下一层永久遮罩 */
    const onDropCapture = () => {
      dragDepth.current = 0;
      setDragging(false);
    };

    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragover", onOver);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("drop", onDrop);
    window.addEventListener("drop", onDropCapture, true);
    window.addEventListener("dragend", onDropCapture, true);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("drop", onDrop);
      window.removeEventListener("drop", onDropCapture, true);
      window.removeEventListener("dragend", onDropCapture, true);
    };
    /* 空依赖是刻意的：监听器只需注册一次；它闭包捕获的 navigate/toast 都是稳定的 useCallback */
  }, []);

  /* 拖入的东西分两路：.kmod 走模组安装确认，文档走工作台导入确认 */
  const routeDrop = async (files: File[]) => {
    try {
      const paths = files.map((f) => window.df.files.pathForFile(f)).filter(Boolean);
      if (!paths.length) return;
      const entries = await window.df.files.expandPaths(paths);
      const split: DropEntries = { docs: [], kmods: [] };
      for (const e of entries) (e.isKmod ? split.kmods : split.docs).push(e);
      if (split.kmods.length) {
        setKmodDrop(split.kmods);
        return;
      }
      if (!split.docs.length) {
        toast("没有找到可导入的文件", "err");
        return;
      }
      navigate("workbench");
      setIncoming((prev) => ({ seq: prev.seq + 1, entries: split.docs }));
    } catch {
      toast("读取拖入的文件失败，请重试", "err");
    }
  };

  const installKmods = async (list: DfPathEntry[]) => {
    setKmodDrop(null);
    for (const k of list) {
      try {
        await client.postJson<{ task_id: string }>("/modules/install", { kmod_path: k.path });
        toast(`已开始安装模组：${k.name}`, "ok", {
          label: "查看模组",
          onClick: () => navigate("settings", { tab: "modules" }),
        });
      } catch (err) {
        const msg = err instanceof ApiError ? err.userMessage ?? err.message : "模组安装失败";
        toast(msg, "err");
      }
    }
  };

  const ctx = useMemo(
    () => ({ client, engine, page, nav, navigate, toast }),
    [client, engine, page, nav, navigate, toast],
  );

  const engineState = engine?.status ?? "starting";
  const engineLabel =
    engineState === "ready" ? "引擎正常" : engineState === "starting" ? "引擎启动中" : "引擎已停止";

  return (
    <AppContext.Provider value={ctx}>
      {/* TODO(V1 收尾)：首启 3 步气泡引导（拖入导入 → 队列观察 → 导出中心），
          按 07 章 §1「无强制向导」，只做一次性气泡，不做模态向导 */}
      <div className={`shell ${expanded ? "shell-expanded" : ""}`}>
        <nav className="sidebar" aria-label="主导航">
          <button
            className="sidebar-toggle"
            onClick={() => setExpanded((v) => !v)}
            title={expanded ? "收起导航" : "展开导航"}
            aria-label={expanded ? "收起导航" : "展开导航"}
          >
            <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M4 6h12M4 10h12M4 14h12" strokeLinecap="round" />
            </svg>
            <span className="sidebar-text">DocFactory</span>
          </button>
          {NAV_ITEMS.map((it) => (
            <button
              key={it.id}
              className={`sidebar-item ${page === it.id ? "sidebar-item-active" : ""}`}
              onClick={() => navigate(it.id)}
              title={PAGE_TITLES[it.id]}
              aria-current={page === it.id ? "page" : undefined}
            >
              <span className="sidebar-icon">{it.icon}</span>
              <span className="sidebar-text">{PAGE_TITLES[it.id]}</span>
            </button>
          ))}
        </nav>

        <div className="main">
          <header className="topbar">
            <div className="topbar-title">{PAGE_TITLES[page]}</div>
            <div className="topbar-search">
              <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="7" cy="7" r="4.4" />
                <path d="M10.4 10.4L14 14" strokeLinecap="round" />
              </svg>
              <input
                type="search"
                value={search}
                placeholder="搜索文档名 / 日志内容"
                aria-label="全局搜索"
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key !== "Enter") return;
                  const q = search.trim();
                  if (!q) return;
                  /* 全局搜索默认落在文档库；日志页自己也接受 q 参数 */
                  navigate("library", { q });
                }}
              />
            </div>
            <div className={`engine-light engine-${engineState}`} title={engineLabel}>
              <span className="engine-dot" />
              <span className="engine-text">{engineLabel}</span>
              {engineState === "down" && (
                <button
                  className="btn btn-sm"
                  onClick={() => {
                    void window.df.engine.restart().catch(() => toast("重启引擎失败", "err"));
                  }}
                >
                  重启引擎
                </button>
              )}
            </div>
          </header>

          <div className="page-host">
            {/* 六页常驻：首次访问后保留实例，切页只切显隐 */}
            <PageSlot active={page === "workbench"} mounted>
              <Workbench incoming={incoming} />
            </PageSlot>
            <PageSlot active={page === "library"} mounted={visited.has("library")}>
              <Library />
            </PageSlot>
            <PageSlot active={page === "export"} mounted={visited.has("export")}>
              <ExportCenter />
            </PageSlot>
            <PageSlot active={page === "dashboard"} mounted={visited.has("dashboard")}>
              <Dashboard />
            </PageSlot>
            <PageSlot active={page === "logs"} mounted={visited.has("logs")}>
              <Logs />
            </PageSlot>
            <PageSlot active={page === "settings"} mounted={visited.has("settings")}>
              <Settings />
            </PageSlot>
          </div>
        </div>

        {dragging && (
          <div className="drag-overlay">
            <div className="drag-overlay-card">
              <div className="drag-overlay-title">松开即可导入</div>
              <div className="drag-overlay-hint">支持 Word / PDF / PPT / Excel；.kmod 将识别为模组更新包</div>
            </div>
          </div>
        )}

        <Modal
          open={kmodDrop !== null}
          title="安装模组更新包"
          onClose={() => setKmodDrop(null)}
          footer={
            <>
              <button className="btn" onClick={() => setKmodDrop(null)}>取消</button>
              <button className="btn btn-primary" onClick={() => void installKmods(kmodDrop ?? [])}>
                验签并安装
              </button>
            </>
          }
        >
          <p className="modal-lead">检测到离线模组包，安装完成后需要重启引擎才会生效。</p>
          <ul className="file-list">
            {(kmodDrop ?? []).map((k) => (
              <li key={k.path} className="file-row">
                <span className="fmt-icon fmt-kmod">kmod</span>
                <span className="file-name ellipsis" title={k.path}>{basename(k.name)}</span>
                <span className="file-size">{fmtBytes(k.size)}</span>
              </li>
            ))}
          </ul>
        </Modal>

        <div className="toast-host" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`toast toast-${t.kind}`}>
              <span className="toast-msg">{t.msg}</span>
              {t.action && (
                <button
                  className="toast-action"
                  onClick={() => {
                    t.action?.onClick();
                    setToasts((prev) => prev.filter((x) => x.id !== t.id));
                  }}
                >
                  {t.action.label}
                </button>
              )}
            </div>
          ))}
        </div>
      </div>
    </AppContext.Provider>
  );
}

/* 页面插槽：未访问过的页面不挂载（省首屏开销），访问过的只隐藏不卸载 */
function PageSlot({ active, mounted, children }: { active: boolean; mounted: boolean; children: ReactNode }) {
  if (!mounted) return null;
  return (
    <div className="page-slot" hidden={!active} aria-hidden={!active}>
      {children}
    </div>
  );
}
