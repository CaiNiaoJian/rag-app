/* 引擎 HTTP 客户端（02 章 §2 IPC 协议）。
 * - 地址与凭据来自 preload：window.df.engine.getInfo() 的 {port, token}，每次启动随机。
 * - 所有请求带 Authorization: Bearer；仅访问 127.0.0.1 回环（离线约束）。
 * - SSE 用 fetch + ReadableStream 手写解析（event:/data: 行）；断线自动放弃，
 *   由页面的定时轮询兜底（进度条降级为 5s 粒度，不弹错）。
 *
 * **启动竞态**：窗口先于引擎就绪（引擎要跑迁移与自检，实测约 2s），而六个页面在
 * mount 那一刻就开始拉数据。若此时拿 status="starting" 的 port=0 去拼 URL，请求
 * 会被 net-guard 正确拦成非白名单地址，页面停在「读取失败」且不会自愈 ——
 * 所以 base() 必须**等引擎就绪**而不是拿当前值就走。等待收敛在这一处，
 * 六个页面与后续所有调用点都不必各自处理，引擎重启换端口时同样受益。
 */

/** 等待引擎就绪的上限：主进程侧启动超时是 15s，这里留一点余量 */
const READY_TIMEOUT_MS = 20_000;
const READY_POLL_MS = 150;

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export class ApiError extends Error {
  status: number;
  body: Record<string, unknown> | null;

  constructor(status: number, body: Record<string, unknown> | null, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }

  /* 引擎错误响应统一带 error_code/user_message/suggestion（errors.py error_payload） */
  get errorCode(): string | null {
    const c = this.body?.["error_code"];
    return typeof c === "string" ? c : null;
  }

  get userMessage(): string | null {
    const m = this.body?.["user_message"] ?? this.body?.["detail"];
    return typeof m === "string" ? m : null;
  }
}

export type SseHandlers = Partial<Record<string, (data: Record<string, unknown>) => void>> & {
  /* 流结束（正常/断线均触发一次），供调用方切换轮询节奏 */
  onEnd?: () => void;
};

export class EngineClient {
  private info: DfEngineInfo | null = null;
  /* 并发等待去重：首屏六个页面同时发请求时只跑一轮轮询，而不是六轮 */
  private pending: Promise<DfEngineInfo> | null = null;

  /* 引擎状态变化（含重启换端口/换 token）后由 App 调用作废缓存 */
  invalidate(): void {
    this.info = null;
  }

  private static isUsable(info: DfEngineInfo | null): info is DfEngineInfo {
    return info !== null && info.status === "ready" && info.port > 0;
  }

  /* 轮询到引擎就绪。status="down" 直接失败：supervisor 重启期间是 "starting"，
   * 停在 "down" 意味着它已经放弃重试，再等下去只是让用户干等满超时。 */
  private async waitForReady(): Promise<DfEngineInfo> {
    if (typeof window.df === "undefined") {
      // renderer 被单独在浏览器里打开（无 preload）时给出可诊断的说法，而不是 TypeError
      throw new ApiError(0, null, "运行环境缺少本地引擎桥接");
    }
    const deadline = Date.now() + READY_TIMEOUT_MS;
    for (;;) {
      const info = await window.df.engine.getInfo();
      // 这里不复用 isUsable：它是 type predicate，会把 else 分支收窄成 never
      if (info.status === "ready" && info.port > 0) {
        this.info = info;
        return info;
      }
      if (info.status === "down") {
        throw new ApiError(0, null, "本地引擎未运行，可在顶栏重启引擎");
      }
      if (Date.now() >= deadline) {
        throw new ApiError(0, null, "本地引擎启动超时");
      }
      await sleep(READY_POLL_MS);
    }
  }

  private async base(): Promise<{ url: string; token: string }> {
    let info = this.info;
    if (!EngineClient.isUsable(info)) {
      this.pending ??= this.waitForReady().finally(() => {
        this.pending = null;
      });
      info = await this.pending;
    }
    return { url: `http://127.0.0.1:${info.port}`, token: info.token };
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const { url, token } = await this.base();
    let res: Response;
    try {
      res = await fetch(url + path, {
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch {
      /* 连接失败（引擎未就绪/正在重启）：作废缓存，下次重取端口 */
      this.invalidate();
      throw new ApiError(0, null, "无法连接本地引擎");
    }
    if (!res.ok) {
      let parsed: Record<string, unknown> | null = null;
      try {
        parsed = (await res.json()) as Record<string, unknown>;
      } catch {
        parsed = null;
      }
      if (res.status === 401) this.invalidate();
      throw new ApiError(res.status, parsed, `请求失败（${res.status}）`);
    }
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  getJson<T>(path: string): Promise<T> {
    return this.request<T>("GET", path);
  }

  postJson<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("POST", path, body ?? {});
  }

  putJson<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>("PUT", path, body);
  }

  del<T>(path: string): Promise<T> {
    return this.request<T>("DELETE", path);
  }

  /* 订阅任务 SSE 进度流（GET /tasks/{id}/events）。
   * 返回取消函数；任何网络异常静默结束（onEnd 通知），不重连。 */
  sse(taskId: string, handlers: SseHandlers): () => void {
    const ac = new AbortController();

    const dispatch = (event: string, raw: string) => {
      let data: Record<string, unknown> = {};
      if (raw) {
        try {
          const parsed = JSON.parse(raw) as unknown;
          if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
        } catch {
          data = { raw };
        }
      }
      handlers[event]?.(data);
    };

    void (async () => {
      try {
        const { url, token } = await this.base();
        const res = await fetch(`${url}/tasks/${encodeURIComponent(taskId)}/events`, {
          headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
          signal: ac.signal,
        });
        if (!res.ok || !res.body) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          /* 事件块以空行分隔；统一处理 \r\n 与 \n */
          for (;;) {
            const m = /\r?\n\r?\n/.exec(buf);
            if (!m) break;
            const block = buf.slice(0, m.index);
            buf = buf.slice(m.index + m[0].length);
            let eventName = "message";
            const dataLines: string[] = [];
            for (const rawLine of block.split(/\r?\n/)) {
              if (rawLine.startsWith("event:")) eventName = rawLine.slice(6).trim();
              else if (rawLine.startsWith("data:")) dataLines.push(rawLine.slice(5).replace(/^ /, ""));
              /* 其余行（id:/retry:/注释）忽略 */
            }
            dispatch(eventName, dataLines.join("\n"));
          }
        }
      } catch {
        /* 断线/中止：放弃，交由轮询兜底 */
      } finally {
        try {
          handlers.onEnd?.();
        } catch {
          /* 收尾回调异常不外抛 */
        }
      }
    })();

    return () => ac.abort();
  }
}
