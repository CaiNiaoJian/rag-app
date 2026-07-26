/* 诊断包导出（08 章 §1 日志架构最后一行）。
 *
 * 分工：**引擎负责打包，主进程负责另存**。
 * 理由：诊断包要含 logs\ + 系统信息 + 最近 task_events，其中 task_events 只有引擎能查；
 * 而 Electron 侧的 app-*.log 与引擎的 engine-*.jsonl 被刻意放在同一个 logs\ 目录
 * （见 index.ts 的日志初始化），所以引擎打整个目录时天然把两侧日志一把抓，
 * 主进程不需要再引入任何 zip 依赖（package.json 冻结，也确实没有可用的压缩库）。
 *
 * 引擎不可用时的降级：调 Windows 自带的 Compress-Archive 现场打一个（只含 logs\ +
 * 系统信息，缺 task_events），保证「引擎起不来」这种最需要诊断的场景反而能出包。
 *
 * 隐私：日志本身只记文件名与元数据、不含文档内容（08 章），因此这里不做二次脱敏；
 * 诊断包只落到用户自选的本地路径，不出机器。
 */

import { spawn } from "node:child_process";
import { copyFile, mkdir, mkdtemp, readdir, rm, stat, statfs, writeFile } from "node:fs/promises";
import { cpus, freemem, totalmem, release, arch, tmpdir } from "node:os";
import { basename, join } from "node:path";

import { app, dialog, type BrowserWindow } from "electron";
import log from "electron-log/main";

import { requestEngine, type EngineSupervisor } from "./engine-supervisor";

/** 降级打包时纳入的日志总量上限，避免长稳测试后攒出几百 MB */
const MAX_FALLBACK_BYTES = 64 * 1024 * 1024;

export interface DiagnosticsOptions {
  supervisor: EngineSupervisor;
  dataRoot: string;
  parent: BrowserWindow | null;
}

function timestamp(): string {
  const d = new Date();
  const p = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

/** 系统信息（08 章：OS 版本/CPU/内存/磁盘），降级打包时随包附上 */
async function collectSystemInfo(dataRoot: string): Promise<Record<string, unknown>> {
  const info: Record<string, unknown> = {
    generated_at: new Date().toISOString(),
    app_version: app.getVersion(),
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    platform: process.platform,
    os_release: release(),
    arch: arch(),
    cpu_count: cpus().length,
    cpu_model: cpus()[0]?.model ?? "unknown",
    total_mem_mb: Math.round(totalmem() / 1024 / 1024),
    free_mem_mb: Math.round(freemem() / 1024 / 1024),
    data_root: dataRoot,
    locale: app.getLocale(),
  };
  try {
    const fsStat = await statfs(dataRoot);
    info["disk_free_mb"] = Math.round((fsStat.bfree * fsStat.bsize) / 1024 / 1024);
    info["disk_total_mb"] = Math.round((fsStat.blocks * fsStat.bsize) / 1024 / 1024);
  } catch {
    /* 老 Node / 特殊卷上取不到磁盘信息时略过，不影响出包 */
  }
  return info;
}

/** 从引擎响应里找出 zip 路径：优先常见键名，兜底扫描任意 .zip 字符串值 */
async function pickZipPath(body: string): Promise<string | null> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const flat = parsed as Record<string, unknown>;
  const nested = (flat["data"] ?? flat["result"]) as Record<string, unknown> | undefined;
  const candidates: unknown[] = [];
  for (const src of [flat, nested]) {
    if (!src || typeof src !== "object") continue;
    for (const key of ["path", "zip_path", "file", "out_path", "output", "zip"]) {
      candidates.push((src as Record<string, unknown>)[key]);
    }
    candidates.push(...Object.values(src));
  }
  for (const c of candidates) {
    if (typeof c !== "string" || !c.toLowerCase().endsWith(".zip")) continue;
    try {
      await stat(c);
      return c;
    } catch {
      /* 路径不存在，继续找 */
    }
  }
  return null;
}

/** 让引擎打包（POST /logs/diagnostics），返回引擎侧生成的 zip 路径 */
async function packViaEngine(supervisor: EngineSupervisor): Promise<string | null> {
  const info = supervisor.getInfo();
  if (info.status !== "ready" || info.port <= 0) return null;
  try {
    // 打包要读整个 logs 目录，给足 60s
    const res = await requestEngine(
      { port: info.port, token: info.token },
      "POST",
      "/logs/diagnostics",
      60_000,
      {},
    );
    if (res.status !== 200) {
      log.warn(`引擎诊断包接口返回 ${res.status}，转降级打包`);
      return null;
    }
    return await pickZipPath(res.body);
  } catch (err) {
    log.warn(`调用引擎诊断包接口失败：${String(err)}`);
    return null;
  }
}

/** 降级打包：临时目录汇总 logs + 系统信息，再用 Compress-Archive 压成 zip */
async function packLocally(dataRoot: string): Promise<string | null> {
  if (process.platform !== "win32") {
    log.error("当前平台缺少可用的压缩手段，无法降级生成诊断包");
    return null;
  }
  const staging = await mkdtemp(join(tmpdir(), "docfactory-diag-"));
  const logsOut = join(staging, "logs");
  await mkdir(logsOut, { recursive: true });

  const logsDir = join(dataRoot, "logs");
  let copied = 0;
  let bytes = 0;
  try {
    const names = await readdir(logsDir);
    // 新文件优先：超限时保留最近的日志，最旧的丢弃
    const entries: Array<{ name: string; mtime: number; size: number }> = [];
    for (const name of names) {
      if (!/\.(log|jsonl|txt)$/i.test(name)) continue;
      try {
        const st = await stat(join(logsDir, name));
        entries.push({ name, mtime: st.mtimeMs, size: st.size });
      } catch {
        /* 正在被写入/已删除，跳过 */
      }
    }
    entries.sort((a, b) => b.mtime - a.mtime);
    for (const e of entries) {
      if (bytes + e.size > MAX_FALLBACK_BYTES) continue;
      try {
        await copyFile(join(logsDir, e.name), join(logsOut, e.name));
        bytes += e.size;
        copied += 1;
      } catch (err) {
        log.warn(`复制日志失败 ${e.name}：${String(err)}`);
      }
    }
  } catch (err) {
    log.warn(`读取日志目录失败：${String(err)}`);
  }

  await writeFile(
    join(staging, "system-info.json"),
    JSON.stringify(await collectSystemInfo(dataRoot), null, 2),
    "utf-8",
  );
  await writeFile(
    join(staging, "README.txt"),
    [
      "DocFactory 诊断包（降级模式）",
      "",
      "引擎不可用，本包由 Electron 主进程现场生成，仅含日志文件与系统信息，",
      "不包含 task_events 明细。日志不含文档内容，仅记录文件名与元数据。",
      `已收录日志文件：${copied} 个`,
    ].join("\r\n"),
    "utf-8",
  );

  const zipPath = join(staging, `DocFactory-诊断包-${timestamp()}.zip`);
  const ok = await compressWithPowerShell(staging, zipPath);
  return ok ? zipPath : null;
}

/** 调用 Windows 自带 PowerShell 压缩（不引入任何第三方依赖，纯本机操作） */
function compressWithPowerShell(srcDir: string, zipPath: string): Promise<boolean> {
  return new Promise((resolve) => {
    const script =
      `$ErrorActionPreference='Stop';` +
      `Compress-Archive -Path (Join-Path -Path '${srcDir.replace(/'/g, "''")}' -ChildPath '*') ` +
      `-DestinationPath '${zipPath.replace(/'/g, "''")}' -Force`;
    const child = spawn(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
      { windowsHide: true },
    );
    let stderr = "";
    child.stderr.on("data", (c: Buffer) => (stderr += c.toString()));
    child.on("error", (err) => {
      log.error(`调用 PowerShell 压缩失败：${String(err)}`);
      resolve(false);
    });
    child.on("exit", (code) => {
      if (code === 0) {
        resolve(true);
        return;
      }
      log.error(`Compress-Archive 退出码 ${code}：${stderr.trim()}`);
      resolve(false);
    });
  });
}

/**
 * 导出诊断包：生成 → 另存对话框 → 复制到用户选择的位置。
 * 返回最终落盘路径；用户取消返回 null。
 */
export async function exportDiagnosticsZip(opts: DiagnosticsOptions): Promise<string | null> {
  log.info("开始导出诊断包");
  let source = await packViaEngine(opts.supervisor);
  let temporary = false;
  if (!source) {
    source = await packLocally(opts.dataRoot);
    temporary = true;
  }
  if (!source) throw new Error("诊断包生成失败，请查看 logs 目录下的 app 日志");

  const defaultName = basename(source).toLowerCase().endsWith(".zip")
    ? basename(source)
    : `DocFactory-诊断包-${timestamp()}.zip`;
  let defaultPath = defaultName;
  try {
    defaultPath = join(app.getPath("desktop"), defaultName);
  } catch {
    /* 无桌面路径（精简系统）时退回纯文件名 */
  }

  const result = opts.parent
    ? await dialog.showSaveDialog(opts.parent, {
        title: "导出诊断包",
        defaultPath,
        filters: [{ name: "压缩包", extensions: ["zip"] }],
      })
    : await dialog.showSaveDialog({
        title: "导出诊断包",
        defaultPath,
        filters: [{ name: "压缩包", extensions: ["zip"] }],
      });

  if (result.canceled || !result.filePath) {
    if (temporary) await cleanupTemp(source);
    log.info("用户取消了诊断包导出");
    return null;
  }

  await copyFile(source, result.filePath);
  if (temporary) await cleanupTemp(source);
  log.info(`诊断包已导出：${result.filePath}`);
  return result.filePath;
}

/** 清理降级打包用的临时目录（zip 就在该目录里，直接删父目录） */
async function cleanupTemp(zipPath: string): Promise<void> {
  try {
    await rm(join(zipPath, ".."), { recursive: true, force: true });
  } catch (err) {
    log.warn(`清理诊断包临时目录失败：${String(err)}`);
  }
}
