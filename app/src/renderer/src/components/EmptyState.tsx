/* 空状态：内置 SVG 插画（离线零资源），文案面向非技术用户。
 * 插画取色全部走 CSS 变量：跟随主题深浅自动翻转，不留一处硬编码。 */

import type { ReactNode } from "react";

export function EmptyState({ title, hint, children }: {
  title: string;
  hint?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <svg viewBox="0 0 120 90" width="120" height="90" aria-hidden="true">
        <rect x="22" y="14" width="60" height="72" rx="8" fill="var(--surface-3)" stroke="var(--line)" />
        <rect x="34" y="6" width="60" height="72" rx="8" fill="var(--surface)" stroke="var(--line)" />
        <line x1="44" y1="24" x2="84" y2="24" stroke="var(--line-strong)" strokeWidth="3" strokeLinecap="round" />
        <line x1="44" y1="36" x2="84" y2="36" stroke="var(--line-soft)" strokeWidth="3" strokeLinecap="round" />
        <line x1="44" y1="48" x2="72" y2="48" stroke="var(--line-soft)" strokeWidth="3" strokeLinecap="round" />
        <circle cx="92" cy="66" r="17" fill="var(--brand-soft)" stroke="var(--brand)" />
        <path d="M92 58v12m0 0l-5-5m5 5l5-5" stroke="var(--brand)" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      </svg>
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint">{hint}</div>}
      {children}
    </div>
  );
}
