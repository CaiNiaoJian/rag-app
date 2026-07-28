/* electron-vite 三段构建配置：main / preload / renderer（02 章 §1 进程拓扑，08 章 §2 打包）。
 *
 * 几处不走默认值的地方，都是被冻结契约逼出来的，改前先看这里的理由：
 * - package.json 的 "main" 冻结为 out/main/index.js，而 electron-vite 在
 *   package.json type=module 时会把 ESM 主进程产物命名成 .mjs。这里显式改回
 *   [name].js —— type=module 下 .js 本身就按 ESM 解析，语义不变、路径对得上。
 * - preload 必须配合 BrowserWindow 的 sandbox:true，而沙箱化 preload 不支持 ESM，
 *   所以单独把它固定为 CommonJS + .cjs 扩展名（主进程按 index.cjs 装载）。
 * - 开发期 dev server 绑 127.0.0.1：离线约束在开发期也不放松，不监听任何外部网卡。
 * - 开发期放宽 index.html 里的 CSP meta：Vite 的 HMR 与 React Refresh 会注入内联
 *   <script>，严格的 script-src 'self' 会把它挡掉。生产构建保持冻结的严格 CSP 不变。
 */

import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, externalizeDepsPlugin } from "electron-vite";
import type { Plugin } from "vite";

/** 相对本配置文件解析绝对路径（ESM 下没有 __dirname，且要兼容中文/空格路径） */
const r = (rel: string): string => fileURLToPath(new URL(rel, import.meta.url));

/** 开发期专用：把冻结的严格 CSP 换成允许 HMR 的版本，仅在 serve 时生效 */
function devCspPlugin(): Plugin {
  const devCsp = [
    "default-src 'self'",
    // HMR 客户端与 React Refresh 需要内联脚本；'unsafe-eval' 供 sourcemap 求值
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' file: data: blob:",
    "font-src 'self' data:",
    // 只放行本机回环：引擎 HTTP/SSE 与 vite dev server 的 WebSocket
    "connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:* http://localhost:* ws://localhost:*",
  ].join("; ");

  return {
    name: "docfactory:dev-csp",
    apply: "serve",
    transformIndexHtml(html: string): string {
      return html.replace(
        /<meta\s[^>]*http-equiv="Content-Security-Policy"[^>]*>/i,
        `<meta http-equiv="Content-Security-Policy" content="${devCsp}" />`,
      );
    },
  };
}

export default defineConfig({
  main: {
    // 主进程可以用 node_modules 里的真实依赖（electron-log），不打进 bundle
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: r("./out/main"),
      emptyOutDir: true,
      rollupOptions: {
        input: { index: r("./src/main/index.ts") },
        output: {
          format: "es",
          entryFileNames: "[name].js",
          chunkFileNames: "[name]-[hash].js",
        },
      },
    },
  },

  preload: {
    build: {
      outDir: r("./out/preload"),
      emptyOutDir: true,
      rollupOptions: {
        input: { index: r("./src/preload/index.ts") },
        output: {
          // 沙箱 preload 只能是 CommonJS；扩展名必须显式 .cjs（外层 type=module）
          format: "cjs",
          entryFileNames: "[name].cjs",
          chunkFileNames: "[name]-[hash].cjs",
        },
      },
    },
  },

  renderer: {
    root: r("./src/renderer"),
    plugins: [react(), devCspPlugin()],
    build: {
      outDir: r("./out/renderer"),
      emptyOutDir: true,
      // 桌面端不需要按体积拆包，单文件反而让 file:// 加载更简单
      chunkSizeWarningLimit: 2048,
      rollupOptions: {
        input: { index: r("./src/renderer/index.html") },
      },
    },
    server: {
      host: "127.0.0.1",
      strictPort: false,
    },
    // 离线：禁止任何 CDN 兜底，依赖一律走本地 node_modules
    optimizeDeps: { include: ["react", "react-dom"] },
  },
});
