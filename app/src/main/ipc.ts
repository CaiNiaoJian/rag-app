/* 全部 ipcMain.handle 实现：window.df 的主进程一侧（02 章 §7 受控 API）。
 *
 * 渲染层是 sandbox + contextIsolation，没有任何 Node 能力，所有本地操作都在这里落地。
 * 因此这里是**最后一道文件访问闸**：
 * - 维护一份「会话内允许前缀集合」，初始只含引擎数据目录；用户经 dialog 选中的路径、
 *   拖拽进来（preload 用 webUtils.getPathForFile 取到）的路径、expandPaths 展开出的
 *   路径会自动加入。readText 越界直接抛错。
 * - 之所以不做成「只允许数据目录」：导出目录、待导入的源文件都在用户自选位置，
 *   而「用户显式选过」正是授权语义本身。
 *
 * 通道名与 preload/index.ts 里的字符串**必须逐字一致**（两侧分别打包，无法共享常量，
 * 改名时务必同时改两处）。
 */

import { readFile, readdir, mkdir, stat, writeFile } from "node:fs/promises";
import { basename, dirname, extname, join, resolve, sep } from "node:path";

import { BrowserWindow, app, dialog, ipcMain, shell } from "electron";
import log from "electron-log/main";

import { exportDiagnosticsZip } from "./diagnostics";
import type { EngineSupervisor } from "./engine-supervisor";

/** 通道名（与 preload 一一对应） */
export const CHANNELS = {
  engineGetInfo: "df:engine:get-info",
  engineRestart: "df:engine:restart",
  /** 主 → 渲染 的单向广播（02 章 §1.1 引擎状态灯） */
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
} as const;

/** 与引擎 ingest.SUPPORTED_EXTS 保持一致（七种格式） */
const SUPPORTED_EXTS = new Set(["doc", "docx", "pdf", "ppt", "pptx", "xls", "xlsx"]);

/** 文件夹展开的自保护上限：深度 8 层、总数 5000 个，防止拖入 C:\ 把 UI 卡死 */
const MAX_DEPTH = 8;
const MAX_FILES = 5000;
/** readText 单文件上限：IR/MD 再大也不该整份塞进渲染进程 */
const MAX_READ_BYTES = 64 * 1024 * 1024;
/** shell.openPath 拒绝直接拉起的可执行类型 */
const EXECUTABLE_EXTS = new Set([
  "exe", "com", "bat", "cmd", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh",
  "msi", "msp", "scr", "cpl", "reg", "hta", "lnk", "pif", "jar",
]);

export interface IpcDeps {
  supervisor: EngineSupervisor;
  /** 引擎数据根目录（%LOCALAPPDATA%\DocFactory） */
  dataRoot: string;
}

// ---------------- 路径闸 ----------------

function normalizeKey(p: string): string {
  const abs = resolve(p);
  // Windows 路径大小写不敏感，比较前统一小写；其余平台保持原样
  return process.platform === "win32" ? abs.toLowerCase() : abs;
}

class FileAccessGuard {
  private readonly prefixes = new Set<string>();

  constructor(roots: string[]) {
    for (const r of roots) this.allow(r);
  }

  allow(p: string): void {
    if (typeof p !== "string" || !p.trim()) return;
    this.prefixes.add(normalizeKey(p));
  }

  /** 校验并返回规范化后的绝对路径；越界抛错 */
  assertAllowed(p: string): string {
    if (typeof p !== "string" || !p.trim()) throw new Error("路径为空");
    const abs = resolve(p);
    const key = normalizeKey(abs);
    for (const prefix of this.prefixes) {
      if (key === prefix) return abs;
      const withSep = prefix.endsWith(sep) ? prefix : prefix + sep;
      if (key.startsWith(withSep)) return abs;
    }
    throw new Error(`拒绝访问未授权路径：${p}`);
  }
}

// ---------------- 文件枚举 ----------------

function extOf(p: string): string {
  return extname(p).replace(/^\./, "").toLowerCase();
}

async function describeFile(p: string): Promise<DfPathEntry | null> {
  let size = 0;
  try {
    const st = await stat(p);
    if (!st.isFile()) return null;
    size = st.size;
  } catch {
    return null; // 权限不足/已删除：静默跳过，不打断整批拖拽
  }
  const ext = extOf(p);
  return {
    path: p,
    name: basename(p),
    ext,
    size,
    supported: SUPPORTED_EXTS.has(ext),
    isKmod: ext === "kmod",
  };
}

async function expandPaths(inputs: string[], guard: FileAccessGuard): Promise<DfPathEntry[]> {
  const out: DfPathEntry[] = [];
  const seen = new Set<string>();
  let truncated = false;

  const pushFile = async (p: string): Promise<void> => {
    if (out.length >= MAX_FILES) {
      truncated = true;
      return;
    }
    const key = normalizeKey(p);
    if (seen.has(key)) return;
    seen.add(key);
    const entry = await describeFile(p);
    if (!entry) return;
    guard.allow(p);
    out.push(entry);
  };

  const walk = async (dir: string, depth: number): Promise<void> => {
    if (depth > MAX_DEPTH) {
      truncated = true;
      return;
    }
    let items;
    try {
      items = await readdir(dir, { withFileTypes: true });
    } catch (err) {
      log.warn(`读取目录失败 ${dir}：${String(err)}`);
      return;
    }
    for (const item of items) {
      if (out.length >= MAX_FILES) {
        truncated = true;
        return;
      }
      // 符号链接一律跳过：既防目录环，也防越权访问被链接到的位置
      if (item.isSymbolicLink()) continue;
      // Office 打开文档时生成的锁文件，导入没有意义
      if (item.name.startsWith("~$")) continue;
      const full = join(dir, item.name);
      if (item.isDirectory()) await walk(full, depth + 1);
      else if (item.isFile()) await pushFile(full);
    }
  };

  for (const raw of inputs) {
    if (typeof raw !== "string" || !raw.trim()) continue;
    const p = resolve(raw);
    let st;
    try {
      st = await stat(p);
    } catch (err) {
      log.warn(`路径不可用 ${p}：${String(err)}`);
      continue;
    }
    // 用户显式拖入/选中的路径本身即视为授权
    guard.allow(p);
    if (st.isDirectory()) await walk(p, 1);
    else if (st.isFile()) await pushFile(p);
  }

  if (truncated) {
    log.warn(`文件夹展开触达上限（深度 ${MAX_DEPTH} / 数量 ${MAX_FILES}），结果已截断`);
  }
  return out;
}

// ---------------- PDF 打印 ----------------

/**
 * 用离屏窗口把导出层产出的打印 HTML 渲染成 PDF（05 章 PDF 导出）。
 * 约定：页面尺寸由 HTML 的 @page 决定（preferCSSPageSize），页边距沿用 Chromium 默认，
 * 需要更大留白请在打印 CSS 里加 padding —— 主进程不猜排版意图。
 */
async function printHtmlToPdf(htmlPath: string, outPdfPath: string): Promise<void> {
  const st = await stat(htmlPath);
  if (!st.isFile()) throw new Error(`打印源不是文件：${htmlPath}`);

  const win = new BrowserWindow({
    show: false,
    width: 1240,
    height: 1754, // A4 @150dpi，仅影响视口，最终分页由打印 CSS 决定
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
      devTools: false,
    },
  });

  try {
    await win.loadFile(htmlPath);
    try {
      // 等字体就绪，避免中文回退字体导致的行高/分页漂移
      await win.webContents.executeJavaScript("document.fonts.ready.then(() => true)", true);
    } catch (err) {
      log.warn(`等待字体就绪失败（继续打印）：${String(err)}`);
    }
    const data = await win.webContents.printToPDF({
      printBackground: true,
      preferCSSPageSize: true,
      landscape: false,
    });
    await mkdir(dirname(outPdfPath), { recursive: true });
    await writeFile(outPdfPath, data);
    log.info(`PDF 已生成：${outPdfPath}（${data.byteLength} 字节）`);
  } finally {
    if (!win.isDestroyed()) win.destroy();
  }
}

// ---------------- 注册 ----------------

function parentOf(sender: Electron.WebContents): BrowserWindow | null {
  return BrowserWindow.fromWebContents(sender) ?? BrowserWindow.getFocusedWindow();
}

function asString(v: unknown, field: string): string {
  if (typeof v !== "string" || !v.trim()) throw new Error(`参数 ${field} 必须是非空字符串`);
  return v;
}

export function registerIpcHandlers(deps: IpcDeps): void {
  const guard = new FileAccessGuard([deps.dataRoot]);

  // ---- engine ----
  ipcMain.handle(CHANNELS.engineGetInfo, (): DfEngineInfo => deps.supervisor.getInfo());
  ipcMain.handle(CHANNELS.engineRestart, async (): Promise<void> => {
    await deps.supervisor.restart();
  });

  // ---- dialog ----
  ipcMain.handle(CHANNELS.dialogPickFiles, async (event): Promise<string[]> => {
    const parent = parentOf(event.sender);
    const options: Electron.OpenDialogOptions = {
      title: "选择要导入的文件",
      properties: ["openFile", "multiSelections", "dontAddToRecent"],
      filters: [
        { name: "支持的文档", extensions: [...SUPPORTED_EXTS] },
        { name: "模组包", extensions: ["kmod"] },
        { name: "全部文件", extensions: ["*"] },
      ],
    };
    const res = parent
      ? await dialog.showOpenDialog(parent, options)
      : await dialog.showOpenDialog(options);
    if (res.canceled) return [];
    for (const p of res.filePaths) guard.allow(p);
    return res.filePaths;
  });

  ipcMain.handle(CHANNELS.dialogPickDirectory, async (event): Promise<string | null> => {
    const parent = parentOf(event.sender);
    const options: Electron.OpenDialogOptions = {
      title: "选择目录",
      properties: ["openDirectory", "createDirectory", "dontAddToRecent"],
    };
    const res = parent
      ? await dialog.showOpenDialog(parent, options)
      : await dialog.showOpenDialog(options);
    const picked = res.canceled ? undefined : res.filePaths[0];
    if (!picked) return null;
    guard.allow(picked);
    return picked;
  });

  ipcMain.handle(
    CHANNELS.dialogPickSavePath,
    async (event, defaultName: unknown): Promise<string | null> => {
      const name = asString(defaultName, "defaultName");
      const ext = extOf(name);
      const parent = parentOf(event.sender);
      let defaultPath = name;
      try {
        defaultPath = join(app.getPath("documents"), name);
      } catch {
        /* 无文档目录时退回纯文件名，由系统决定初始位置 */
      }
      const options: Electron.SaveDialogOptions = {
        title: "另存为",
        defaultPath,
        filters: ext
          ? [
              { name: `${ext.toUpperCase()} 文件`, extensions: [ext] },
              { name: "全部文件", extensions: ["*"] },
            ]
          : [{ name: "全部文件", extensions: ["*"] }],
      };
      const res = parent
        ? await dialog.showSaveDialog(parent, options)
        : await dialog.showSaveDialog(options);
      if (res.canceled || !res.filePath) return null;
      guard.allow(res.filePath);
      guard.allow(dirname(res.filePath));
      return res.filePath;
    },
  );

  // ---- shell ----
  ipcMain.handle(CHANNELS.shellOpenPath, async (_event, p: unknown): Promise<void> => {
    const target = asString(p, "path");
    // 不用路径闸（用户常要打开引擎返回的导出目录），但拒绝直接拉起可执行文件：
    // 万一渲染层被注入，也不能变成任意程序启动器
    if (EXECUTABLE_EXTS.has(extOf(target))) {
      throw new Error(`出于安全考虑，拒绝打开可执行文件：${basename(target)}`);
    }
    const err = await shell.openPath(target);
    if (err) throw new Error(err);
  });

  ipcMain.handle(CHANNELS.shellShowItemInFolder, async (_event, p: unknown): Promise<void> => {
    shell.showItemInFolder(asString(p, "path"));
  });

  // ---- files ----
  // preload 里 pathForFile 是同步 API，取到的拖拽路径经这条单向通道补登记到白名单
  ipcMain.on(CHANNELS.filesNotePath, (_event, p: unknown) => {
    if (typeof p === "string" && p.trim()) guard.allow(p);
  });

  ipcMain.handle(
    CHANNELS.filesExpandPaths,
    async (_event, paths: unknown): Promise<DfPathEntry[]> => {
      if (!Array.isArray(paths)) throw new Error("参数 paths 必须是字符串数组");
      return expandPaths(paths as string[], guard);
    },
  );

  ipcMain.handle(CHANNELS.filesReadText, async (_event, p: unknown): Promise<string> => {
    const target = guard.assertAllowed(asString(p, "path"));
    const st = await stat(target);
    if (!st.isFile()) throw new Error(`不是文件：${target}`);
    if (st.size > MAX_READ_BYTES) {
      throw new Error(
        `文件过大（${Math.round(st.size / 1024 / 1024)}MB），已超过 ${MAX_READ_BYTES / 1024 / 1024}MB 上限`,
      );
    }
    const buf = await readFile(target);
    // 去 UTF-8 BOM：引擎产物统一 UTF-8，但用户自选文件可能带 BOM
    return buf.toString("utf-8").replace(/^\uFEFF/, "");
  });

  // ---- pdf ----
  ipcMain.handle(
    CHANNELS.pdfPrintHtmlToPdf,
    async (_event, htmlPath: unknown, outPdfPath: unknown): Promise<void> => {
      const html = asString(htmlPath, "htmlPath");
      const out = asString(outPdfPath, "outPdfPath");
      await printHtmlToPdf(html, out);
      guard.allow(out);
    },
  );

  // ---- diagnostics ----
  ipcMain.handle(CHANNELS.diagnosticsExportZip, async (event): Promise<string | null> => {
    return exportDiagnosticsZip({
      supervisor: deps.supervisor,
      dataRoot: deps.dataRoot,
      parent: parentOf(event.sender),
    });
  });

  // ---- appInfo ----
  ipcMain.handle(
    CHANNELS.appVersions,
    (): { app: string; electron: string; chrome: string; node: string } => ({
      app: app.getVersion(),
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      node: process.versions.node,
    }),
  );

  log.info("IPC handlers 已注册");
}
