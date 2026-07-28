/* 检查更新：向 GitHub Releases 询问最新版本（08 章 §2 分发渠道）。
 *
 * 与离线纪律（02 章 §7 / FR-17）的关系——三条边界划得很死：
 * - **只有用户点「检查更新」才出网**：没有启动时自检、没有定时器、没有遥测。
 *   出网期间经 net-guard.withRemoteHostsAllowed 临时放行 api.github.com，
 *   请求结束立即收回，放行与收回都写日志留痕。
 * - **只读一个公开端点**：GET /repos/{owner}/{repo}/releases/latest，
 *   不带任何本机信息（无 token、无查询参数，User-Agent 只有产品名与版本）。
 * - **不做应用内下载与静默安装**：发现新版只把 Release 页面交给系统默认浏览器
 *   （shell.openExternal，URL 白名单限定在本仓库 releases 下），
 *   下载什么、装不装由用户在浏览器里自己决定。
 */

import https from "node:https";

import { app, shell } from "electron";
import log from "electron-log/main";

import { withRemoteHostsAllowed } from "./net-guard";

/** 发布仓库（与 git remote / CI release job 保持一致） */
const REPO_OWNER = "ohaoz";
const REPO_NAME = "rag-app";
const API_HOST = "api.github.com";
/** openExternal 的 URL 白名单前缀：只允许打开本仓库的 Release 页面 */
const RELEASE_PAGE_PREFIX = `https://github.com/${REPO_OWNER}/${REPO_NAME}/releases`;

const REQUEST_TIMEOUT_MS = 10_000;
/** Release Notes 给 UI 的截断长度：设置页只展示摘要，全文去 Release 页看 */
const NOTES_MAX_CHARS = 600;

export interface UpdateCheckResult {
  current: string;
  latest: string | null;
  hasUpdate: boolean;
  /** Release 页面地址（发现新版时用于「去下载」） */
  url: string | null;
  notes: string | null;
  /** 面向用户的一句话错误；null 表示检查成功 */
  error: string | null;
}

function httpsGetJson(host: string, path: string): Promise<{ status: number; body: unknown }> {
  return new Promise((resolvePromise, reject) => {
    const req = https.get(
      {
        host,
        path,
        headers: {
          // GitHub API 要求 UA；不带任何机器指纹
          "User-Agent": `DocFactory/${app.getVersion()}`,
          Accept: "application/vnd.github+json",
        },
        timeout: REQUEST_TIMEOUT_MS,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf-8");
          try {
            resolvePromise({ status: res.statusCode ?? 0, body: text ? JSON.parse(text) : null });
          } catch {
            reject(new Error("响应不是有效的 JSON"));
          }
        });
      },
    );
    req.on("timeout", () => {
      req.destroy(new Error("请求超时"));
    });
    req.on("error", reject);
  });
}

/** "v1.2.3" / "1.2.3-beta" → [1,2,3]；解析不了返回 null（比较时视为不可比） */
function parseVersion(tag: string): [number, number, number] | null {
  const m = tag.trim().replace(/^v/i, "").match(/^(\d+)\.(\d+)\.(\d+)/);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function isNewer(latest: string, current: string): boolean {
  const a = parseVersion(latest);
  const b = parseVersion(current);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i += 1) {
    const l = a[i] as number;
    const c = b[i] as number;
    if (l !== c) return l > c;
  }
  return false;
}

export async function checkForUpdates(): Promise<UpdateCheckResult> {
  const current = app.getVersion();
  const base: UpdateCheckResult = {
    current,
    latest: null,
    hasUpdate: false,
    url: null,
    notes: null,
    error: null,
  };
  try {
    const { status, body } = await withRemoteHostsAllowed([API_HOST], () =>
      httpsGetJson(API_HOST, `/repos/${REPO_OWNER}/${REPO_NAME}/releases/latest`),
    );
    if (status === 404) {
      // 仓库还没有任何正式 Release：不算错误，如实告知
      return { ...base, error: "尚未发布过正式版本" };
    }
    if (status !== 200 || !body || typeof body !== "object") {
      return { ...base, error: `更新服务返回异常（HTTP ${status}）` };
    }
    const r = body as Record<string, unknown>;
    const tag = typeof r["tag_name"] === "string" ? r["tag_name"] : null;
    if (!tag) return { ...base, error: "未能识别最新版本号" };

    const latest = tag.replace(/^v/i, "");
    const url = typeof r["html_url"] === "string" ? r["html_url"] : RELEASE_PAGE_PREFIX;
    const rawNotes = typeof r["body"] === "string" ? r["body"] : "";
    const notes =
      rawNotes.length > NOTES_MAX_CHARS ? `${rawNotes.slice(0, NOTES_MAX_CHARS)}…` : rawNotes;

    const hasUpdate = isNewer(latest, current);
    log.info(`[update] 检查完成：当前 ${current}，最新 ${latest}${hasUpdate ? "（有新版）" : ""}`);
    return { ...base, latest, hasUpdate, url, notes: notes || null };
  } catch (err) {
    const msg = String(err instanceof Error ? err.message : err);
    log.warn(`[update] 检查失败：${msg}`);
    // 最常见的失败就是没网/被防火墙拦——这在离线产品的目标环境里是常态，不是事故
    return { ...base, error: "无法连接更新服务（可能当前无外网）" };
  }
}

/** 打开 Release 下载页：URL 必须落在本仓库 releases 下，其余一律拒绝 */
export async function openDownloadPage(url: string): Promise<void> {
  if (!url.startsWith(RELEASE_PAGE_PREFIX)) {
    throw new Error(`拒绝打开非发布页地址：${url}`);
  }
  log.info(`[update] 用系统浏览器打开下载页：${url}`);
  await shell.openExternal(url);
}
