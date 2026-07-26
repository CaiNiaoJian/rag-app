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
        <rect x="22" y="14" width="60" height="72" rx="6" fill="oklch(0.962 0.006 265)" stroke="oklch(0.906 0.008 265)" />
        <rect x="34" y="6" width="60" height="72" rx="6" fill="oklch(0.995 0.002 265)" stroke="oklch(0.906 0.008 265)" />
        <line x1="44" y1="24" x2="84" y2="24" stroke="oklch(0.855 0.011 265)" strokeWidth="3" strokeLinecap="round" />
        <line x1="44" y1="36" x2="84" y2="36" stroke="oklch(0.925 0.007 265)" strokeWidth="3" strokeLinecap="round" />
        <line x1="44" y1="48" x2="72" y2="48" stroke="oklch(0.925 0.007 265)" strokeWidth="3" strokeLinecap="round" />
        <circle cx="92" cy="66" r="17" fill="oklch(0.955 0.028 265)" stroke="oklch(0.545 0.176 265)" />
        <path d="M92 58v12m0 0l-5-5m5 5l5-5" stroke="oklch(0.545 0.176 265)" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      </svg>
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint">{hint}</div>}
      {children}
    </div>
  );
}
