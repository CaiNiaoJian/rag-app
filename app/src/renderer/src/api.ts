/* 引擎 HTTP 客户端（02 章 §2 IPC 协议）。
 * - 地址与凭据来自 preload：window.df.engine.getInfo() 的 {port, token}，每次启动随机。
 * - 所有请求带 Authorization: Bearer；仅访问 127.0.0.1 回环（离线约束）。
 * - SSE 用 fetch + ReadableStream 手写解析（event:/data: 行）；断线自动放弃，
 *   由页面的定时轮询兜底（进度条降级为 5s 粒度，不弹错）。
 */

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

  /* 引擎状态变化（含重启换端口/换 token）后由 App 调用作废缓存 */
  invalidate(): void {
    this.info = null;
  }

  private async base(): Promise<{ url: string; token: string }> {
    if (!this.info || this.info.status !== "ready" || !this.info.port) {
      this.info = await window.df.engine.getInfo();
    }
    return { url: `http://127.0.0.1:${this.info.port}`, token: this.info.token };
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
