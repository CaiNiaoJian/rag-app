/// <reference types="vite/client" />

/* CSS 以副作用方式引入（Vite 处理），仅为通过类型检查声明模块 */
declare module "*.css";

/* preload 暴露的受控 API（冻结契约，02 章 §7：renderer 一切能力经 window.df） */
export {};

declare global {
  interface DfEngineInfo {
    port: number;
    token: string;
    status: "starting" | "ready" | "down";
  }
  interface DfPathEntry {
    path: string;
    name: string;
    ext: string;
    size: number;
    supported: boolean;
    isKmod: boolean;
  }
  interface DfApi {
    engine: {
      getInfo(): Promise<DfEngineInfo>;
      onStatusChange(cb: (info: DfEngineInfo) => void): () => void;
      restart(): Promise<void>;
    };
    dialog: {
      pickFiles(): Promise<string[]>;
      pickDirectory(): Promise<string | null>;
      pickSavePath(defaultName: string): Promise<string | null>;
    };
    shell: {
      openPath(p: string): Promise<void>;
      showItemInFolder(p: string): Promise<void>;
    };
    files: {
      pathForFile(f: File): string;
      expandPaths(paths: string[]): Promise<DfPathEntry[]>;
      readText(p: string): Promise<string>;
      fileUrl(p: string): string;
    };
    pdf: {
      printHtmlToPdf(htmlPath: string, outPdfPath: string): Promise<void>;
    };
    diagnostics: {
      exportZip(): Promise<string | null>;
    };
    appInfo: {
      versions(): Promise<{ app: string; electron: string; chrome: string; node: string }>;
    };
  }
  interface Window {
    df: DfApi;
  }
}
