/* 网络封锁：离线与安全强制的 Electron 侧实现（02 章 §7）。
 *
 * 四道闸，缺一不可：
 *   ① webRequest.onBeforeRequest 白名单 —— 硬闸。CSP 只约束页面发起的请求，
 *      这里连 Chromium 内部组件发起的请求一起挡，非白名单一律 cancel 并记 warning。
 *   ② onHeadersReceived 注入 CSP —— 纵深防御。注意 file:// 文档走不到响应头，
 *      生产环境实际生效的是 index.html 里那份冻结的 CSP meta，本函数是给
 *      开发期 dev server（http 响应）与将来可能的自定义协议兜底。
 *      两份必须逐条同步，同步规则与例外见 buildCsp 上方注释。
 *   ③ 导航闸 —— will-navigate / setWindowOpenHandler 一律 deny，
 *      避免任何一次误点把 renderer 导航到外部页面（那会绕过 ① 的白名单语义）。
 *   ④ 主进程 socket 闸 —— ①②③ 全在 Chromium 网络栈里，而主进程自己用的是 Node 的
 *      net/http 与 undici fetch，一道都不经过。引擎侧早有 offline_guard.py 在
 *      socket 层强制，主进程这边缺了对等物就只剩「靠代码评审自觉」，
 *      与 02 章 §7「代码层面强制」的定位对不上。见 installProcessSocketGuard。
 *
 * 白名单只认本机回环的两个端口：引擎端口（随启动变化，故用回调实时取）与
 * 开发期 vite dev server 端口。除此之外 http/ws 一律拒绝，https 一律拒绝。
 */

import net from "node:net";

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

/* 同一份 CSP 在仓库里有三处落点，改任何一条指令都必须三处一起过一遍：
 *   1. 本函数 —— 经 onHeadersReceived 下发。生产走 file://，响应头这条路根本不存在，
 *      所以它实际只在开发期（dev server 的 http 响应）生效。
 *   2. src/renderer/index.html 的 CSP meta —— **生产环境真正生效的那一份**。
 *   3. electron.vite.config.ts 的 devCspPlugin —— 仅 serve 时把 2 整个换成放宽 HMR 的版本。
 *
 * 审计已经抓到过一次漂移：本函数写满 13 条，meta 却停在 5 条，于是 base-uri、
 * form-action、object-src、frame-src 在生产里全都没生效。前两条尤其致命 —— 它们
 * **没有 default-src 回退**，缺了就等于允许 base 标签劫持全部相对路径加载、允许表单
 * 提交到任意 URL，而读代码只看本函数会以为一切都拦住了。只改一处不会有任何编译或运行
 * 期报错，唯一的防线就是这段注释与 index.html 里那段对应的注释。
 *
 * meta 与本函数生产分支逐条一致，仅一处有意差异：frame-ancestors。按 CSP 规范，
 * meta 交付时 frame-ancestors / report-uri / sandbox 会被忽略并在控制台报错，所以
 * meta 里不写它；顶层 file:// 文档本就无法被嵌套，webview 挂载与 window.open 又已被
 * 导航闸 ③ 全拒，生产侧不存在实际缺口。
 *
 * 端口不写死：引擎端口每次启动随机（见 engine-supervisor），connect-src 只能用
 * http://127.0.0.1:* 这种通配端口；渲染层的 baseURL 也固定拼 127.0.0.1（api.ts），
 * 所以生产不必放行 localhost 这个别名。
 */
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
    // 唯一不出现在 index.html meta 里的一条（meta 交付时规范要求忽略它），理由见上
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

  /* 设备/系统权限默认全拒（摄像头、麦克风、定位、通知、剪贴板**读**……本产品都不需要）。
   * 唯一放行 clipboard-sanitized-write：07 章的「复制切片/复制错误详情」按钮走
   * navigator.clipboard.writeText，全拒会让它抛 NotAllowedError，只能退到
   * document.execCommand("copy") 那条已废弃的兜底路径上。该权限只是「往系统剪贴板写
   * 经过消毒的内容」，既不读剪贴板也不碰网络，放行不削弱离线与隐私约束。 */
  const ALLOWED_PERMISSIONS = new Set<string>(["clipboard-sanitized-write"]);

  ses.setPermissionRequestHandler((_wc, permission, callback) => {
    if (ALLOWED_PERMISSIONS.has(permission)) {
      callback(true);
      return;
    }
    log.warn(`[net-guard] 已拒绝权限请求：${permission}`);
    callback(false);
  });
  ses.setPermissionCheckHandler((_wc, permission) => ALLOWED_PERMISSIONS.has(permission));
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
    /* 最后一道保险：除回环外的域名解析直接 NOTFOUND。
     * `MAP *` 会连 IP 字面量一起改写（Chromium 的 HostMappingRules 是按 host 字符串
     * 做 MatchPattern，不区分域名与字面量），所以 127.0.0.1 / ::1 必须显式 EXCLUDE，
     * 否则渲染进程 fetch("http://127.0.0.1:port") 会直接 ERR_NAME_NOT_RESOLVED，
     * 开发期连 dev server 页面本身都加载不出来。实测：只写 EXCLUDE localhost 时
     * 回环字面量被拦，补上下面两条后回环通、外部域名仍解析失败。 */
    ["host-resolver-rules", "MAP * ~NOTFOUND , EXCLUDE localhost , EXCLUDE 127.0.0.1 , EXCLUDE ::1"],
  ];
  for (const [name, value] of switches) {
    if (value === undefined) app.commandLine.appendSwitch(name);
    else app.commandLine.appendSwitch(name, value);
  }
}

/**
 * 主进程 socket 级离线闸：非回环的 TCP 连接直接抛，与引擎侧 offline_guard.py 对称。
 *
 * 上面三道闸全部活在 Chromium 网络栈里。主进程自己却不走 Chromium ——
 * `engine-supervisor` 用的是 `node:http`，Node 还内置了 undici 的全局 `fetch`，
 * 这两条路径对 webRequest 白名单与 `host-resolver-rules` 完全免疫。
 * 今天主进程只连 127.0.0.1，但那是「靠自觉」；FR-17 要的是「代码层面强制」，
 * 两者的差别会在某次顺手加个 HTTP 调用时显现出来。
 *
 * 在 `net.Socket.prototype.connect` 这一层拦，是因为 Node 的 http/https/undici
 * 最终都落到它上面 —— 补一处等于把主进程所有出网路径一起收口。
 * DNS 不解析：离线环境里主机名一律视为非回环，不为了判定去发一次查询。
 */

/* 受控临时放行集合：**唯一**的合法写入方是 withRemoteHostsAllowed()。
 * 默认恒空 —— 应用的一切常规路径仍是「非回环一律拒绝」；只有用户主动触发的
 * 动作（目前仅「检查更新」）会在动作存续期内放行指定主机，动作结束立即收回。
 * 这保持了 FR-17 的语义：不存在任何**自动**出网路径，出网必须来自用户当下的点击。 */
const temporarilyAllowedHosts = new Set<string>();

/**
 * 在回调存续期内放行指定远程主机（大小写不敏感），结束后无条件收回。
 * 放行与收回都写日志留痕，供离线审计核对「每一次出网都对应一次用户动作」。
 */
export async function withRemoteHostsAllowed<T>(hosts: string[], fn: () => Promise<T>): Promise<T> {
  const normalized = hosts.map((h) => h.toLowerCase());
  for (const h of normalized) temporarilyAllowedHosts.add(h);
  log.info(`[net-guard] 临时放行远程主机（用户动作）：${normalized.join(", ")}`);
  try {
    return await fn();
  } finally {
    for (const h of normalized) temporarilyAllowedHosts.delete(h);
    log.info("[net-guard] 已收回临时放行");
  }
}

export function installProcessSocketGuard(): void {
  const realConnect = net.Socket.prototype.connect;

  const hostAllowed = (host: unknown): boolean =>
    typeof host !== "string" ||
    host === "" ||
    isLoopbackHost(host) ||
    temporarilyAllowedHosts.has(host.toLowerCase());

  net.Socket.prototype.connect = function patchedConnect(
    this: net.Socket,
    ...args: Parameters<typeof net.Socket.prototype.connect>
  ): net.Socket {
    const [first] = args;
    // connect(options) / connect(port, host) / connect(path)（IPC 管道，本机内通信）
    if (typeof first === "object" && first !== null && "host" in first) {
      if (!hostAllowed((first as net.TcpNetConnectOpts).host)) {
        throw new Error(`offline guard: blocked -> ${(first as net.TcpNetConnectOpts).host}`);
      }
    } else if (typeof first === "number") {
      const host = args.find((a) => typeof a === "string");
      if (!hostAllowed(host)) throw new Error(`offline guard: blocked -> ${String(host)}`);
    }
    return realConnect.apply(this, args);
  } as typeof net.Socket.prototype.connect;

  log.info("主进程 socket 闸已安装：非回环 TCP 连接一律拒绝");
}
