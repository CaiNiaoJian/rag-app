/* 网络封锁：离线与安全强制的 Electron 侧实现（02 章 §7）。
 *
 * 三道闸，缺一不可：
 *   ① webRequest.onBeforeRequest 白名单 —— 硬闸。CSP 只约束页面发起的请求，
 *      这里连 Chromium 内部组件发起的请求一起挡，非白名单一律 cancel 并记 warning。
 *   ② onHeadersReceived 注入 CSP —— 纵深防御。注意 file:// 文档走不到响应头，
 *      生产环境实际生效的是 index.html 里那份冻结的 CSP meta，本函数是给
 *      开发期 dev server（http 响应）与将来可能的自定义协议兜底。
 *   ③ 导航闸 —— will-navigate / setWindowOpenHandler 一律 deny，
 *      避免任何一次误点把 renderer 导航到外部页面（那会绕过 ① 的白名单语义）。
 *
 * 白名单只认本机回环的两个端口：引擎端口（随启动变化，故用回调实时取）与
 * 开发期 vite dev server 端口。除此之外 http/ws 一律拒绝，https 一律拒绝。
 */

import { app, type Session } from "electron";
import log from "electron-log/main";

export interface NetGuardOptions {
  /** 引擎当前端口，未就绪时返回 0（此时除本地资源外全部拒绝） */
  enginePort: () => number;
  /** 开发期 vite dev server 地址（electron-vite 注入的 ELECTRON_RENDERER_URL），生产为 null */
  devServerUrl: string | null;
}

/** 无网络语义的本地方案，直接放行 */
const LOCAL_SCHEMES = new Set([
  "file:",
  "devtools:",
  "app:",
  "data:",
  "blob:",
  "about:",
  "chrome-extension:",
]);

function isLoopbackHost(hostname: string): boolean {
  const h = hostname.toLowerCase();
  return h === "127.0.0.1" || h === "localhost" || h === "::1" || h === "[::1]";
}

function portOf(rawUrl: string | null): number {
  if (!rawUrl) return 0;
  try {
    const port = Number(new URL(rawUrl).port);
    return Number.isInteger(port) && port > 0 ? port : 0;
  } catch {
    return 0;
  }
}

/** 白名单判定（导出供导航闸复用） */
export function isAllowedUrl(rawUrl: string, opts: NetGuardOptions): boolean {
  let u: URL;
  try {
    u = new URL(rawUrl);
  } catch {
    return false;
  }
  if (LOCAL_SCHEMES.has(u.protocol)) return true;
  if (u.protocol !== "http:" && u.protocol !== "ws:") return false;
  if (!isLoopbackHost(u.hostname)) return false;

  const port = Number(u.port);
  if (!Number.isInteger(port) || port <= 0) return false;

  const enginePort = opts.enginePort();
  if (enginePort > 0 && port === enginePort) return true;

  const devPort = portOf(opts.devServerUrl);
  return devPort > 0 && port === devPort;
}

function buildCsp(opts: NetGuardOptions): string {
  const dev = opts.devServerUrl !== null;
  const connect = dev
    ? "'self' http://127.0.0.1:* ws://127.0.0.1:* http://localhost:* ws://localhost:*"
    : "'self' http://127.0.0.1:*";
  // 开发期 HMR / React Refresh 依赖内联脚本与 eval，生产收紧回 'self'
  const script = dev ? "'self' 'unsafe-inline' 'unsafe-eval'" : "'self'";
  return [
    "default-src 'self'",
    `script-src ${script}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' file: data: blob:",
    "font-src 'self' file: data:",
    "media-src 'self' file: data:",
    `connect-src ${connect}`,
    "worker-src 'self' blob:",
    "object-src 'none'",
    "frame-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'none'",
  ].join("; ");
}

/** 同一 URL 的拦截日志节流窗口：引擎未就绪时 UI 会持续轮询同一地址，不节流会刷屏 */
const BLOCK_LOG_INTERVAL_MS = 60_000;
const blockedLogAt = new Map<string, number>();

function logBlocked(url: string, resourceType: string): void {
  const now = Date.now();
  const last = blockedLogAt.get(url);
  if (last !== undefined && now - last < BLOCK_LOG_INTERVAL_MS) return;
  // 防止无界增长（恶意/异常情况下 URL 可能无限变化）
  if (blockedLogAt.size > 200) blockedLogAt.clear();
  blockedLogAt.set(url, now);
  // 记 warning 而不是 error：这属于「被成功拦下」的预期事件，但必须留痕以便审计
  log.warn(`[net-guard] 已拦截非白名单请求：${resourceType} ${url}`);
}

/** 安装会话级封锁（默认会话即可覆盖窗口与离屏打印窗口） */
export function installNetGuard(ses: Session, opts: NetGuardOptions): void {
  const csp = buildCsp(opts);

  ses.webRequest.onBeforeRequest({ urls: ["<all_urls>"] }, (details, callback) => {
    if (isAllowedUrl(details.url, opts)) {
      callback({ cancel: false });
      return;
    }
    logBlocked(details.url, details.resourceType);
    callback({ cancel: true });
  });

  ses.webRequest.onHeadersReceived((details, callback) => {
    const headers = { ...(details.responseHeaders ?? {}) };
    // 大小写不敏感地清掉上游可能带的 CSP，再写入我们这份
    for (const key of Object.keys(headers)) {
      if (key.toLowerCase() === "content-security-policy") delete headers[key];
      if (key.toLowerCase() === "content-security-policy-report-only") delete headers[key];
    }
    headers["Content-Security-Policy"] = [csp];
    callback({ responseHeaders: headers });
  });

  // 一切设备/系统权限一律拒绝：本产品不需要摄像头、麦克风、定位、通知、剪贴板读
  ses.setPermissionRequestHandler((_wc, permission, callback) => {
    log.warn(`[net-guard] 已拒绝权限请求：${permission}`);
    callback(false);
  });
  ses.setPermissionCheckHandler(() => false);
  ses.setDevicePermissionHandler(() => false);

  // 拼写检查会向 Google 下载词典（02 章 §7 明令禁用）
  ses.setSpellCheckerEnabled(false);

  // 忽略系统代理设置：与引擎侧 trust_env=False 对齐，避免流量被代理软件牵走
  void ses.setProxy({ mode: "direct" }).catch((err) => {
    log.warn(`[net-guard] 设置直连代理失败：${String(err)}`);
  });
}

/** 安装进程级导航闸（对所有 webContents 生效，包括离屏打印窗口） */
export function installNavigationGuard(opts: NetGuardOptions): void {
  app.on("web-contents-created", (_event, contents) => {
    contents.on("will-navigate", (event, url) => {
      if (isAllowedUrl(url, opts)) return;
      event.preventDefault();
      log.warn(`[net-guard] 已阻止导航：${url}`);
    });

    // 任何 window.open / target=_blank 一律拒绝（离线应用没有「打开外部页面」的语义）
    contents.setWindowOpenHandler(({ url }) => {
      log.warn(`[net-guard] 已阻止新建窗口：${url}`);
      return { action: "deny" };
    });

    contents.on("will-attach-webview", (event) => {
      event.preventDefault();
      log.warn("[net-guard] 已阻止 webview 挂载");
    });
  });

  // 证书错误一律不放行（正常流程根本不该出现 TLS 连接）
  app.on("certificate-error", (event, _wc, url, error, _cert, callback) => {
    event.preventDefault();
    log.warn(`[net-guard] 证书校验失败，已拒绝：${url}（${error}）`);
    callback(false);
  });
}

/** 关闭 Chromium 自带的联网特性；必须在 app ready 之前调用 */
export function disableChromiumNetworkFeatures(): void {
  const switches: Array<[string, string?]> = [
    ["disable-background-networking"],
    ["disable-component-update"],
    ["disable-domain-reliability"],
    ["disable-breakpad"],
    ["disable-crash-reporter"],
    ["disable-sync"],
    ["disable-speech-api"],
    ["no-pings"],
    [
      "disable-features",
      [
        "NetworkPrediction",
        "OptimizationHints",
        "OptimizationGuideModelDownloading",
        "Translate",
        "AutofillServerCommunication",
        "MediaRouter",
        "DialMediaRouteProvider",
        "CertificateTransparencyComponentUpdater",
        "CrashpadUpload",
        "SpellcheckService",
        "SafeBrowsing",
      ].join(","),
    ],
    // 最后一道保险：除回环外的域名解析直接 NOTFOUND（我们只用 127.0.0.1 字面量）
    ["host-resolver-rules", "MAP * ~NOTFOUND , EXCLUDE localhost"],
  ];
  for (const [name, value] of switches) {
    if (value === undefined) app.commandLine.appendSwitch(name);
    else app.commandLine.appendSwitch(name, value);
  }
}
