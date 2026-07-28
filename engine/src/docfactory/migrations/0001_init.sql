-- DocFactory SQLite schema v1（02 章 §4，冻结契约）
-- meta 表存 schema_version；六业务表 + 常用索引。

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE documents (
  id TEXT PRIMARY KEY,              -- uuid
  name TEXT NOT NULL,
  src_path TEXT NOT NULL,
  fmt TEXT NOT NULL,                -- doc|docx|pdf|ppt|pptx|xls|xlsx
  size INTEGER,
  hash TEXT,                        -- SHA-256，导入去重提示
  status TEXT NOT NULL,             -- imported|parsing|ok|warning|failed
  page_cnt INTEGER,
  parse_level TEXT,                 -- 整体主级别 L0|L1|L2（混合时取占比最高级）
  text_coverage REAL,
  table_confidence REAL,
  ocr_confidence REAL,              -- 完整性指标(0~1)
  degraded_pages INTEGER DEFAULT 0,
  ir_version TEXT,
  created_at TEXT NOT NULL,
  parsed_at TEXT
);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  doc_id TEXT REFERENCES documents(id),
  type TEXT NOT NULL,               -- parse|export|rechunk|module_install|qa_generate|dataset_build
  status TEXT NOT NULL,             -- queued|running|done|failed|canceled|interrupted
  progress REAL DEFAULT 0,
  stage TEXT,                       -- convert|parse|ocr|chunk|export
  error_code TEXT,                  -- E01~E07
  payload_json TEXT,
  started_at TEXT,
  ended_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE chunks (
  id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL REFERENCES documents(id),
  seq INTEGER NOT NULL,
  parent_id TEXT,                   -- child 行的 parent_id 指向章节块
  kind TEXT NOT NULL,               -- child|parent
  type TEXT NOT NULL,               -- text|table|slide|sheet_region
  text TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  char_count INTEGER NOT NULL,      -- 双写（内核 token 计，UI 展示字符）
  heading_path TEXT,                -- "第2章>2.3节"
  pages TEXT,                       -- JSON 数组
  node_ids TEXT,                    -- JSON 数组
  meta_json TEXT,
  hash TEXT
);

CREATE TABLE task_events (          -- 结构化日志/报错（UI 日志查看器与失败 TOP 数据源）
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT REFERENCES tasks(id),
  doc_id TEXT,
  level TEXT NOT NULL,              -- info|warning|error
  code TEXT,                        -- E01~E07 或 DGR-L1/DGR-L2
  stage TEXT,
  page INTEGER,
  message TEXT,
  detail_json TEXT,
  ts TEXT NOT NULL
);

CREATE TABLE metrics_daily (        -- 仪表盘趋势预聚合（任务完成时增量更新）
  day TEXT PRIMARY KEY,
  imported INTEGER DEFAULT 0,
  parsed_ok INTEGER DEFAULT 0,
  parsed_warn INTEGER DEFAULT 0,
  parsed_fail INTEGER DEFAULT 0,
  chunk_cnt INTEGER DEFAULT 0,
  ocr_pages INTEGER DEFAULT 0,
  total_ms INTEGER DEFAULT 0
);

CREATE TABLE modules (
  id TEXT,
  name TEXT,
  type TEXT,                        -- parser|ocr|converter|llm-runtime|llm-model
  version TEXT,
  enabled INTEGER DEFAULT 1,
  prev_version TEXT,                -- 回滚指针
  manifest_json TEXT,
  installed_at TEXT,
  PRIMARY KEY (id)
);

-- 常用索引（02 章 §4 取数策略）
CREATE INDEX idx_documents_status ON documents(status);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_chunks_doc_id ON chunks(doc_id);
CREATE INDEX idx_task_events_code_level ON task_events(code, level);
CREATE INDEX idx_task_events_task ON task_events(task_id);
