/* contextBridge 暴露 window.df（02 章 §7：renderer 一切能力经受控 API）。
 *
 * 形状由 src/renderer/src/env.d.ts 的 DfApi **冻结**：这里用 `const api: DfApi`
 * 让编译器逐字段校验，多一个少一个都过不了 typecheck。
 *
 * 沙箱 preload 的两条硬约束：
 * - 只能是 CommonJS（见 electron.vite.config.ts 把本文件打成 index.cjs）；
 * - 除 electron 外几乎没有 Node 模块可用，所以 fileUrl 的路径转 URL 是手写的，
 *   不 require('url')（沙箱下 node: 前缀是否可用不稳定，不赌）。
 *
 * 通道名与 main/ipc.ts 的 CHANNELS 必须逐字一致（两侧分别打包，无法共享常量）。
 */

import { contextBridge, ipcRenderer, webUtils, type IpcRendererEvent } from "electron";

const CH = {
  engineGetInfo: "df:engine:get-info",
  engineRestart: "df:engine:restart",
  engineStatus: "df:engine-status",
  dialogPickFiles: "df:dialog:pick-files",
  dialogPickDirectory: "df:dialog:pick-directory",
  dialogPickSavePath: "df:dialog:pick-save-path",
  shellOpenPath: "df:shell:open-path",
  shellShowItemInFolder: "df:shell:show-item-in-folder",
  filesNotePath: "df:files:note-path",
  filesExpandPaths: "df:files:expand-paths",
  filesReadText: "df:files:read-text",
  pdfPrintHtmlToPdf: "df:pdf:print-html-to-pdf",
  diagnosticsExportZip: "df:diagnostics:export-zip",
  appVersions: "df:app:versions",
  appUninstall: "df:app:uninstall",
  updateCheck: "df:update:check",
  updateOpenDownload: "df:update:open-download",
} as const;

/** 单个路径段的转义：盘符段（"G:"）原样保留，其余按 URI 组件编码（中文/空格/#/?） */
function encodeSegment(seg: string): string {
  if (/^[A-Za-z]:$/.test(seg)) return seg.toUpperCase();
  return encodeURIComponent(seg);
}

/**
 * 本地路径 → file:// URL。
 * 手写而非 pathToFileURL：沙箱 preload 里 Node 模块可用面很窄。
 * 覆盖三种形态：盘符路径、UNC 路径、以及已经是 file: URL 的原样返回。
 */
function toFileUrl(p: string): string {
  if (/^file:/i.test(p)) return p;
  const norm = p.replace(/\\/g, "/");
  if (norm.startsWith("//")) {
    // UNC：\\host\share\path → file://host/share/path
    const parts = norm.slice(2).split("/").filter(Boolean);
    const host = parts.shift() ?? "";
    return `file://${encodeURIComponent(host)}/${parts.map(encodeSegment).join("/")}`;
  }
  return `file:///${norm.split("/").filter(Boolean).map(encodeSegment).join("/")}`;
}

const api: DfApi = {
  engine: {
    getInfo: () => ipcRenderer.invoke(CH.engineGetInfo) as Promise<DfEngineInfo>,
    onStatusChange: (cb) => {
      const listener = (_event: IpcRendererEvent, info: DfEngineInfo): void => cb(info);
      ipcRenderer.on(CH.engineStatus, listener);
      return () => {
        ipcRenderer.removeListener(CH.engineStatus, listener);
      };
    },
    restart: () => ipcRenderer.invoke(CH.engineRestart) as Promise<void>,
  },

  dialog: {
    pickFiles: () => ipcRenderer.invoke(CH.dialogPickFiles) as Promise<string[]>,
    pickDirectory: () => ipcRenderer.invoke(CH.dialogPickDirectory) as Promise<string | null>,
    pickSavePath: (defaultName) =>
      ipcRenderer.invoke(CH.dialogPickSavePath, defaultName) as Promise<string | null>,
  },

  shell: {
    openPath: (p) => ipcRenderer.invoke(CH.shellOpenPath, p) as Promise<void>,
    showItemInFolder: (p) => ipcRenderer.invoke(CH.shellShowItemInFolder, p) as Promise<void>,
  },

  files: {
    /* Electron 32+ 移除了 File.path，改用 webUtils.getPathForFile（同步）。
     * 顺手把拖拽得到的路径登记到主进程白名单，后续 readText 才放行。 */
    pathForFile: (f) => {
      const p = webUtils.getPathForFile(f);
      if (p) ipcRenderer.send(CH.filesNotePath, p);
      return p;
    },
    expandPaths: (paths) =>
      ipcRenderer.invoke(CH.filesExpandPaths, paths) as Promise<DfPathEntry[]>,
    readText: (p) => ipcRenderer.invoke(CH.filesReadText, p) as Promise<string>,
    fileUrl: (p) => toFileUrl(p),
  },

  pdf: {
    printHtmlToPdf: (htmlPath, outPdfPath) =>
      ipcRenderer.invoke(CH.pdfPrintHtmlToPdf, htmlPath, outPdfPath) as Promise<void>,
  },

  diagnostics: {
    exportZip: () => ipcRenderer.invoke(CH.diagnosticsExportZip) as Promise<string | null>,
  },

  appInfo: {
    versions: () =>
      ipcRenderer.invoke(CH.appVersions) as Promise<{
        app: string;
        electron: string;
        chrome: string;
        node: string;
      }>,
  },

  appControl: {
    uninstall: () =>
      ipcRenderer.invoke(CH.appUninstall) as Promise<{ ok: boolean; reason: string | null }>,
  },

  update: {
    check: () => ipcRenderer.invoke(CH.updateCheck) as Promise<DfUpdateInfo>,
    openDownload: (url) => ipcRenderer.invoke(CH.updateOpenDownload, url) as Promise<void>,
  },
};

contextBridge.exposeInMainWorld("df", api);
