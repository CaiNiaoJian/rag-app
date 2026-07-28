/* UI 截图工具（开发期，不进发布包）。
 *
 * 为什么需要它：桌面应用的视觉改动没法靠读 CSS 判断成败，而每次手工启动应用、
 * 造数据、逐页翻看再截图，成本高到没人愿意做第二遍 —— 于是视觉回归就没人看了。
 * 这个脚本用无头 Electron 加载构建产物，注入假的 window.df 与 fetch，
 * 把六个页面各截一张图，几秒钟跑完。
 *
 * 假数据刻意「有内容且不完美」：既有成功也有警告和失败、名字有长有短、中英混排、
 * 覆盖率有高有低。空页面看不出设计问题，全绿的页面同样看不出。
 *
 * 用法（必须用 electron 跑，不能用 node）：
 *   npm run build && npx electron tools/ui-shot.mjs [输出目录] [--page=workbench]
 *   或一步到位：npm run ui:shot
 * 下面 import 的是 Electron 内建的 CJS 模块，node 解析 ESM 具名导入时拿不到
 * BrowserWindow，会直接报 SyntaxError: Named export 'BrowserWindow' not found。
 */

import { app, BrowserWindow } from "electron";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { mkdir, writeFile } from "node:fs/promises";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_DIR = resolve(HERE, "..");

const args = process.argv.slice(2).filter((a) => !a.startsWith("--"));
const flags = process.argv.slice(2).filter((a) => a.startsWith("--"));
const OUT_DIR = args[0] ? resolve(args[0]) : join(APP_DIR, ".ui-shots");
const onlyPage = flags.find((f) => f.startsWith("--page="))?.split("=")[1] ?? null;
/* --theme=dark：强制深色主题截图（深色回归也能一条命令跑完） */
const themeFlag = flags.find((f) => f.startsWith("--theme="))?.split("=")[1] ?? null;

/* 1440x900 是这类桌面工具最常见的窗口尺寸；deviceScaleFactor=2 出的图放大看得清抗锯齿 */
const WIDTH = 1440;
const HEIGHT = 900;

const PAGES = ["workbench", "library", "export", "dashboard", "logs", "settings"];

// ---------------------------------------------------------------- 假数据

const DOCS = [
  { id: "d1", name: "2026年度技术服务合同（终稿）.docx", fmt: "docx", size: 284_912, status: "ok",
    page_cnt: 18, parse_level: "L0", text_coverage: 0.996, table_confidence: 0.94,
    ocr_confidence: null, degraded_pages: 0, created_at: "2026-07-26T09:12:00+08:00",
    parsed_at: "2026-07-26T09:12:48+08:00", ir_version: "1.0", hash: "a".repeat(64), src_path: "C:\\docs\\合同.docx" },
  { id: "d2", name: "Q3 区域经营分析.pptx", fmt: "pptx", size: 4_182_390, status: "ok",
    page_cnt: 42, parse_level: "L0", text_coverage: 1.0, table_confidence: 0.91,
    ocr_confidence: null, degraded_pages: 0, created_at: "2026-07-26T09:14:00+08:00",
    parsed_at: "2026-07-26T09:15:31+08:00", ir_version: "1.0", hash: "b".repeat(64), src_path: "C:\\docs\\q3.pptx" },
  { id: "d3", name: "设备采购明细表_2026H1.xlsx", fmt: "xlsx", size: 1_204_112, status: "warning",
    page_cnt: 6, parse_level: "L0", text_coverage: 0.981, table_confidence: 0.87,
    ocr_confidence: null, degraded_pages: 0, created_at: "2026-07-26T09:20:00+08:00",
    parsed_at: "2026-07-26T09:20:52+08:00", ir_version: "1.0", hash: "c".repeat(64), src_path: "C:\\docs\\采购.xlsx" },
  { id: "d4", name: "扫描件-历史档案-1998.pdf", fmt: "pdf", size: 22_481_002, status: "warning",
    page_cnt: 124, parse_level: "L2", text_coverage: 0.72, table_confidence: null,
    ocr_confidence: 0.58, degraded_pages: 124, created_at: "2026-07-26T10:02:00+08:00",
    parsed_at: "2026-07-26T10:31:09+08:00", ir_version: "1.0", hash: "d".repeat(64), src_path: "C:\\docs\\1998.pdf" },
  { id: "d5", name: "Technical Whitepaper v3 (EN).pdf", fmt: "pdf", size: 8_120_338, status: "ok",
    page_cnt: 66, parse_level: "L1", text_coverage: 0.984, table_confidence: 0.9,
    ocr_confidence: null, degraded_pages: 3, created_at: "2026-07-26T11:41:00+08:00",
    parsed_at: "2026-07-26T11:45:12+08:00", ir_version: "1.0", hash: "e".repeat(64), src_path: "C:\\docs\\wp.pdf" },
  { id: "d6", name: "旧版会议纪要.doc", fmt: "doc", size: 96_204, status: "failed",
    page_cnt: null, parse_level: null, text_coverage: null, table_confidence: null,
    ocr_confidence: null, degraded_pages: 0, created_at: "2026-07-26T11:52:00+08:00",
    parsed_at: null, ir_version: null, hash: "f".repeat(64), src_path: "C:\\docs\\纪要.doc" },
];

const TASKS = [
  { id: "t1", doc_id: "d4", type: "parse", status: "running", progress: 0.63, stage: "ocr",
    error_code: null, payload_json: "{}", started_at: "2026-07-26T10:02:00+08:00",
    ended_at: null, created_at: "2026-07-26T10:02:00+08:00" },
  { id: "t2", doc_id: "d5", type: "parse", status: "running", progress: 0.21, stage: "parse",
    error_code: null, payload_json: "{}", started_at: "2026-07-26T11:41:00+08:00",
    ended_at: null, created_at: "2026-07-26T11:41:00+08:00" },
  { id: "t3", doc_id: "d6", type: "parse", status: "failed", progress: 0, stage: "convert",
    error_code: "E03", payload_json: "{}", started_at: "2026-07-26T11:52:00+08:00",
    ended_at: "2026-07-26T11:52:04+08:00", created_at: "2026-07-26T11:52:00+08:00" },
  { id: "t4", doc_id: "d1", type: "parse", status: "done", progress: 1, stage: "chunk",
    error_code: null, payload_json: "{}", started_at: "2026-07-26T09:12:00+08:00",
    ended_at: "2026-07-26T09:12:48+08:00", created_at: "2026-07-26T09:12:00+08:00" },
  { id: "t5", doc_id: "d2", type: "export", status: "done", progress: 1, stage: "export",
    error_code: null, payload_json: "{}", started_at: "2026-07-26T09:16:00+08:00",
    ended_at: "2026-07-26T09:16:07+08:00", created_at: "2026-07-26T09:16:00+08:00" },
  { id: "t6", doc_id: "d3", type: "rechunk", status: "queued", progress: 0, stage: null,
    error_code: null, payload_json: "{}", started_at: null, ended_at: null,
    created_at: "2026-07-26T11:58:00+08:00" },
];

const LOGS = [
  { id: 9, task_id: "t3", doc_id: "d6", level: "error", code: "E03", stage: "convert", page: null,
    message: "旧格式转换失败：LibreOffice 退出码 137（超时被终止）", detail_json: null, ts: "2026-07-26T11:52:04+08:00" },
  { id: 8, task_id: "t1", doc_id: "d4", level: "warning", code: "DGR-L2", stage: "parse", page: 87,
    message: "第 87 页降级到 L2（low_confidence）", detail_json: null, ts: "2026-07-26T10:18:22+08:00" },
  { id: 7, task_id: "t1", doc_id: "d4", level: "warning", code: "E04", stage: "ocr", page: null,
    message: "页均 OCR 置信 0.58，低于 0.6，建议检查预览中的黄色高亮区域", detail_json: null, ts: "2026-07-26T10:31:00+08:00" },
  { id: 6, task_id: "t4", doc_id: "d1", level: "info", code: null, stage: "chunk", page: null,
    message: "切片完成：48 个 child / 12 个 parent", detail_json: null, ts: "2026-07-26T09:12:48+08:00" },
  { id: 5, task_id: "t4", doc_id: "d1", level: "info", code: null, stage: "parse", page: null,
    message: "解析完成，文本覆盖率 99.6%", detail_json: null, ts: "2026-07-26T09:12:40+08:00" },
  { id: 4, task_id: "t2", doc_id: "d5", level: "warning", code: "DGR-L1", stage: "parse", page: 12,
    message: "第 12 页降级到 L1（timeout）", detail_json: null, ts: "2026-07-26T11:43:02+08:00" },
  { id: 3, task_id: null, doc_id: null, level: "info", code: null, stage: null, page: null,
    message: "模组 ocr-hp v2.3 安装完成，重启引擎后生效", detail_json: null, ts: "2026-07-26T08:30:00+08:00" },
];

const CHUNKS = Array.from({ length: 12 }, (_, i) => ({
  id: `c-${String(i + 1).padStart(4, "0")}`, doc_id: "d1", seq: i, parent_id: "p-0001",
  kind: i % 5 === 0 ? "parent" : "child", type: i % 4 === 2 ? "table" : "text",
  text: i % 4 === 2
    ? "项目\t规格\t金额\n服务器\t2U 机架式\t120000\n交换机\t48 口千兆\t18000"
    : "乙方应于合同签订之日起 90 日内完成全部交付物的开发、测试与部署，并向甲方提交验收报告。逾期交付的，每逾期一日按合同总金额的千分之三支付违约金。",
  token_count: 380 + i * 17, char_count: 612 + i * 29,
  heading_path: i < 4 ? "第1章 总则>1.1 定义" : "第2章 交付条款>2.3 交付期限",
  pages: "[3,4]", node_ids: '["n12","n13"]', meta_json: "{}", hash: "sha256:abc",
}));

const DASHBOARD = {
  cards: {
    docs_total: 6, documents: 6, imported: 6, chunks_total: 386, chunk_cnt: 386,
    docs_recent: 5, parsed_ok: 3, parsed_warn: 2, parsed_fail: 1,
    ocr_pages: 124, avg_coverage: 0.933, text_coverage: 0.933, avg_ms: 32_400,
  },
  fmt_dist: [
    { label: "pdf", count: 2 }, { label: "docx", count: 1 },
    { label: "pptx", count: 1 }, { label: "xlsx", count: 1 }, { label: "doc", count: 1 },
  ],
  status_dist: [
    { label: "ok", count: 3 }, { label: "warning", count: 2 }, { label: "failed", count: 1 },
  ],
  level_dist: [
    { label: "L0", count: 3 }, { label: "L1", count: 1 }, { label: "L2", count: 1 },
  ],
  chunk_hist: [
    { label: "0-256", count: 42 }, { label: "256-512", count: 168 },
    { label: "512-768", count: 121 }, { label: "768-1024", count: 55 },
  ],
  chunk_per_doc: [
    { label: "合同（终稿）", count: 48 }, { label: "Q3 区域经营分析", count: 96 },
    { label: "设备采购明细", count: 64 }, { label: "历史档案 1998", count: 122 },
    { label: "Whitepaper v3", count: 56 },
  ],
  fail_top: [
    { label: "E03", count: 4 }, { label: "E04", count: 3 },
    { label: "E01", count: 2 }, { label: "E06", count: 1 },
  ],
  duration: {
    by_type: [
      { label: "parse", value: 41_200, samples: 6 },
      { label: "export", value: 7_100, samples: 3 },
      { label: "rechunk", value: 900, samples: 2 },
    ],
  },
  trend: Array.from({ length: 14 }, (_, i) => {
    const d = new Date(Date.UTC(2026, 6, 14 + i));
    return {
      day: d.toISOString().slice(0, 10),
      imported: [0, 2, 5, 3, 8, 12, 6, 4, 9, 14, 7, 3, 11, 6][i],
      parsed_ok: [0, 2, 4, 3, 7, 10, 5, 4, 8, 12, 6, 3, 9, 5][i],
      parsed_warn: [0, 0, 1, 0, 1, 2, 1, 0, 1, 1, 1, 0, 2, 1][i],
      parsed_fail: [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0][i],
      chunk_cnt: [0, 24, 61, 38, 96, 148, 72, 51, 110, 173, 84, 36, 132, 71][i],
      ocr_pages: [0, 0, 0, 0, 12, 0, 0, 0, 0, 124, 0, 0, 0, 0][i],
      text_coverage: [null, 0.99, 0.97, 0.99, 0.95, 0.98, 0.99, 0.99, 0.96, 0.72, 0.98, 0.99, 0.97, 0.98][i],
      docs_created: [0, 2, 5, 3, 8, 12, 6, 4, 9, 14, 7, 3, 11, 6][i],
    };
  }),
};

const SETTINGS = {
  ocr_mode: "on", degrade_policy: "auto", page_timeout_s: 30, parallel_tasks: 4,
  output_dir: null,
  chunk: { target_tokens: 512, max_tokens: 1024, overlap: 0.12, split_by_heading: true,
           table_atomic: true, drop_header_footer: true, footnote_to_end: true },
  pdf_export: { font_size: 12, header_footer: true },
  dataset: { format: "alpaca", file_format: "json", mode: "blank", per_chunk: 1 },
};

const MODULES = {
  modules: [
    { id: "ocr-hp", name: "高精度 OCR（PP-OCRv5 server）", type: "ocr", version: "2.3",
      prev_version: "2.1", enabled: true, installed_at: "2026-07-26T08:30:00+08:00",
      dir_ok: true, rollbackable: true, manifest: null },
    { id: "layout-accurate", name: "TableFormer accurate", type: "parser", version: "1.4",
      prev_version: null, enabled: true, installed_at: "2026-07-20T14:02:00+08:00",
      dir_ok: true, rollbackable: false, manifest: null },
  ],
};

// ---------------------------------------------------------------- 主流程

async function shoot() {
  await mkdir(OUT_DIR, { recursive: true });
  const targets = onlyPage ? [onlyPage] : PAGES;

  // 假数据经环境变量交给 preload：preload 早于一切页面脚本，React 首帧就能拿到数据，
  // 截出来的才是设计本身而不是「正在等待引擎就绪」的加载态
  process.env.DF_MOCK_DATA = JSON.stringify({ DOCS, TASKS, LOGS, CHUNKS, DASHBOARD, SETTINGS, MODULES });

  const win = new BrowserWindow({
    width: WIDTH, height: HEIGHT, show: false,
    webPreferences: {
      preload: join(HERE, "mock-preload.cjs"),
      sandbox: false, contextIsolation: false, nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  await win.webContents.session.setProxy({ mode: "direct" });
  await win.loadFile(join(APP_DIR, "out", "renderer", "index.html"));

  if (themeFlag) {
    /* 直接改 data-theme：App 的主题 effect 只在偏好变化时重跑，截图期间不会覆盖它 */
    await win.webContents.executeJavaScript(
      `document.documentElement.dataset.theme = ${JSON.stringify(themeFlag)}; 1`
    ).catch(() => {});
  }

  const written = [];
  for (const page of targets) {
    // 点左侧导航切页：走真实交互路径，顺带能发现导航本身的问题
    const hit = await win.webContents.executeJavaScript(
      `(() => { const b = document.querySelector('[data-page="${page}"]'); if (b) b.click(); return !!b; })()`
    ).catch(() => false);
    if (!hit) console.warn(`  ! 未找到导航按钮 [data-page="${page}"]`);

    // 等到导航真的切过去为止。固定 sleep 会在 React 还没提交时就截图，
    // 拍出来的是上一页 —— 这种「截图和文件名对不上」的假象最难发现。
    const switched = await win.webContents.executeJavaScript(`
      new Promise((resolve) => {
        const deadline = Date.now() + 4000;
        const tick = () => {
          const active = document.querySelector('.sidebar-item-active');
          if (active && active.getAttribute('data-page') === '${page}') return resolve(true);
          if (Date.now() > deadline) return resolve(false);
          setTimeout(tick, 60);
        };
        tick();
      })
    `).catch(() => false);
    if (!switched) console.warn(`  ! 页面未切换到 ${page}（截图可能仍是上一页）`);
    // 切换后再留一拍给数据请求与图表渲染落定
    await new Promise((r) => setTimeout(r, 700));
    // 再等两帧：capturePage 拿的是最近**合成**的帧，DOM 更新完不等于已重绘 ——
    // 少了这一步，截出来的图会整体滞后一页，而文件名却是对的，最难察觉
    await win.webContents.executeJavaScript(
      "new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(1))))"
    ).catch(() => {});
    // 光等两帧还不够：show:false 的窗口合成器可能根本不产新帧，
    // capturePage 于是端出上一页的旧帧（批量截图从第 4 页起整体错位一页就是它）。
    // invalidate 强制走一次完整重绘，之后必须再等两帧 + 一拍：截早了会拿到
    // 「标题换了、内容还没画」的混合帧。
    win.webContents.invalidate();
    await win.webContents.executeJavaScript(
      "new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(1))))"
    ).catch(() => {});
    await new Promise((r) => setTimeout(r, 350));
    const image = await win.webContents.capturePage();
    const file = join(OUT_DIR, `${page}.png`);
    await writeFile(file, image.toPNG());
    written.push(file);
    console.log(`✓ ${page} → ${file}`);
  }

  win.destroy();
  return written;
}

app.commandLine.appendSwitch("host-resolver-rules", "MAP * ~NOTFOUND , EXCLUDE localhost");
app.whenReady().then(async () => {
  try {
    await shoot();
  } catch (err) {
    console.error("截图失败：", err);
    process.exitCode = 1;
  }
  app.quit();
});
