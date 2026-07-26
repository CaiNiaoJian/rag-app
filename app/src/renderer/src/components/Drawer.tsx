/* 右侧抽屉：任务/文档详情统一交互（07 章约定——详情不打断队列视图，不用弹窗）。 */

import { useEffect, type ReactNode } from "react";

export function Drawer({ open, title, onClose, children, width = 440, footer }: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  width?: number;
  footer?: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="drawer-overlay" onMouseDown={onClose}>
      <aside
        className="drawer"
        style={{ width }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="drawer-head">
          <div className="drawer-title">{title}</div>
          <button className="icon-btn" onClick={onClose} title="关闭" aria-label="关闭">
            <svg viewBox="0 0 16 16" width="14" height="14"><path d="M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
          </button>
        </header>
        <div className="drawer-body">{children}</div>
        {footer && <footer className="drawer-foot">{footer}</footer>}
      </aside>
    </div>
  );
}
