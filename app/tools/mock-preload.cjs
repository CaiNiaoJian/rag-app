/* 截图工具的假 preload：在渲染进程任何脚本执行**之前**装好 window.df 与 fetch。
 *
 * 为什么必须是 preload 而不是 executeJavaScript：后者最早也只能在 did-start-loading
 * 之后跑，而 React 首帧已经发出了请求 —— 结果就是页面停在「正在等待引擎就绪」，
 * 截出来的是加载态而不是设计。preload 天然早于一切页面脚本。
 *
 * 假数据由 ui-shot.mjs 经 process.env.DF_MOCK_DATA 传入（JSON），这里只负责装配。
 */

const DATA = JSON.parse(process.env.DF_MOCK_DATA || "{}");

const paged = (items) => ({ items: items || [], total: (items || []).length });

const ROUTES = [
  [/\/health$/, () => ({ status: "ok", engine_version: "0.1.0", api_version: "1.0",
                         ir_version: "1.0", schema_version: 1 })],
  [/\/settings$/, () => DATA.SETTINGS],
  [/\/stats\/dashboard/, () => DATA.DASHBOARD],
  [/\/modules$/, () => DATA.MODULES],
  [/\/logs/, () => paged(DATA.LOGS)],
  [/\/tasks\/[^/]+\/events/, () => ({})],
  [/\/tasks\/[^/]+$/, () => ({
    ...(DATA.TASKS || [])[2],
    payload: {}, result: null,
    timeline: [
      { stage: "convert", started_at: "2026-07-26T11:52:00+08:00",
        ended_at: "2026-07-26T11:52:04+08:00", level: "error", code: "E03",
        message: "旧格式转换失败：LibreOffice 退出码 137", events: 2 },
    ],
    events: (DATA.LOGS || []).slice(0, 3),
  })],
  [/\/tasks/, () => paged(DATA.TASKS)],
  [/\/documents\/[^/]+\/chunks/, () => paged(DATA.CHUNKS)],
  [/\/documents\/[^/]+\/ir/, () => ({ path: "C:\\ws\\d1\\parsed\\doc.ir.json",
                                      ir_version: "1.0", node_count: 214, size: 184302 })],
  [/\/documents\/[^/]+\/preview\//, () => ({ path: "" })],
  [/\/documents\/[^/]+$/, () => (DATA.DOCS || [])[0]],
  [/\/documents/, () => paged(DATA.DOCS)],
];

const MARKDOWN = [
  "# 第1章 总则",
  "",
  "本合同由甲乙双方于平等自愿基础上签订，具备完全法律效力。双方应本着诚实信用原则履行各自义务。",
  "",
  "## 1.1 定义",
  "",
  "本合同中「交付物」指乙方按约定向甲方提供的全部成果，包括但不限于：",
  "",
  "- 源代码及构建脚本",
  "- 部署文档与运维手册",
  "- 验收测试报告",
  "",
  "## 1.2 采购清单",
  "",
  "| 项目 | 规格 | 金额 |",
  "|---|---|---|",
  "| 服务器 | 2U 机架式 | 120000 |",
  "| 交换机 | 48 口千兆 | 18000 |",
  "| 合计 |  | 138000 |",
  "",
  "# 第2章 交付条款",
  "",
  "交付期限为合同签订后 90 天。逾期按日千分之三支付违约金。",
  "",
].join("\n");

function install(target) {
  const json = (body) =>
    new target.Response(JSON.stringify(body ?? {}), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  target.fetch = async (input) => {
    const url = typeof input === "string" ? input : input && input.url;
    for (const [re, make] of ROUTES) {
      if (re.test(String(url))) return json(make());
    }
    return json({});
  };

  const noop = () => {};
  target.df = {
    engine: {
      getInfo: async () => ({ port: 1, token: "mock-token", status: "ready" }),
      onStatusChange: () => noop,
      restart: async () => {},
    },
    dialog: {
      pickFiles: async () => [],
      pickDirectory: async () => null,
      pickSavePath: async () => null,
    },
    shell: { openPath: async () => {}, showItemInFolder: async () => {} },
    files: {
      pathForFile: () => "",
      expandPaths: async () => [],
      readText: async () => MARKDOWN,
      fileUrl: (p) => "file:///" + String(p).replace(/\\/g, "/"),
    },
    pdf: { printHtmlToPdf: async () => {} },
    diagnostics: { exportZip: async () => null },
    appInfo: {
      versions: async () => ({ app: "0.1.0", electron: "42.7.1", chrome: "148", node: "24" }),
    },
    appControl: {
      uninstall: async () => ({ ok: false, reason: "截图环境不可卸载" }),
    },
  };
}

install(globalThis);
