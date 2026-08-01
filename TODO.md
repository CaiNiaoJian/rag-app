# TODO —— DocFactory V1

> 依据：六维度代码审计（145 条 + 独立复核补充 20 条）与本机实测，2026-07-27。
> 里程碑定义见 `docs/08-日志打包测试与里程碑.md` §4。

## 结论

**V1 未完成。** 实际卡位：M1 收尾阶段，**M2 核心解析基本未启动**。

| 里程碑 | done | partial | missing | 完成度 | 还差什么（一句话） |
|---|---|---|---|---|---|
| M1 骨架跑通 | 31 | 22 | 6 | ~52% | 收尾清单已清零（日志契约 2026-08-01 对齐）；余为 22 项 partial 打磨（键盘可达/CSP 生效等） |
| **M2 核心解析** | 6 | 12 | 18 | **~16%** | **Docling / RapidOCR / LibreOffice 三大能力一个都没接** |
| M3 切片与导出 | 13 | 8 | 4 | ~52% | Qwen tokenizer 未接、PDF 导出三项、切片规则无单测 |
| M4 模组与模型接口 | 13 | 10 | 3 | ~50% | 模组装完不生效（无 loader）、安装闭环断在半路 |
| M5 打磨发布 | 0 | 11 | 8 | **0%** | 整体未启动 |

M3/M4 数字高于 M2，是因为**实现顺序偏离了里程碑计划**——切片、六格式导出、.kmod 验签这些后期活儿先写完了，而 M2 那三项才是产品核心卖点的载体。

---

## 一、本轮已完成（均已验证）

1. **修复打包产物启动即挂** —— `docfactory.spec` 漏收包内 `migrations/*.sql`，`_bootstrap` 第一步 `db.migrate()` 就 `FileNotFoundError` 退 2。162 个单测全绿也照样漏掉：门禁从没跑过打包产物。
2. **NSIS 安装包首次产出** —— `DocFactory-0.1.0-win-x64-setup.exe` 111MB，引擎正确随包进 `resources/engine/`。这是 M1 首要交付物「打包链路最大集成风险前置」，此前从未构建成功过。
3. **打包产物纳入门禁** —— `smoke_e2e.py` 加 `--exe` 模式（同一套链路改跑真 exe，已验证全通）；`ci.yml` 新增 `package` job：PyInstaller → 打包产物冒烟 → NSIS 出包 → 体积门禁 + 引擎存在性校验。
4. **corpus 10 → 20 份**，M1 门禁达成，20/20 通过。新增均为此前 README 标注「需人工补真实语料」的场景：双栏 PDF、跨页表、210 页 PPT、密码文件、扫描件、中英混排、嵌套列表、页眉页脚、内嵌图片、多区域 Excel。
5. **性能与体积基线首次实测** —— `engine/scripts/perf_baseline.py`，报告落 `corpus/perf-baseline.md`。
6. **离线闸逃生门封堵** —— `DOCFACTORY_DISABLE_OFFLINE_GUARD=1` 原先在打包产物里同样生效，而主进程 spawn 时 `{...process.env}` 会把父进程环境整个传下去：目标机上设个系统环境变量就能让 FR-17 失效。已按项目既有的 frozen 判定模式封掉 + 测试锁定。
7. **`shell.openPath` / `showItemInFolder` 打穿离线约束** —— 实测 `openPath("https://…")` 会把 URL 交给系统浏览器（返回成功），UNC 路径会触发 SMB + NTLM 凭据外泄；两条都**完全不经 Chromium**，net-guard 四道闸一道都拦不到。已改为允许清单 + URL/UNC 拒绝 + Win32 尾部归一化（`x.exe.` / `x.exe ` 绕过）+ 过路径闸。
8. **主进程 socket 级离线闸** —— 引擎侧早有 `offline_guard.py`，主进程的 `node:http` 与 undici `fetch` 却不经 Chromium，此前只靠代码评审自觉。已补 `installProcessSocketGuard()`，与引擎侧对称。
9. **孤儿进程兜底** —— 主进程被任务管理器强杀时没机会发 `/shutdown`，engine.exe 连同 soffice.exe 变常驻孤儿。已加 `parent_watch.py`（子进程反向守望父进程，避开 Job Object 所需的原生模块）+ 9 条测试。

### 2026-07-29 增量（均已验证：190 单测 + 21 corpus 样本全绿，typecheck/build 通过）

10. **CI 许可证门禁转绿** —— `license_scan.py` 增加 `BUILD_ONLY_PACKAGES` 构建期依赖排除集（PyInstaller GPL-2.0 带官方 Bootloader Exception，本体不进发布包；**禁用清单优先级高于豁免**，被点名的组件不能借「构建期」逃逸）。4 条回归测试锁定，`package` job 不再被挡。
11. **`.xls` xlrd 兜底路径**（M2 低成本对冲落地）—— 无 LibreOffice 时 .xls 经 xlrd（BSD-3）→ openpyxl 转 xlsx 后走既有解析链：值/日期/布尔/错误码、合并区域、**加粗表头**（表头启发式依赖）全部穿透；`convert_chain` 记 `xls->xlsx(xlrd)` 溯源，能力边界（图片/图表不保留）写事件日志。soffice 存在时仍优先。新增 corpus 样本 `legacy_table.xls`——CI 无 LibreOffice，恰好常态锁定兜底路径。5 条单测。
12. **`text_coverage` 去假**（完整性指标第一项）—— docx/pptx 分母由新模块 `parsers/raw_text.py` 从包内 XML **旁路**统计（含 footnotes/endnotes/SmartArt——解析器抽不到的丢失从此看得见；页眉页脚按解析器同款规则去重；`a:fld` 页码域剔除）；xlsx 在单元格扫描时顺路累计；PDF 原有路径不变。分母统计失败记 **None**（UI「—」）而不是假 1.0。已有测试证明 docx 脚注丢字会按比例压低覆盖率。8 条单测。
13. **THIRD-PARTY-NOTICES.txt 进安装包** —— CI 许可证扫描步骤把声明写进 `engine/dist/engine/`，随 extraResources 进 `resources\engine\`；`package` job 体积门禁新增存在性核验（缺失即构建失败）。本地出包的生成命令已注释进 `electron-builder.yml`。
14. **500MB 上限执行覆盖** —— 压小上限走同一条分支：超限报 E05 且不留半成品目录；恰好等于上限（<=）放行。
15. **取消 10s 强制兜底**（此前整条路径缺失）—— `cancel()` 对 running 任务加宽限计时器：超时仍未响应 → 记 E06 warning → 强标 canceled → **弃用**卡死的 worker 线程（Python 杀不掉线程，与 `run_with_timeout` 同款取舍）→ **补位**新 worker（并发槽不泄漏）。弃用线程的迟到结果/进度一律丢弃，不改写终态；写入顺序契约：warning 先于终态可见、终态先于 SSE 事件。宽限期内在检查点自行停下则撤计时器，不误伤。
16. **队列暂停持久化** —— 引擎侧 `meta.queue_paused` 开关 + `GET /queue` / `POST /queue/pause` 端点：暂停 = 暂停派发，排队任务**原地保留**（task_id 稳定、追溯链不断），正在跑的不打断，模组安装豁免；重启引擎/刷新页面都不丢。Workbench 废弃「取消再重建」模拟与 `held[]`，暂停态以引擎为唯一事实来源。
17. **技术版本清单锁定** —— `docs/08` §6 表按 uv.lock/package-lock 实测更新（FastAPI 0.140.x 等，未接入组件明确标注 M2 锁版）；`pyproject.toml` 运行时依赖全部收紧到小版本区间（上界挡住重 lock 时静默跨版），`uv lock` 复核无版本漂移。

### 2026-08-01 增量（已验证：212 单测 + 21 corpus 全绿，ruff / typecheck / build 通过）

18. **日志两端对齐九字段 JSONL**（M1 收尾唯一剩余项，08 章 §1 契约落地）—— 引擎 `logsetup.py` 弃用 `serialize=True`（只能吐 loguru 私有嵌套结构、无 `src`），改自定义 **callable formatter** 把每条 record 拍平成 `{ts,level,src,task_id,doc_id,code,page,msg,detail}`（callable formatter 下 loguru 不再自动追加异常栈/换行，输出严格一行一 JSON）；Electron `index.ts` 弃用纯文本模板，electron-log `format` 函数输出同构九字段（`src="app"`、level `warn→warning` 归一、`code` 从文案正则抽取、整行 JSON 包成单元素数组绕开模板拼接）。两端 `src` 区分产日志进程，诊断包里 `engine-*.jsonl` 与 `app-main.log` 从此可用同一解析器合并时间线（`routes_logs.py` 的 README 文案同步更新）。新增 `test_logsetup.py`（5 条：字段集合恒为九项且全扁平 / 字段映射与 level 小写归一 / 未绑定业务字段为 null / 异常入 detail / 真跑 setup_logging 后落盘每行都是合法 JSON）锁定契约防回退。

### 实测数据（基线，非门禁）

| 指标 | 实测 | 01 章目标 | |
|---|---|---|---|
| 冷启动（spawn → /health 200，打包产物） | **5.32s** | ≤ 5s | ✗ 略超 |
| 100 页文本 PDF 全流程 | 9.60s | ≤ 480s | ✓ |
| 10 万字纯文本快速路径 | 1.48s | ≤ 60s | ✓ |
| 引擎进程内存峰值 | 108MB | ≤ 4GB | ✓ |
| 引擎 onedir | 80MB | 08 章预估 60MB | 超 33% |
| 安装后总体积 / NSIS 包 | 389MB / 111MB | ≤ 2GB | ✓ |

> 性能余量看着很大，但**当前跑的是 L1 粗解析**——L0 版面分析（RT-DETR ~3~5s/页）与 OCR 接入后会大幅改变这组数字。M2 完成后必须重跑再冻结门禁。

---

## 二、待办

### M1 收尾（剩余）

- [x] ~~**日志 JSONL 行格式与 08 章 §1 契约不符**~~（2026-08-01 完成，见增量 18：两端已对齐扁平九字段 `{ts,level,src,task_id,doc_id,code,page,msg,detail}`，诊断包可用同一解析器合并时间线）
- [x] ~~取消后 10s 未响应则 kill worker~~（2026-07-29 完成，见增量 15）
- [x] ~~队列「暂停」只活在渲染进程内存里~~（2026-07-29 完成，见增量 16）
- [x] ~~单文件 500MB 上限零执行覆盖~~（2026-07-29 完成，见增量 14）
- [x] ~~THIRD-PARTY-NOTICES.txt 进不了安装包~~（2026-07-29 完成，见增量 13）
- [x] ~~技术版本清单与实测对不上~~（2026-07-29 完成，见增量 17）
- [x] ~~CI 许可证门禁当前是红的~~（2026-07-29 完成，见增量 10）

### M2 核心解析 —— 主线，最大缺口

这一段是产品的立身之本（01 章 §1.1 核心卖点「复杂文档解析的内容完整性」）。当前状态：**七格式实为四格式，L0 精解析在代码里恒不可达**（即便装了 docling 也 `return None`）。

**三项 XL 主线**（互相独立，可并行）：

- [ ] **D1 / L0 版面分析**：接 Docling，RT-DETR layout 模型（~100MB 权重）输出块类型与阅读顺序。不做则「多栏阅读顺序正确率 ≥ 90%」不可能达成——现在按 top 单键排序，双栏页会把左右栏逐行交错。
- [ ] **D1 / 表格结构还原**：TableFormer（fast 146MB 随包）输出 cell 网格与 rowspan/colspan。PDF 表格现在丢失全部合并信息与真实表头行。
- [ ] **D2 / OCR**：RapidOCR + onnxruntime CPU EP + PP-OCRv5 mobile（~20MB 随包）。扫描件、文档内嵌图片、PPT 截图里的文字当前**全部丢失**且无提示。
- [ ] **D3 / LibreOffice 归一化**（L）：裁剪版随包 + 构建步骤（下载/裁剪/剔除 GPL 组件）。`find_soffice()` 三路查找在干净 Windows 机上全部落空，导入 `.doc/.ppt/.xls` 必抛 E03。

> **依赖链**：LibreOffice 不只解锁旧格式，还解锁 **Office 文档的页快照** → 而页快照是**文档库三栏对照**的「原文」栏（07 章 §1）。当前只有 `pdf_parser.py:386` 生成快照，docx/pptx/xlsx 三个 parser 一次都没调，所以 Office 用户的三栏对照退化成两栏——**核心卖点「用户可亲自验证完整性」对 Office 用户不成立**。
>
> ~~**低成本对冲**：`.xls` 走 xlrd 作为离线备选路径~~ **已落地**（2026-07-29，见增量 11）：.xls 已真通，.doc/.ppt 仍完全依赖 LibreOffice。

**完整性指标目前是假的**（M，卖点直接相关）：

- [x] ~~`text_coverage` 恒写 1.0~~（2026-07-29 完成，见增量 12。现在 `corpus/report.md` 的平均 1.000 是**真算**出来的——fixtures 本就无损）
- [ ] `table_confidence`：占位常量，恒 1.0（待 TableFormer 接入后由模型 cell 置信覆盖）。
- [ ] `ocr_confidence`：恒 NULL；「页均置信 < 0.6 记 E04」无实现（待 D2/OCR）。
- [ ] `prov.bbox` / `charspan`：**无任何生产者，字段是空壳**。连带三处失守：OCR 坐标回填无落点、三栏对照无法做区域级高亮、`text_coverage` 无法按 charspan 口径核算。

**其他 M2 项**：

- [ ] 跨页表合并（M）——IR 契约已预留（`ir.py:22`），解析层未实现。corpus 的 `long_table.pdf` 现在解析成 4 张独立表。
- [ ] PDF 图片抽取落 assets（M）——PDF 文档导出的 Markdown 不含任何图片。
- [ ] PDF 页眉/页脚/脚注识别（M）——`drop_header_footer` 默认 True 但对 PDF 完全无效，页码噪声原样进数据集。
- [ ] **docx 脚注静默丢字**（M）——合同/标准/论文的关键限定条件常写在脚注里，当前既不进 IR 也不进数据集，而用户看到的 `text_coverage` 还是 1.0。「卖点是内容完整性、却在无提示地丢内容」。
- [ ] 单页 30s 超时对整页处理无效（M）——病态 PDF 卡在渲染时降级永不触发，任务无限期挂起并永久占用一个 worker 槽。
- [ ] OCR 三档开关（off/on/high）当前行为完全一致（M）。
- [ ] 「跳过版面分析」快速路径开关（S）——它是「10 万字 ≤ 60s」这条门禁的**前提**。
- [ ] 文档状态判定省略了「全部页 L0」条件（S）——L0 落地后必须补回，否则 L1 结果会被判 ok，「降级透明」失守。
- [ ] IR major 版本闸门与 `ir_outdated` 标记（M）——读到 `ir_version="2.0"` 会被当 1.0 静默解析。
- [ ] corpus 扩至 50+（XL）——缺的正是需要真实语料 + 人工标注的那批（扫描件/旧格式/多栏）。
- [ ] 四项量化指标当前不可测量：TEDS（L）、多栏阅读顺序正确率（L）、OCR 字符准确率（XL）、OCR P95 耗时（XL，连分位统计能力都没有）。

### M3 切片与导出（补漏）

- [ ] **Qwen tokenizer 未接入**（M）。实测 `tokenizer_backend()` 返回 `heuristic`，`import tokenizers` 直接 ModuleNotFoundError——**所有导出产物里的 `token_count` 都是估算值**（中文 1 字 = 1 token），与训练侧口径不一致，而产品里没有任何位置能看出这一点。需加依赖 + 打包 tokenizer.json + 对存量文档触发重切。
- [ ] PDF 导出三项（S 各）：内嵌中文字体（现在完全依赖目标机装有微软雅黑，与「零外部依赖」矛盾）、生成书签大纲、页眉页脚模板（引擎写的 meta 契约无人消费，PDF 里没有页码）。
- [ ] `.print.html` 中间件落进了用户导出目录（S）——不在格式矩阵里，且里面的图片是 `file://` 绝对路径，转发给别人就是一堆坏图。
- [ ] 切片五条规则 + 六个参数**零单测**（M）——任何重构都可能静默改变切片结果而 CI 不报警。
- [ ] `sheet_region` 切片 type 不是 `table`（S），导致 `dataset.py:68` 的表格类问题生成对 Excel 永远不触发。
- [ ] 数据集 schema 缺自己的版本锚点（S），被切片导出的 `schema_version` 顶替。
- [ ] corpus 缺导出维度回归（L）——PDF 分页与中文字体回退完全没有网。

### M4 模组与模型接口（补漏）

- [ ] **模组装完不生效**（L）。没有任何按 type 的加载/注册点，装上高精度 OCR 或解析组件后引擎完全不会去用它，「重启引擎生效」这句提示**没有对应实现**。ocr/parser/converter 三类在 V1 就该有消费方。
- [ ] **安装闭环断在半路**（M）。点「验签并安装」后只有一句「已开始安装」：成功与否要自己去工作台队列找，新版本号要自己回设置页刷新。07 章用户流 C 后半段一步都没闭合。
- [ ] 安装确认弹窗无版本对比（M）——只有文件名和大小，07 章要求的「v2.1→v2.3 + 更新说明」拿不到。根因是引擎没有只读 manifest 的预检端点，需补 `POST /modules/inspect`。
- [ ] 模组安装失败的人话文案是解析域的（M）——模组被篡改时 UI 显示「暂不支持此文件格式」并建议「另存为新格式后导入」，既误导又不可操作。错误码注册表没有模组域的码。
- [ ] `manifest.version` 不校验 semver（S）——版本先后靠字符串相等比较，装入更旧的包会被当成升级，**回滚方向反转**。
- [ ] `qa_generate` / `dataset_build` 只能走 HTTP API（M），桌面端无入口；两个 runner 零测试覆盖。
- [ ] 模组端点层零回归保护（S）。
- [ ] 打包产物里 `.kmod` 全流程从未验证过（M）——新加的打包冒烟门禁没覆盖 `/modules` 与 `/v1`。

### M5 打磨发布（未启动）

- [ ] 桌面端**连测试运行器都没有**（M）。主进程零单测——路径闸、白名单判定、READY 解析、日志轮转裁剪这些纯函数全可测且值得测。
- [ ] 断网验证（防火墙全阻断 + Wireshark 抓包，**发布必测**）、崩溃恢复、长稳 200 份、拖拽五场景、路径边界——全部无用例（M 各）。
- [ ] `longPathAware` 清单注入（M）——`electron-builder.yml` 只有 TODO 注释，`app/build/` 目录不存在。**顺序不能反**：清单注入必须早于签名。
- [ ] AVX2 检测（M）——01 章 §2.4 明确要求，安装器/主进程/引擎三层都没有。
- [ ] 代码签名 + 杀软送样（M）；正式签名密钥对未产出，`PUBLIC_KEYS` 仍是开发占位公钥（**按现状出包，所有正式签名的 .kmod 都装不上**）。
- [ ] 设置页磁盘治理三项（L）——缺 `POST /maintenance/clean-cache` 与 workspace 迁移。当前 E07 磁盘不足的建议文案「在设置中迁移数据目录」**指向一个不存在的功能**。
- [ ] 模组卸载/停用与旧版本目录回收（M）——`modules.enabled` 列与 UI「已停用」状态已预设该语义但无实现，安装 N 次就在 `modules\{id}\` 下堆 N 份模型文件。
- [ ] 用户手册（M）。
- [ ] 卸载器询问「是否保留解析数据」（S）。

---

## 三、进行中

七个 UI / 外壳缺口的并行修复已完成落盘，`npm run typecheck && npm run build` 已统一验证通过（2026-07-29）。行为级验收（键盘可达性、CSP 生效、窗口尺寸持久化等）待人工/视觉回归确认：

| 组 | 文件 | 内容 |
|---|---|---|
| dashboard | `Dashboard.tsx` `types.ts` | 补 `chunk_per_doc` 图与 `p50_ms` 中位耗时（引擎已算好，前端连类型都没声明） |
| keyboard | `Tree.tsx` `Workbench.tsx` | IR 结构树整行是 `div+onClick`，键盘完全够不到 |
| search-filters | `App.tsx` `Logs.tsx` | 全局搜索 placeholder 撒谎；错误码过滤只在前端做，跨页会漏 |
| library | `Library.tsx` | 预览载入期左右栏先闪空状态；时间筛选只覆盖当前页 |
| csp | `index.html` `net-guard.ts` | 两份 CSP 已漂移，生产生效的那份缺 `base-uri`/`form-action`（无 default-src 回退） |
| window-tools | `index.ts` `ui-shot.mjs` `package.json` | 窗口尺寸不持久化；视觉回归工具注释写错用法且无 script 入口 |
| shell-hygiene | `diagnostics.ts` `ipc.ts` | 打印窗口 `document.fonts.ready` 无超时可被挂死；诊断包在数据根堆积；IPC 无来源校验 |

---

## 四、与文档不符的既成事实

改文档还是改实现，需要决策：

1. **切片边界高亮被改成独立页签**。07 章 §1 把它和低置信高亮并列写在右栏 Markdown 上；实现拆成第二个页签（`Library.tsx:10-11` 的注释说明了理由——不破坏正文阅读）。是有意识的取舍，但默认页签下用户看不到任何切片边界痕迹。
2. **`chunk_id` 形态偏离 05 章样例**（样例是 `c-0012`/`p-0003`，实现是全局唯一 ID）。偏离有充分理由且已注释，但短号在 JSON/CSV/数据集 metadata 里完全取不到，下游只能靠字符串切分还原。
3. ~~**队列「暂停」不是暂停**~~ 已修复（2026-07-29，增量 16）：真·引擎侧暂停派发，任务 ID 不再变更。
4. **`tasks` 表没有 `result_json` 列**，`routes_tasks.py:107` 读它恒得 null（`row.get()` 不会崩）。代码注释表明是有意的前瞻设计，但 UI 拿不到任务摘要。
5. **CI 把 NSIS 出包标为 M5**，而 08 章 §4 明确写着 M1 交付「可安装 NSIS 包（打包链路最大集成风险前置）」+「CI 出包」。本轮已按 08 章正文办（新增 `package` job）。
6. **`docs/08` §2 体积表 8 项预估一项未验**，08 章自己写着「M1 实测修订」。本轮已产出实测数据（见上），**表格待更新**。

---

## 五、建议的推进顺序

1. ~~**先解 CI 红**~~ ✅ 2026-07-29 完成（增量 10）。
2. ~~**`.xls` 走 xlrd 兜底**~~ ✅ 2026-07-29 完成（增量 11）。
3. **LibreOffice 随包**（L）——解锁旧格式 + Office 页快照（三栏对照卖点）两件事，是 M2 里性价比最高的一项。
4. **OCR**（XL）→ **版面分析 + 表格还原**（XL）——按此顺序：OCR 的缺失是「内容直接丢失」，版面分析的缺失是「顺序错乱」，前者更致命。
5. ~~**完整性指标去假**~~ ✅ text_coverage 已完成（增量 12）；table/ocr 置信与 bbox/charspan 随 M2 模型接入落地。
6. **Qwen tokenizer**（M）——影响所有已导出数据集的可信度，越晚接存量重切成本越高。**建议列为下一项**。
