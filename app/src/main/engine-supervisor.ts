/* 引擎 sidecar 的拉起 / 守护 / 重启（02 章 §1.1 生命周期）。
 *
 * 设计要点（都是踩过的坑，别随手简化）：
 * - **握手只认第一行 READY**：引擎的 stdout 契约是「全进程只写一行 READY {...}」，
 *   但 PyInstaller 产物里第三方库（onnxruntime 等）仍可能往 stdout 吐东西。因此
 *   逐行读，命中第一条 READY 就完成握手，之后所有 stdout 行一律当普通日志落盘，
 *   绝不再尝试解析 —— 这正是 02 章 §2 弃用 stdio JSON-RPC 的理由。
 * - **READY 之后还要探活**：READY 只说明端口已绑定，路由/DB 未必就绪，再轮询
 *   GET /health 直到 200；从 spawn 起算 15s 总超时，超时按 E06 交给 UI 提示重试。
 * - **重启配额**：非零退出自动重启，30s 窗口内最多 2 次；超配额置 down 并广播，
 *   由用户在 UI 上手动「重试」（restart() 会清空配额）。避免崩溃循环烧 CPU。
 * - **退出必须清进程树**：引擎会派生 soffice.exe，宽限期后单杀引擎 pid 会留下孤儿，
 *   所以 Windows 上用 taskkill /T /F 连根拔。
 */

import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { join, resolve as resolvePath } from "node:path";
import { createInterface } from "node:readline";

import log from "electron-log/main";

/** 启动总超时（与引擎 main.py 的 _STARTUP_TIMEOUT_S 保持一致） */
const STARTUP_TIMEOUT_MS = 15_000;
/** POST /shutdown 后的退出宽限 */
const SHUTDOWN_GRACE_MS = 10_000;
/** 自动重启配额窗口与次数 */
const RESTART_WINDOW_MS = 30_000;
const RESTART_MAX = 2;

export interface SupervisorOptions {
  /** 数据根目录（%LOCALAPPDATA%\DocFactory），显式传给引擎保证两侧一致 */
  dataRoot: string;
  /** app.isPackaged：决定跑 venv python 还是 resources/engine/engine.exe */
  isPackaged: boolean;
  /** 开发模式下的 app/ 目录，用于定位同级的 engine/ */
  appRoot: string;
}

export interface EngineEndpoint {
  port: number;
  token: string;
}

interface EngineLaunch {
  exe: string;
  args: string[];
  cwd: string;
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/** 结束整棵进程树（Windows 必须带 /T，否则 soffice.exe 会变孤儿进程） */
export function killProcessTree(pid: number | undefined): void {
  if (!pid) return;
  if (process.platform === "win32") {
    const res = spawnSync("taskkill", ["/pid", String(pid), "/T", "/F"], { windowsHide: true });
    if (res.status !== 0) {
      log.warn(`taskkill 未能结束进程树 pid=${pid}：${res.stderr?.toString().trim() ?? ""}`);
    }
  } else {
    try {
      process.kill(-pid, "SIGKILL");
    } catch {
      /* 进程已退出 */
    }
  }
}

/**
 * 向引擎发一次 HTTP 请求（仅 127.0.0.1 回环）。
 * 主进程侧刻意不引第三方 HTTP 客户端：Node 内置 http 足够，且少一个联网代码路径。
 */
export function requestEngine(
  ep: EngineEndpoint,
  method: string,
  path: string,
  timeoutMs = 5_000,
  body?: unknown,
): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body), "utf-8");
    const headers: Record<string, string> = {};
    if (ep.token) headers["Authorization"] = `Bearer ${ep.token}`;
    if (payload) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = String(payload.byteLength);
    }

    const req = httpRequest(
      { host: "127.0.0.1", port: ep.port, path, method, headers, timeout: timeoutMs },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => {
          resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString("utf-8") });
        });
      },
    );
    req.on("timeout", () => req.destroy(new Error(`请求超时：${method} ${path}`)));
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

export class EngineSupervisor {
  private readonly opts: SupervisorOptions;
  private child: ChildProcessWithoutNullStreams | null = null;
  private info: DfEngineInfo = { port: 0, token: "", status: "down" };
  private readonly listeners = new Set<(info: DfEngineInfo) => void>();
  /** 最近一次失败原因，供 UI 的 E06 详情展示 */
  private lastError: string | null = null;
  /** 最近若干次自动重启的时间戳（滑动窗口配额） */
  private restarts: number[] = [];
  private stopping = false;
  private starting: Promise<DfEngineInfo> | null = null;

  constructor(opts: SupervisorOptions) {
    this.opts = opts;
  }

  getInfo(): DfEngineInfo {
    return { ...this.info };
  }

  getLastError(): string | null {
    return this.lastError;
  }

  /** 订阅状态变化，返回退订函数 */
  onChange(cb: (info: DfEngineInfo) => void): () => void {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  }

  async start(): Promise<DfEngineInfo> {
    if (this.info.status === "ready" && this.child) return this.getInfo();
    if (this.starting) return this.starting;
    this.starting = this.launch().finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  /** UI 的「重试」入口：清空自动重启配额后重来一次 */
  async restart(): Promise<void> {
    log.info("收到重启引擎请求");
    this.restarts = [];
    await this.stop();
    /* stop() 只等到子进程退出，此时上一轮 launch() 的 promise 可能还没收尾；
     * 不等它就直接 start()，会命中 start() 里的「已有启动在飞」分支，把那个注定
     * 失败的 promise 当成本次重启的结果返回 —— 表现为用户点了「重试」却没反应。
     * 等它（无论成败）落地后 this.starting 必为 null，再真正拉起新进程。 */
    const pending = this.starting;
    if (pending) await pending.catch(() => undefined);
    await this.start();
  }

  /** 优雅退出：POST /shutdown → 宽限 10s → 强杀进程树 */
  async stop(): Promise<void> {
    this.stopping = true;
    const child = this.child;
    if (!child || child.exitCode !== null || child.signalCode !== null) {
      this.child = null;
      this.update({ status: "down", port: 0 });
      return;
    }

    if (this.info.port > 0) {
      try {
        await requestEngine(
          { port: this.info.port, token: this.info.token },
          "POST",
          "/shutdown",
          3_000,
          {},
        );
        log.info("已请求引擎优雅退出，等待收尾");
      } catch (err) {
        log.warn(`请求引擎退出失败，转为等待/强杀：${String(err)}`);
      }
    }

    const exited = await this.waitExit(child, SHUTDOWN_GRACE_MS);
    if (!exited) {
      log.warn(`引擎未在 ${SHUTDOWN_GRACE_MS / 1000}s 宽限内退出，强制结束进程树`);
      killProcessTree(child.pid);
      await this.waitExit(child, 3_000);
    }
    this.child = null;
    this.update({ status: "down", port: 0 });
  }

  // ---------------- 内部实现 ----------------

  private update(patch: Partial<DfEngineInfo>): void {
    this.info = { ...this.info, ...patch };
    const snapshot = this.getInfo();
    for (const cb of this.listeners) {
      try {
        cb(snapshot);
      } catch (err) {
        log.warn(`引擎状态订阅回调异常：${String(err)}`);
      }
    }
  }

  /** 开发模式跑 venv 里的 python -m docfactory.main；生产跑 PyInstaller 产物 */
  private resolveLaunch(token: string): EngineLaunch {
    /* --parent-pid：用户在任务管理器里直接结束 DocFactory.exe 时，我们没机会发
     * /shutdown，子进程会被过继给别人变成常驻孤儿（engine.exe 还会拖着 soffice.exe）。
     * Windows 没有 PDEATHSIG，正规解法是 Job Object，但那要引原生模块——
     * electron-builder 里 npmRebuild:false 正是为了躲开这个。所以改由引擎盯着我们：
     * 见 engine/src/docfactory/parent_watch.py。 */
    const common = [
      "--port", "0",
      "--token", token,
      "--data-dir", this.opts.dataRoot,
      "--parent-pid", String(process.pid),
    ];
    if (this.opts.isPackaged) {
      const dir = join(process.resourcesPath, "engine");
      return { exe: join(dir, "engine.exe"), args: common, cwd: dir };
    }
    const engineDir =
      process.env["DOCFACTORY_ENGINE_DIR"] ?? resolvePath(this.opts.appRoot, "..", "engine");
    const pythonName = process.platform === "win32" ? "python.exe" : "python";
    const python =
      process.env["DOCFACTORY_ENGINE_PYTHON"] ??
      join(engineDir, ".venv", process.platform === "win32" ? "Scripts" : "bin", pythonName);
    return { exe: python, args: ["-m", "docfactory.main", ...common], cwd: engineDir };
  }

  private async launch(): Promise<DfEngineInfo> {
    this.stopping = false;
    this.lastError = null;
    // token 每次启动随机 128bit，仅经命令行传入，不落盘（02 章 §7）
    const token = randomBytes(16).toString("hex");
    const launch = this.resolveLaunch(token);

    this.update({ port: 0, token, status: "starting" });

    if (!existsSync(launch.exe)) {
      return this.fail(`未找到引擎可执行文件：${launch.exe}`);
    }

    log.info(`启动引擎：${launch.exe} (cwd=${launch.cwd})`);
    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(launch.exe, launch.args, {
        cwd: launch.cwd,
        windowsHide: true,
        env: {
          ...process.env,
          // Windows 中文环境下必须强制 UTF-8，否则日志与路径会乱码
          PYTHONUTF8: "1",
          PYTHONIOENCODING: "utf-8",
          PYTHONUNBUFFERED: "1",
          DOCFACTORY_DATA_DIR: this.opts.dataRoot,
          // 与引擎的 trust_env=False 呼应：任何意外的 HTTP 客户端也不走系统代理
          NO_PROXY: "*",
          no_proxy: "*",
        },
      });
    } catch (err) {
      return this.fail(`引擎进程启动失败：${String(err)}`);
    }

    this.child = child;
    child.stdin.end();
    this.attachStderr(child);
    child.on("error", (err) => log.error(`引擎进程错误：${String(err)}`));
    child.on("exit", (code, signal) => this.onExit(child, code, signal));

    const deadline = Date.now() + STARTUP_TIMEOUT_MS;
    try {
      const port = await this.awaitReady(child, deadline);
      await this.awaitHealthy(port, deadline);
      this.update({ port, token, status: "ready" });
      log.info(`引擎就绪：127.0.0.1:${port}`);
      return this.getInfo();
    } catch (err) {
      // 启动失败的半死进程必须清掉，否则端口与 soffice 都会泄漏；
      // 已经退出的就别再 taskkill：既省一条误导性 warn，也避免 pid 复用误伤别的进程
      if (this.child === child) this.child = null;
      if (child.exitCode === null && child.signalCode === null) killProcessTree(child.pid);
      return this.fail(String(err instanceof Error ? err.message : err));
    }
  }

  private fail(reason: string): DfEngineInfo {
    this.lastError = reason;
    // E06：引擎未响应/启动失败（errors.py 注册表），UI 顶栏据此变红并给「重试」
    log.error(`[E06] 引擎启动失败：${reason}`);
    this.update({ status: "down", port: 0 });
    return this.getInfo();
  }

  /** 逐行读 stdout：命中第一行 READY 完成握手，其余全部当日志 */
  private awaitReady(child: ChildProcessWithoutNullStreams, deadline: number): Promise<number> {
    return new Promise<number>((resolve, reject) => {
      let settled = false;
      let handshaked = false;

      const finish = (fn: () => void): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn();
      };

      const timer = setTimeout(
        () => finish(() => reject(new Error("引擎在 15s 内未完成 READY 握手"))),
        Math.max(0, deadline - Date.now()),
      );

      const rl = createInterface({ input: child.stdout });
      rl.on("line", (raw: string) => {
        const line = raw.trim();
        if (!handshaked) {
          const m = /^READY\s+(\{.*\})$/.exec(line);
          if (m) {
            handshaked = true;
            let port = 0;
            try {
              const payload = JSON.parse(m[1]) as { port?: number };
              port = Number(payload.port ?? 0);
            } catch {
              finish(() => reject(new Error(`READY 握手行无法解析：${line}`)));
              return;
            }
            if (!Number.isInteger(port) || port <= 0) {
              finish(() => reject(new Error(`READY 握手端口非法：${line}`)));
              return;
            }
            finish(() => resolve(port));
            return;
          }
        }
        if (line) log.info(`[engine] ${line}`);
      });

      child.once("exit", (code, signal) =>
        finish(() => reject(new Error(`引擎在握手前退出（code=${code} signal=${signal}）`))),
      );
      child.once("error", (err) => finish(() => reject(err)));
    });
  }

  /** READY 之后轮询 /health（免鉴权），200 即视为可服务 */
  private async awaitHealthy(port: number, deadline: number): Promise<void> {
    for (;;) {
      try {
        const res = await requestEngine({ port, token: "" }, "GET", "/health", 2_000);
        if (res.status === 200) return;
      } catch {
        /* 尚未接受连接，继续等 */
      }
      if (Date.now() >= deadline) throw new Error("引擎 /health 探活超时");
      await sleep(200);
    }
  }

  private attachStderr(child: ChildProcessWithoutNullStreams): void {
    const rl = createInterface({ input: child.stderr });
    rl.on("line", (line: string) => {
      if (line.trim()) log.warn(`[engine:err] ${line.trim()}`);
    });
  }

  private onExit(
    child: ChildProcessWithoutNullStreams,
    code: number | null,
    signal: NodeJS.Signals | null,
  ): void {
    if (this.child !== child) return; // 旧进程的退出事件，忽略
    this.child = null;

    if (this.stopping) {
      log.info("引擎已按请求退出");
      this.update({ status: "down", port: 0 });
      return;
    }

    log.warn(`引擎意外退出（code=${code} signal=${signal}）`);
    this.update({ status: "down", port: 0 });

    /* 启动期就崩了：launch() 自己的失败路径会置 down 并记 E06，这里再走一遍配额
     * 只会白白吃掉一次重启额度（而且 start() 会撞上在飞的 launch 直接返回，实际
     * 并不会拉起新进程）。留给 UI 的「重试」处理。 */
    if (this.starting) return;

    // 正常退出码 0 视为引擎自行收尾（例如被外部 /shutdown），不自动拉起
    if (code === 0) return;

    const now = Date.now();
    this.restarts = this.restarts.filter((t) => now - t < RESTART_WINDOW_MS);
    if (this.restarts.length >= RESTART_MAX) {
      this.lastError = `引擎在 ${RESTART_WINDOW_MS / 1000}s 内连续崩溃超过 ${RESTART_MAX} 次`;
      log.error(`[E06] ${this.lastError}，停止自动重启，等待用户手动重试`);
      return;
    }
    this.restarts.push(now);
    log.info(`自动重启引擎（本窗口第 ${this.restarts.length}/${RESTART_MAX} 次）`);
    void this.start().catch((err) => log.error(`自动重启失败：${String(err)}`));
  }

  private waitExit(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<boolean> {
    if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
    return new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), timeoutMs);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve(true);
      });
    });
  }
}
