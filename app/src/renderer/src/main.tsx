/* 渲染进程入口（index.html 直接引用本文件）。
 * 样式以副作用方式引入，交给 Vite 打进本地 bundle——绝不引用远程字体/CDN（02 章 §7 离线约束）。
 * 开启 StrictMode：本应用的副作用（SSE 订阅、定时器）都写了清理函数，
 * 开发期的双次挂载正好用来暴露漏掉的 cleanup。
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("缺少 #root 挂载点");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
