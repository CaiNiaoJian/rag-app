/* 应用入口：单实例锁 → 网络封锁 → 窗口 → IPC → 引擎 sidecar → 生命周期
 * （02 章 §1 进程拓扑与 §1.1 生命周期，08 章 §1 日志架构）。
 *
 * 几个刻意的顺序，改动前先读：
 * ① 日志必须最先就位（且与引擎共用 %LOCALAPPDATA%\DocFactory\logs），
 *    否则启动期的失败信息无处可查，诊断包也抓不全（08 章「一把抓」的前提）。
 * ② Chromium 联网开关必须在 app ready 之前 appendSwitch，ready 之后设置无效。
 * ③ 导航闸挂在 app 的 web-contents-created 上，必须早于任何窗口创建。
 * ④ 引擎在窗口创建之后再拉起：窗口先出来，用户看到的是「启动中」而不是白屏等待。
 */

import { mkdirSync, readdirSync, renameSync, rmSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { BrowserWindow, Menu, app, session } from "electron";
import log from "electron-log/main";

import { EngineSupervisor } from "./engine-supervisor";
import { CHANNELS, registerIpcHandlers } from "./ipc";
import {
  disableChromiumNetworkFeatures,
  installNavigationGuard,
  installNetGuard,
  type NetGuardOptions,
} from "./net-guard";

/** 日志轮转策略（与引擎 loguru 一致）：10MB 一份、最多 10 份归档、保留 14 天 */
const LOG_MAX_SIZE = 10 * 1024 * 1024;
const LOG_MAX_ARCHIVES = 10;
const LOG_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;

/** 数据根目录，与引擎 config.default_data_root() 的规则逐条对齐 */
function resolveDataRoot(): string {
  const env = process.env["DOCFACTORY_DATA_DIR"];
  if (env && env.trim()) return resolve(env);
  const local = process.env["LOCALAPPDATA"] ?? join(app.getPath("home"), "AppData", "Local");
  return join(local, "DocFactory");
}

/** 归档后清理：先按份数裁，再按天数裁（两条都满足才留） */
function pruneArchives(logsDir: string): void {
  try {
    const now = Date.now();
    const files = readdirSync(logsDir)
      .filter((n) => /^app-main-.+\.log$/i.test(n))
      .map((n) => {
        const full = join(logsDir, n);
        try {
          return { full, mtime: statSync(full).mtimeMs };
        } catch {
          return { full, mtime: 0 };
        }
      })
      .sort((a, b) => b.mtime - a.mtime);

    for (const [index, f] of files.entries()) {
      if (index < LOG_MAX_ARCHIVES && now - f.mtime <= LOG_MAX_AGE_MS) continue;
      rmSync(f.full, { force: true });
    }
  } catch (err) {
    // 这里不能用 log.*：轮转回调内部再写日志会递归
    console.error("[docfactory] 清理历史日志失败", err);
  }
}

function setupLogging(logsDir: string, isDev: boolean): void {
  mkdirSync(logsDir, { recursive: true });
  // preload:false —— 我们有自己的沙箱 preload，不让 electron-log 再注入一个
  log.initialize({ preload: false, spyRendererConsole: false });

  log.transports.file.level = "info";
  log.transports.file.maxSize = LOG_MAX_SIZE;
  log.transports.file.format = "[{y}-{m}-{d} {h}:{i}:{s}.{ms}] [{level}] {text}";
  log.transports.file.resolvePathFn = () => join(logsDir, "app-main.log");
  log.transports.file.archiveLogFn = (file) => {
    try {
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      renameSync(file.path, join(logsDir, `app-main-${stamp}.log`));
    } catch (err) {
      console.error("[docfactory] 归档日志失败", err);
    }
    pruneArchives(logsDir);
  };

  log.transports.console.level = isDev ? "debug" : false;
  // 远程/IPC 传输一律关死：离线应用不存在「把日志发出去」的场景（02 章 §7）
  if (log.transports.remote) log.transports.remote.level = false;
  if (log.transports.ipc) log.transports.ipc.level = false;

  log.errorHandler.startCatching({ showDialog: false });
}

// ---------------- 启动 ----------------

const dataRoot = resolveDataRoot();
try {
  setupLogging(join(dataRoot, "logs"), !app.isPackaged);
} catch (err) {
  // 日志目录不可写（只读盘/权限异常）时不能连界面都起不来：退回 electron-log 默认位置
  console.error("[docfactory] 日志初始化失败，已退回默认日志位置", err);
}

// 开发模式下由 electron-vite 注入；生产为 undefined，走 file:// 加载
const devServerUrl = process.env["ELECTRON_RENDERER_URL"] ?? null;

/* 开发模式用主进程产物位置反推 app/ 根目录（out/main/index.js → app/）：
 * app.getAppPath() 的取值依赖 electron 的启动方式（`electron .` 与
 * `electron out/main/index.js` 结果不同），反推则恒定。生产模式不走这条路径。 */
const appRoot = app.isPackaged
  ? app.getAppPath()
  : fileURLToPath(new URL("../..", import.meta.url));

const supervisor = new EngineSupervisor({
  dataRoot,
  isPackaged: app.isPackaged,
  appRoot,
});

const guardOptions: NetGuardOptions = {
  enginePort: () => supervisor.getInfo().port,
  devServerUrl,
};

let mainWindow: BrowserWindow | null = null;
let quitting = false;

function broadcastEngineStatus(info: DfEngineInfo): void {
  log.info(`引擎状态变更：${info.status}${info.port > 0 ? ` @127.0.0.1:${info.port}` : ""}`);
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.send(CHANNELS.engineStatus, info);
  }
}

async function createWindow(): Promise<void> {
  const preloadPath = fileURLToPath(new URL("../preload/index.cjs", import.meta.url));

  const win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1120,
    minHeight: 700,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: "#0f1115",
    title: "DocFactory",
    webPreferences: {
      preload: preloadPath,
      // 02 章 §7：渲染层零 Node 能力，一切经 preload 白名单
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
      webviewTag: false,
      allowRunningInsecureContent: false,
      devTools: !app.isPackaged,
    },
  });
  mainWindow = win;

  win.once("ready-to-show", () => win.show());
  win.on("closed", () => {
    mainWindow = null;
  });

  win.webContents.on("render-process-gone", (_event, details) => {
    log.error(`渲染进程异常退出：${details.reason}（exitCode=${details.exitCode}）`);
  });
  win.webContents.on("did-fail-load", (_event, code, description, url) => {
    log.error(`页面加载失败：${url} → ${code} ${description}`);
  });

  /* 渲染进程的 console 转进主日志。
   * 没有它，UI 侧出问题时诊断包里一片空白 —— 用户只能描述「卡住了」，
   * 而真正的原因（某个请求被拒、某个 Promise 挂着）全留在没人打开的 DevTools 里。
   * 只收 warn 及以上：info/debug 量大且多为 React 与 Vite 的噪声。 */
  win.webContents.on("console-message", (details) => {
    if (details.level !== "warning" && details.level !== "error") return;
    const where = details.sourceId ? ` (${details.sourceId}:${details.lineNumber})` : "";
    const line = `[renderer] ${details.message}${where}`;
    if (details.level === "error") log.error(line);
    else log.warn(line);
  });
  // 页面每次加载完成都补发一次当前引擎状态：渲染层订阅时机可能晚于状态变化
  win.webContents.on("did-finish-load", () => {
    win.webContents.send(CHANNELS.engineStatus, supervisor.getInfo());
  });

  if (devServerUrl) {
    log.info(`加载开发服务器：${devServerUrl}`);
    await win.loadURL(devServerUrl);
  } else {
    const html = fileURLToPath(new URL("../renderer/index.html", import.meta.url));
    log.info(`加载本地页面：${html}`);
    await win.loadFile(html);
  }
}

function bootstrap(): void {
  log.info(`DocFactory ${app.getVersion()} 启动，数据目录：${dataRoot}`);

  app.setAppUserModelId("com.docfactory.app");
  // ② 必须在 ready 之前
  disableChromiumNetworkFeatures();
  app.enableSandbox();
  // ③ 必须早于任何窗口创建
  installNavigationGuard(guardOptions);

  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });

  app.whenReady().then(
    async () => {
      if (app.isPackaged) Menu.setApplicationMenu(null);

      installNetGuard(session.defaultSession, guardOptions);
      registerIpcHandlers({ supervisor, dataRoot });
      supervisor.onChange(broadcastEngineStatus);

      await createWindow();

      // ④ 引擎失败不阻断 UI：状态灯变红 + E06 指引，用户可在设置页点「重试」
      void supervisor.start().catch((err) => log.error(`引擎启动失败：${String(err)}`));
    },
    (err: unknown) => log.error(`应用初始化失败：${String(err)}`),
  );

  // 单窗口产品：窗口关完即退出（Windows 无 dock 常驻语义）
  app.on("window-all-closed", () => app.quit());

  app.on("before-quit", (event) => {
    if (quitting) return;
    // 先拦下退出，把引擎优雅收尾（宽限 10s，超时强杀进程树），再真正退出
    event.preventDefault();
    quitting = true;
    log.info("准备退出：正在停止引擎");
    void supervisor
      .stop()
      .catch((err) => log.error(`停止引擎失败：${String(err)}`))
      .finally(() => app.quit());
  });
}

if (!app.requestSingleInstanceLock()) {
  log.info("检测到已有实例在运行，本次启动直接退出");
  app.quit();
} else {
  bootstrap();
}
