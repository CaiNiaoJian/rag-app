/* 空状态：内置 SVG 插画（离线零资源），文案面向非技术用户。 */

import type { ReactNode } from "react";

export function EmptyState({ title, hint, children }: {
  title: string;
  hint?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <svg viewBox="0 0 120 90" width="120" height="90" aria-hidden="true">
        <rect x="22" y="14" width="60" height="72" rx="6" fill="#EEF2F7" stroke="#D4DAE3" />
        <rect x="34" y="6" width="60" height="72" rx="6" fill="#FFFFFF" stroke="#D4DAE3" />
        <line x1="44" y1="24" x2="84" y2="24" stroke="#C3CBD6" strokeWidth="3" strokeLinecap="round" />
        <line x1="44" y1="36" x2="84" y2="36" stroke="#DCE2EA" strokeWidth="3" strokeLinecap="round" />
        <line x1="44" y1="48" x2="72" y2="48" stroke="#DCE2EA" strokeWidth="3" strokeLinecap="round" />
        <circle cx="92" cy="66" r="17" fill="#EAF1FE" stroke="#2467E5" />
        <path d="M92 58v12m0 0l-5-5m5 5l5-5" stroke="#2467E5" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      </svg>
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint">{hint}</div>}
      {children}
    </div>
  );
}
