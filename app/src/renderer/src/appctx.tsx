/* 全局上下文：引擎客户端、引擎状态、页面导航（无路由库，页内切换）与轻提示。
 * 六页常驻挂载（访问过即保活），跨页跳转经 navigate 传参（如日志页带 task_id 过滤）。
 */

import { createContext, useContext } from "react";
import type { EngineClient } from "./api";

export type PageId = "workbench" | "library" | "export" | "dashboard" | "logs" | "settings";

export const PAGE_TITLES: Record<PageId, string> = {
  workbench: "工作台",
  library: "文档库",
  export: "导出中心",
  dashboard: "仪表盘",
  logs: "日志",
  settings: "设置",
};

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface NavRequest {
  /* 单调递增序号：同参数重复跳转也能触发目标页响应 */
  seq: number;
  page: PageId;
  params: Record<string, string>;
}

/* 界面外观（本地偏好，存 localStorage，与引擎设置无关） */
export type ThemeChoice = "system" | "light" | "dark";
export type DensityChoice = "compact" | "comfortable";

export interface AppContextValue {
  client: EngineClient;
  engine: DfEngineInfo | null;
  page: PageId;
  nav: NavRequest;
  navigate: (page: PageId, params?: Record<string, string>) => void;
  toast: (msg: string, kind?: "info" | "ok" | "err", action?: ToastAction) => void;
  theme: ThemeChoice;
  setTheme: (t: ThemeChoice) => void;
  density: DensityChoice;
  setDensity: (d: DensityChoice) => void;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const v = useContext(AppContext);
  if (!v) throw new Error("AppContext 未初始化");
  return v;
}
