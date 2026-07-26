# golden corpus —— 解析质量回归门禁

08 章 §3.1 的落地：一组**人工标注了期望输出**的标准文档，CI 每次批量解析后与标注自动比对打分，
输出指标趋势报告。**每个里程碑作为合入门禁**：M1 建 v0（20 份），M2 扩至 50+。

## 目录结构

```
corpus/
  fixtures/make_fixtures.py   # 程序化生成的基础样本（无版权、可入库、CI 必跑）
  samples/                    # 真实语料（.gitignore 忽略：体积大、可能含敏感/版权内容）
  expected/{名}.expected.json # 标注：每份样本的期望输出
  run_corpus.py               # 批量解析 → 比对 → 打分 → 报告
  report.md                   # 最近一次运行的报告（.gitignore 忽略）
```

**为什么分 fixtures 与 samples**：真实语料不能入库（版权 + 体积 + 可能含敏感信息），
但 CI 又必须有可跑的最小回归集。fixtures 用 python-docx / openpyxl / reportlab
程序化生成结构确定的样本（合并单元格、多级标题、多 sheet、长表），
标注即生成脚本本身的意图，永远不会过期。真实语料由开发者放进 `samples/` 本地跑。

## 用法

```bash
cd engine
uv run python ../corpus/run_corpus.py --generate     # 先生成 fixtures
uv run python ../corpus/run_corpus.py                # 跑全量比对，失败退出码 1
uv run python ../corpus/run_corpus.py --report ../corpus/report.md
uv run python ../corpus/run_corpus.py --only 表格     # 只跑名字含「表格」的样本
```

## 标注格式（`expected/{名}.expected.json`）

```json
{
  "sample": "merged_table.docx",
  "note": "合并单元格表格 + 三级标题，验证 rowspan/colspan 与层级还原",
  "must_not_fail": true,
  "min_text_coverage": 0.95,
  "max_seconds": 30,
  "expect": {
    "node_counts": { "section": 3, "table": 1 },
    "headings": ["第1章 总则", "1.1 定义"],
    "text_contains": ["交付期限为合同签订后 90 天"],
    "tables": [
      { "rows": 4, "cols": 3, "has_merged": true, "cells_contain": ["项目", "金额"] }
    ]
  }
}
```

字段语义：

| 字段 | 含义 |
|---|---|
| `must_not_fail` | 解析必须有产出（对应「解析成功率 ≥ 99%」门禁）。损坏/密码样本设为 `false`，改用 `expect_error_code` |
| `expect_error_code` | 期望的错误码（E01~E07）。用于损坏/密码/超大文件样本——**优雅报错也是通过** |
| `min_text_coverage` | 文本覆盖率下限（数字 PDF 门禁 0.97） |
| `max_seconds` | 单份耗时上限，超时记 warning 不判失败（机器性能差异大，趋势比绝对值有意义） |
| `expect.node_counts` | 各类型节点数**下限**（解析越完整节点越多，用下限避免为噪声节点频繁改标注） |
| `expect.headings` | 必须出现的标题文本（子串匹配，验证层级还原与阅读顺序） |
| `expect.text_contains` | 必须出现在 IR 全文里的片段（验证内容不丢） |
| `expect.tables[i]` | 第 i 个表格的形状：`rows`/`cols` 为下限，`has_merged` 精确匹配，`cells_contain` 子串 |

## 加一份样本的步骤

1. 文件放进 `samples/`（真实语料）或在 `fixtures/make_fixtures.py` 里加生成函数。
2. 跑 `python run_corpus.py --only 新样本名 --dump` 看实际解析出的结构。
3. 照着实际结构写 `expected/新样本名.expected.json`，**只标注你真正在意的不变量**——
   标注过细会让每次解析器改进都要改标注，标注就没人维护了。
4. 提交时在 PR 里说明这份样本覆盖了哪个失败模式。

## M1 的 20 份覆盖面（v0 清单）

程序化 fixtures 覆盖：多级标题 docx、合并单元格表 docx、列表 docx、图文混排 pptx、
多 sheet + 公式 + 合并单元格 xlsx、超大表 xlsx（截断路径）、多段文本 PDF、空文档、
损坏文件、非支持格式。真实语料需人工补：多栏学术 PDF、扫描 PDF（倾斜/低分辨率）、
跨页表 PDF、200+ 页大 PPT、旧版 doc/ppt、密码文件、500MB 超大 PDF、中英混排。
