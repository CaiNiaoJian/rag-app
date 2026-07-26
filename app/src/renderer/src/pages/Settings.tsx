/* 设置（07 章 §1 第 6 页）：通用 / 高级 / 模组管理 / 模型接口 / 关于 五个 Tab。
 *
 * 设计取舍：
 * - **默认值即最佳实践**（05 章 §5）：通用 Tab 只放三项真正常用的；
 *   降级策略、超时、切片默认值这类专家参数收进「高级」，避免小白被前置配置卡住。
 * - **改动集中提交**：本页所有编辑先改本地副本，点「保存」才 PUT /settings。
 *   引擎侧是深合并的部分更新（routes_core.put_settings → SettingsHolder.patch），
 *   本页仍提交完整对象：通用与高级两个 Tab 共用同一份草稿，
 *   整份提交才不会因为用户跨 Tab 改动而漏掉另一个 Tab 的字段。
 * - **模型接口 V1 置灰**：不给未实现的能力留可点入口，但把 V2 计划写清楚，
 *   免得用户以为是坏了（06 章 §4）。
 */

import { useCallback, useEffect, useState } from "react";
import { useApp } from "../appctx";
import { Badge } from "../components/Badge";
import { ConfirmModal } from "../components/Modal";
import type { EngineSettings } from "../types";
import { MODULE_TYPE_LABEL } from "../types";
import { fmtTime } from "../util";

/* 与导出中心一致的展示换算：中英混排约 1 token ≈ 1.7 字符，仅用于 UI */
const CHARS_PER_TOKEN = 1.7;

type TabId = "general" | "advanced" | "modules" | "models" | "about";

const TABS: { id: TabId; label: string }[] = [
  { id: "general", label: "通用" },
  { id: "advanced", label: "高级" },
  { id: "modules", label: "模组管理" },
  { id: "models", label: "模型接口" },
  { id: "about", label: "关于" },
];

interface ModuleInfo {
  id: string;
  name?: string | null;
  type?: string | null;
  version?: string | null;
  prev_version?: string | null;
  enabled?: boolean;
  installed_at?: string | null;
  dir_ok?: boolean;
  rollbackable?: boolean;
}

interface HealthInfoEx {
  engine_version?: string;
  api_version?: string;
  ir_version?: string;
  schema_version?: string | number;
  data_dir?: string;
  root?: string;
}

function moduleList(resp: unknown): ModuleInfo[] {
  if (Array.isArray(resp)) return resp as ModuleInfo[];
  if (resp && typeof resp === "object") {
    const r = resp as Record<string, unknown>;
    for (const key of ["modules", "items"]) {
      if (Array.isArray(r[key])) return r[key] as ModuleInfo[];
    }
  }
  return [];
}

export function Settings() {
  const { client, nav, toast } = useApp();

  const [tab, setTab] = useState<TabId>("general");
  const [draft, setDraft] = useState<EngineSettings | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const [modules, setModules] = useState<ModuleInfo[]>([]);
  const [toRollback, setToRollback] = useState<ModuleInfo | null>(null);

  const [health, setHealth] = useState<HealthInfoEx | null>(null);
  const [versions, setVersions] = useState<{ app: string; electron: string; chrome: string; node: string } | null>(null);

  const loadSettings = useCallback(async () => {
    try {
      setDraft(await client.getJson<EngineSettings>("/settings"));
      setDirty(false);
    } catch {
      toast("读取设置失败", "err");
    }
  }, [client, toast]);

  const loadModules = useCallback(async () => {
    try {
      setModules(moduleList(await client.getJson<unknown>("/modules")));
    } catch {
      setModules([]);
    }
  }, [client]);

  useEffect(() => {
    void loadSettings();
    void loadModules();
    void client.getJson<HealthInfoEx>("/health").then(setHealth).catch(() => undefined);
    void window.df.appInfo.versions().then(setVersions).catch(() => undefined);
  }, [client, loadSettings, loadModules]);

  useEffect(() => {
    if (nav.page !== "settings") return;
    const t = nav.params["tab"];
    if (t && TABS.some((x) => x.id === t)) {
      setTab(t as TabId);
      if (t === "modules") void loadModules();
    }
  }, [nav, loadModules]);

  const patch = (p: Partial<EngineSettings>) => {
    setDraft((prev) => (prev ? { ...prev, ...p } : prev));
    setDirty(true);
  };
  const patchChunk = (p: Partial<EngineSettings["chunk"]>) => {
    setDraft((prev) => (prev ? { ...prev, chunk: { ...prev.chunk, ...p } } : prev));
    setDirty(true);
  };

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      await client.putJson<EngineSettings>("/settings", draft);
      setDirty(false);
      toast("设置已保存，对新任务立即生效", "ok");
    } catch {
      toast("保存失败，请检查填写的数值", "err");
    } finally {
      setSaving(false);
    }
  };

  const installKmod = async () => {
    try {
      const paths = await window.df.dialog.pickFiles();
      const kmods = paths.filter((p) => /\.kmod$/i.test(p));
      if (!kmods.length) {
        toast("请选择 .kmod 离线更新包", "err");
        return;
      }
      for (const p of kmods) {
        await client.postJson<{ task_id: string }>("/modules/install", { kmod_path: p });
      }
      toast(`已开始安装 ${kmods.length} 个模组，完成后需重启引擎生效`, "ok");
    } catch {
      toast("安装模组失败", "err");
    }
  };

  const doRollback = async (m: ModuleInfo) => {
    try {
      await client.postJson("/modules/rollback", { module_id: m.id });
      toast(`已回滚「${m.name ?? m.id}」，重启引擎后生效`, "ok");
      void loadModules();
    } catch {
      toast("回滚失败，可能上一版本目录已损坏", "err");
    }
  };

  const dataDir = health?.data_dir ?? health?.root ?? null;

  return (
    <div className="page page-settings">
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`tab ${tab === t.id ? "tab-active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="settings-body">
        {tab === "general" && (
          <section className="settings-pane">
            {!draft ? (
              <div className="col-empty">正在读取设置…</div>
            ) : (
              <>
                <div className="field-row">
                  <div className="field-main">
                    <div className="field-title">默认输出目录</div>
                    <div className="field-desc">导出产物的存放位置；留空则放在各文档自己的 exports 目录。</div>
                  </div>
                  <div className="path-row">
                    <span className="path-text ellipsis" title={draft.output_dir ?? ""}>
                      {draft.output_dir || "（默认）"}
                    </span>
                    <button
                      className="btn btn-sm"
                      onClick={() => {
                        void window.df.dialog.pickDirectory().then((p) => {
                          if (p) patch({ output_dir: p });
                        });
                      }}
                    >
                      选择…
                    </button>
                    {draft.output_dir && (
                      <button className="btn btn-sm btn-ghost" onClick={() => patch({ output_dir: null })}>清除</button>
                    )}
                  </div>
                </div>

                <div className="field-row">
                  <div className="field-main">
                    <div className="field-title">同时处理的文件数</div>
                    <div className="field-desc">调高会更快，但内存与 CPU 占用也更高；默认值已按本机核心数算好。</div>
                  </div>
                  <input
                    className="input input-sm input-num"
                    type="number"
                    min={1}
                    max={8}
                    value={draft.parallel_tasks}
                    onChange={(e) => patch({ parallel_tasks: Math.min(8, Math.max(1, Number(e.target.value) || 1)) })}
                  />
                </div>

                <div className="field-row">
                  <div className="field-main">
                    <div className="field-title">文字识别（OCR）</div>
                    <div className="field-desc">扫描件与图片型 PDF 需要它；关闭后这类文件只会提取已有文字层。</div>
                  </div>
                  <select
                    className="select"
                    value={draft.ocr_mode}
                    onChange={(e) => patch({ ocr_mode: e.target.value as EngineSettings["ocr_mode"] })}
                  >
                    <option value="on">开启（推荐）</option>
                    <option value="off">关闭（仅已有文字层）</option>
                    <option value="high">高精度（需安装高精度 OCR 模组）</option>
                  </select>
                </div>

                <SaveBar dirty={dirty} saving={saving} onSave={() => void save()} onReset={() => void loadSettings()} />
              </>
            )}
          </section>
        )}

        {tab === "advanced" && (
          <section className="settings-pane">
            {!draft ? (
              <div className="col-empty">正在读取设置…</div>
            ) : (
              <>
                <div className="field-row">
                  <div className="field-main">
                    <div className="field-title">解析降级策略</div>
                    <div className="field-desc">
                      自动降级：某页深度解析失败时退到更保守的方式继续，尽量不丢内容（推荐）。
                      仅精解析：任何一页失败即判定整份失败，适合对版面还原要求极高的场景。
                    </div>
                  </div>
                  <select
                    className="select"
                    value={draft.degrade_policy}
                    onChange={(e) => patch({ degrade_policy: e.target.value as EngineSettings["degrade_policy"] })}
                  >
                    <option value="auto">自动降级（推荐）</option>
                    <option value="strict">仅精解析</option>
                  </select>
                </div>

                <div className="field-row">
                  <div className="field-main">
                    <div className="field-title">单页处理超时</div>
                    <div className="field-desc">超过该时长的页会自动降级处理，避免个别复杂页拖住整批任务。</div>
                  </div>
                  <div className="path-row">
                    <input
                      className="input input-sm input-num"
                      type="number"
                      min={5}
                      max={300}
                      value={draft.page_timeout_s}
                      onChange={(e) => patch({ page_timeout_s: Math.min(300, Math.max(5, Number(e.target.value) || 30)) })}
                    />
                    <span className="field-unit">秒</span>
                  </div>
                </div>

                <div className="field-group-title">切片默认值（导出中心可临时覆盖）</div>

                <div className="field-row">
                  <div className="field-main">
                    <div className="field-title">切片长度</div>
                    <div className="field-desc">
                      约 {Math.round((draft.chunk.target_tokens * CHARS_PER_TOKEN) / 50) * 50} 字符；
                      更长保留更多上下文，更短检索更精准。
                    </div>
                  </div>
                  <input
                    type="range"
                    min={256}
                    max={2048}
                    step={64}
                    value={draft.chunk.target_tokens}
                    onChange={(e) => patchChunk({ target_tokens: Number(e.target.value) })}
                  />
                </div>

                <div className="field-row">
                  <div className="field-main">
                    <div className="field-title">相邻切片重叠</div>
                    <div className="field-desc">{Math.round(draft.chunk.overlap * 100)}%，让跨切片的句子不被切断。</div>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={40}
                    step={2}
                    value={Math.round(draft.chunk.overlap * 100)}
                    onChange={(e) => patchChunk({ overlap: Number(e.target.value) / 100 })}
                  />
                </div>

                <div className="field-row">
                  <div className="field-main">
                    <div className="field-title">切片结构选项</div>
                    <div className="field-desc">按标题切分、表格不拆开、剔除页眉页脚、脚注归到文末。</div>
                  </div>
                  <div className="switch-col">
                    <label className="switch">
                      <input type="checkbox" checked={draft.chunk.split_by_heading} onChange={(e) => patchChunk({ split_by_heading: e.target.checked })} />
                      按标题切分
                    </label>
                    <label className="switch">
                      <input type="checkbox" checked={draft.chunk.table_atomic} onChange={(e) => patchChunk({ table_atomic: e.target.checked })} />
                      表格不拆开
                    </label>
                    <label className="switch">
                      <input type="checkbox" checked={draft.chunk.drop_header_footer} onChange={(e) => patchChunk({ drop_header_footer: e.target.checked })} />
                      剔除页眉页脚
                    </label>
                    <label className="switch">
                      <input type="checkbox" checked={draft.chunk.footnote_to_end} onChange={(e) => patchChunk({ footnote_to_end: e.target.checked })} />
                      脚注归到文末
                    </label>
                  </div>
                </div>

                <details className="tech-details">
                  <summary>技术详情（内核参数）</summary>
                  <dl className="kv">
                    <dt>target_tokens</dt>
                    <dd className="mono">{draft.chunk.target_tokens}</dd>
                    <dt>max_tokens</dt>
                    <dd className="mono">{draft.chunk.max_tokens}</dd>
                    <dt>overlap</dt>
                    <dd className="mono">{draft.chunk.overlap}</dd>
                  </dl>
                  <p className="panel-note">引擎内部按 token 计数（04 章 §3.2），界面上的字符数为等价换算，仅用于直观理解。</p>
                </details>

                <SaveBar dirty={dirty} saving={saving} onSave={() => void save()} onReset={() => void loadSettings()} />
              </>
            )}
          </section>
        )}

        {tab === "modules" && (
          <section className="settings-pane">
            <div className="pane-head">
              <div>
                <div className="field-title">已安装模组</div>
                <div className="field-desc">模组用于扩展解析、OCR 与格式转换能力；安装或回滚后需重启引擎生效。</div>
              </div>
              <div className="pane-head-actions">
                <button className="btn btn-sm" onClick={() => void loadModules()}>刷新</button>
                <button className="btn btn-sm btn-primary" onClick={() => void installKmod()}>导入离线更新包</button>
              </div>
            </div>

            {modules.length === 0 ? (
              <div className="col-empty">还没有安装任何模组。把 .kmod 文件拖进窗口即可安装。</div>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>类型</th>
                      <th>版本</th>
                      <th>状态</th>
                      <th>安装时间</th>
                      <th className="col-ops">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modules.map((m) => (
                      <tr key={m.id}>
                        <td className="ellipsis" title={m.id}>{m.name ?? m.id}</td>
                        <td>{m.type ? MODULE_TYPE_LABEL[m.type] ?? m.type : "—"}</td>
                        <td className="mono">{m.version ?? "—"}</td>
                        <td>
                          {m.dir_ok === false ? (
                            <Badge kind="err" text="文件缺失" />
                          ) : m.enabled === false ? (
                            <Badge kind="neutral" text="已停用" />
                          ) : (
                            <Badge kind="ok" text="已启用" />
                          )}
                        </td>
                        <td>{fmtTime(m.installed_at)}</td>
                        <td className="col-ops">
                          <button
                            className="btn btn-sm"
                            disabled={!m.rollbackable}
                            title={m.rollbackable ? `回滚到 ${m.prev_version}` : "没有可回滚的上一版本"}
                            onClick={() => setToRollback(m)}
                          >
                            回滚
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}

        {tab === "models" && (
          <section className="settings-pane">
            <div className="pane-lead">
              本地模型接口用于「由模型自动生成问答数据集」等能力。当前版本只提供接口占位，
              不包含任何模型，也不会联网下载——这是刻意的：离线可控优先。
            </div>
            <div className="field-row field-row-off">
              <div className="field-main">
                <div className="field-title">本地模型运行时</div>
                <div className="field-desc">未安装。后续可通过 .kmod 离线包导入运行时与 GGUF 模型。</div>
              </div>
              <button className="btn" disabled>选择模型</button>
            </div>
            <div className="field-row field-row-off">
              <div className="field-main">
                <div className="field-title">问答生成</div>
                <div className="field-desc">导出中心的「模型生成 Q&amp;A」将在安装模型后解锁；当前提供留空模板与规则生成两档。</div>
              </div>
              <button className="btn" disabled>启用</button>
            </div>
            <div className="pane-note">
              接口形态兼容 OpenAI 规范（/v1/models、/v1/chat/completions），仅监听本机回环地址。
            </div>
          </section>
        )}

        {tab === "about" && (
          <section className="settings-pane">
            <div className="about-title">DocFactory</div>
            <div className="about-sub">Windows 完全离线的 RAG 文档数据工厂</div>
            <dl className="kv kv-wide">
              <dt>应用版本</dt>
              <dd className="mono">{versions?.app ?? "—"}</dd>
              <dt>引擎版本</dt>
              <dd className="mono">{health?.engine_version ?? "—"}</dd>
              <dt>接口版本</dt>
              <dd className="mono">{health?.api_version ?? "—"}</dd>
              <dt>中间格式版本</dt>
              <dd className="mono">{health?.ir_version ?? "—"}</dd>
              <dt>数据库版本</dt>
              <dd className="mono">{health?.schema_version ?? "—"}</dd>
              <dt>运行环境</dt>
              <dd className="mono">Electron {versions?.electron ?? "—"} · Chromium {versions?.chrome ?? "—"} · Node {versions?.node ?? "—"}</dd>
              <dt>数据目录</dt>
              <dd className="path-row">
                <span className="ellipsis mono" title={dataDir ?? ""}>
                  {dataDir ?? "%LOCALAPPDATA%\\DocFactory"}
                </span>
                {dataDir && (
                  <button className="btn btn-sm" onClick={() => void window.df.shell.openPath(dataDir)}>打开</button>
                )}
              </dd>
            </dl>
            <div className="about-block">
              <div className="field-title">离线声明</div>
              <p className="field-desc">
                本应用不含任何联网代码路径：界面与引擎之间只经本机回环地址通信，
                不做遥测、不检查更新、不下载模型。所有解析与导出都在本机完成。
              </p>
            </div>
            <div className="about-block">
              <div className="field-title">第三方组件</div>
              <p className="field-desc">
                Electron（MIT）· Python（PSF）· FastAPI / uvicorn（MIT / BSD）· Docling 及其模型（MIT / Apache-2.0 / CDLA-P-2.0）·
                pdfplumber / pypdfium2（MIT / BSD-3 · Apache-2.0）· RapidOCR + onnxruntime（Apache-2.0 / MIT）·
                python-docx / python-pptx / openpyxl（MIT）· LibreOffice（MPL-2.0，独立进程调用）。
                完整许可证文本随安装包一同分发。
              </p>
            </div>
          </section>
        )}
      </div>

      <ConfirmModal
        open={toRollback !== null}
        title="回滚模组"
        confirmText="回滚"
        message={`将「${toRollback?.name ?? toRollback?.id ?? ""}」回滚到上一版本 ${toRollback?.prev_version ?? ""}，重启引擎后生效。`}
        onConfirm={() => {
          if (toRollback) void doRollback(toRollback);
        }}
        onClose={() => setToRollback(null)}
      />
    </div>
  );
}

function SaveBar({ dirty, saving, onSave, onReset }: {
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
  onReset: () => void;
}) {
  return (
    <div className="save-bar">
      {dirty ? <span className="hint-dim">有未保存的修改</span> : <span className="hint-dim">设置已是最新</span>}
      <button className="btn btn-sm" disabled={!dirty || saving} onClick={onReset}>放弃修改</button>
      <button className="btn btn-sm btn-primary" disabled={!dirty || saving} onClick={onSave}>
        {saving ? "保存中…" : "保存"}
      </button>
    </div>
  );
}
