/* 轻量 Markdown 渲染器（零依赖，覆盖引擎导出的 MD 子集）：
 * 标题 / 段落 / 列表 / 管道表 / HTML 表（合并单元格）/ 图片 / 引用块 / 代码块 / 分隔线。
 * 意图：只服务本工厂自产的 doc.md，不追求完整 CommonMark。
 * HTML 表**不透传**：先解析成结构化数据再交给 React 渲染（见 parseHtmlTable）；
 * 图片相对路径经 resolveUrl 转 file:// 地址。
 */

import { useMemo, type ReactNode } from "react";

interface TableCell {
  text: string;
  header: boolean;
  rowspan: number;
  colspan: number;
}

type TableRow = TableCell[];

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "para"; text: string }
  | { kind: "quote"; lines: string[] }
  | { kind: "code"; lang: string; lines: string[] }
  | { kind: "table"; header: string[]; rows: string[][] }
  | { kind: "htmltable"; head: TableRow[]; body: TableRow[] }
  | { kind: "image"; alt: string; src: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "hr" };

const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;

function splitCells(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  /* 支持转义竖线 \| */
  return s.split(/(?<!\\)\|/).map((c) => c.replace(/\\\|/g, "|").trim());
}

function parseBlocks(src: string): Block[] {
  const lines = src.split(/\r?\n/);
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    /* 代码块 ``` */
    const fence = trimmed.match(/^```(\w*)\s*$/);
    if (fence) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // 跳过收尾 ```
      blocks.push({ kind: "code", lang: fence[1] ?? "", lines: body });
      continue;
    }

    /* HTML 表（导出契约：含合并单元格的表用 HTML table） */
    if (/^<table[\s>]/i.test(trimmed)) {
      const body: string[] = [];
      while (i < lines.length) {
        body.push(lines[i]);
        if (/<\/table>/i.test(lines[i])) {
          i += 1;
          break;
        }
        i += 1;
      }
      const raw = body.join("\n");
      /* 抽不出表结构时按纯文本收着：宁可把原文显示出来，也不静默丢内容 */
      blocks.push(parseHtmlTable(raw) ?? { kind: "para", text: raw });
      continue;
    }

    /* 标题 */
    const h = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      blocks.push({ kind: "heading", level: h[1].length, text: h[2] });
      i += 1;
      continue;
    }

    /* 分隔线 */
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      blocks.push({ kind: "hr" });
      i += 1;
      continue;
    }

    /* 引用块 */
    if (trimmed.startsWith(">")) {
      const q: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        q.push(lines[i].trim().replace(/^>\s?/, ""));
        i += 1;
      }
      blocks.push({ kind: "quote", lines: q });
      continue;
    }

    /* 管道表：当前行含 |，下一行是分隔行 */
    if (trimmed.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const header = splitCells(trimmed);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        rows.push(splitCells(lines[i]));
        i += 1;
      }
      blocks.push({ kind: "table", header, rows });
      continue;
    }

    /* 独占一行的图片 */
    const img = trimmed.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
    if (img) {
      blocks.push({ kind: "image", alt: img[1], src: img[2] });
      i += 1;
      continue;
    }

    /* 列表（无序/有序，单层足够） */
    if (/^([-*+]|\d+[.)])\s+/.test(trimmed)) {
      const ordered = /^\d/.test(trimmed);
      const items: string[] = [];
      while (i < lines.length && /^([-*+]|\d+[.)])\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^([-*+]|\d+[.)])\s+/, ""));
        i += 1;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }

    /* 段落：连续非空行合并 */
    const para: string[] = [trimmed];
    i += 1;
    while (i < lines.length) {
      const t = lines[i].trim();
      if (
        !t ||
        t.startsWith("#") ||
        t.startsWith(">") ||
        t.startsWith("```") ||
        /^<table[\s>]/i.test(t) ||
        /^([-*+]|\d+[.)])\s+/.test(t) ||
        (t.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]))
      ) {
        break;
      }
      para.push(t);
      i += 1;
    }
    blocks.push({ kind: "para", text: para.join("\n") });
  }
  return blocks;
}

/* 单元格 span 上限：文档里写个 colspan="999999" 只会把布局撑爆，浏览器本身也只认到 1000 */
const MAX_SPAN = 1000;

/**
 * HTML 表 → 结构化数据：只取标签、rowspan/colspan 与纯文本，其余节点与属性一律丢弃。
 *
 * 这里刻意**不**走「清洗后交给 dangerouslySetInnerHTML」那条路。doc.md 的正文来自用户
 * 导入的任意文档，等于不可信输入；而黑名单式正则清洗挡不住 `<img src=x/onerror=…>`
 * 这类写法——HTML 允许用 `/` 分隔属性，正则却要求事件属性前是空白符。白名单式重建
 * 从根上就没有让文档内容变成标记的机会，也就不需要跟绕过写法赛跑。
 *
 * DOMParser 产出的是惰性文档：不执行脚本、不加载外部资源，解析本身是安全的。
 */
function parseHtmlTable(html: string): Extract<Block, { kind: "htmltable" }> | null {
  if (typeof DOMParser === "undefined") return null;
  let table: HTMLTableElement | null = null;
  try {
    table = new DOMParser().parseFromString(html, "text/html").querySelector("table");
  } catch {
    return null;
  }
  if (!table) return null;

  const span = (raw: string | null): number => {
    const n = Number.parseInt(raw ?? "", 10);
    return Number.isFinite(n) && n > 1 ? Math.min(n, MAX_SPAN) : 1;
  };

  /* <br> 是引擎写入单元格内换行的唯一形式（见 exporters/markdown.render_table_html），
   * 先还原成 \n，再整体取纯文本。
   * script/style 先整个摘掉：textContent 会把它们的内容也算进来，于是脚本正文
   * 会以纯文本形式出现在单元格里——不构成执行风险，但那是噪声不是内容。 */
  const cellText = (cell: Element): string => {
    const clone = cell.cloneNode(true) as Element;
    for (const el of Array.from(clone.querySelectorAll("script, style"))) el.remove();
    for (const br of Array.from(clone.querySelectorAll("br"))) br.replaceWith("\n");
    return clone.textContent ?? "";
  };

  const head: TableRow[] = [];
  const body: TableRow[] = [];
  for (const tr of Array.from(table.querySelectorAll("tr"))) {
    if (tr.closest("table") !== table) continue; // 嵌套表的行不并进来
    const cells: TableRow = [];
    for (const cell of Array.from(tr.children)) {
      if (cell.tagName !== "TD" && cell.tagName !== "TH") continue;
      cells.push({
        text: cellText(cell),
        header: cell.tagName === "TH",
        rowspan: span(cell.getAttribute("rowspan")),
        colspan: span(cell.getAttribute("colspan")),
      });
    }
    if (!cells.length) continue;
    (tr.parentElement?.tagName === "THEAD" ? head : body).push(cells);
  }
  if (!head.length && !body.length) return null;
  return { kind: "htmltable", head, body };
}

/* 单元格内换行：文本已是纯文本，换行用 <br> 元素表达 */
function cellLines(text: string): ReactNode[] {
  return text.split("\n").flatMap((line, i) => (i === 0 ? [line] : [<br key={i} />, line]));
}

function TableCellView({ cell }: { cell: TableCell }): ReactNode {
  const rowSpan = cell.rowspan > 1 ? cell.rowspan : undefined;
  const colSpan = cell.colspan > 1 ? cell.colspan : undefined;
  const content = cellLines(cell.text);
  return cell.header ? (
    <th rowSpan={rowSpan} colSpan={colSpan}>{content}</th>
  ) : (
    <td rowSpan={rowSpan} colSpan={colSpan}>{content}</td>
  );
}

function TableRows({ rows }: { rows: TableRow[] }): ReactNode {
  return rows.map((row, i) => (
    <tr key={i}>
      {row.map((cell, j) => <TableCellView key={j} cell={cell} />)}
    </tr>
  ));
}

/* 行内格式：`code`、**bold**、行内图片、链接（离线：链接降级为文字） */
function renderInline(text: string, resolveUrl?: (rel: string) => string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(!\[[^\]]*\]\([^)]+\))|(\[[^\]]*\]\([^)]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("`")) {
      out.push(<code key={key++} className="md-code-inline">{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith("**")) {
      out.push(<strong key={key++}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("![")) {
      const mm = tok.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
      if (mm) out.push(<img key={key++} className="md-img-inline" alt={mm[1]} src={resolveSrc(mm[2], resolveUrl)} />);
    } else {
      const mm = tok.match(/^\[([^\]]*)\]\(([^)]+)\)$/);
      /* 离线应用不跳外链：仅显示链接文字并给出原地址提示 */
      if (mm) out.push(<span key={key++} className="md-link" title={mm[2]}>{mm[1]}</span>);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function resolveSrc(src: string, resolveUrl?: (rel: string) => string): string {
  if (/^(data:|file:|https?:)/i.test(src)) return src;
  return resolveUrl ? resolveUrl(src) : src;
}

export function MdView({ source, resolveUrl }: {
  source: string;
  resolveUrl?: (rel: string) => string;
}) {
  const blocks = useMemo(() => parseBlocks(source), [source]);
  return (
    <div className="md-view">
      {blocks.map((b, idx) => {
        switch (b.kind) {
          case "heading": {
            const Tag = (`h${Math.min(b.level, 6)}`) as "h1";
            return <Tag key={idx} className={`md-h md-h${b.level}`}>{renderInline(b.text, resolveUrl)}</Tag>;
          }
          case "para":
            return <p key={idx}>{renderInline(b.text, resolveUrl)}</p>;
          case "quote":
            return (
              <blockquote key={idx}>
                {b.lines.map((l, j) => <p key={j}>{renderInline(l, resolveUrl)}</p>)}
              </blockquote>
            );
          case "code":
            return (
              <pre key={idx} className="md-pre">
                <code>{b.lines.join("\n")}</code>
              </pre>
            );
          case "table":
            return (
              <table key={idx} className="md-table">
                <thead>
                  <tr>{b.header.map((c, j) => <th key={j}>{renderInline(c, resolveUrl)}</th>)}</tr>
                </thead>
                <tbody>
                  {b.rows.map((r, j) => (
                    <tr key={j}>{r.map((c, k) => <td key={k}>{renderInline(c, resolveUrl)}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            );
          case "htmltable":
            /* 含合并单元格的表：结构化重建，与管道表共用样式 */
            return (
              <table key={idx} className="md-table">
                {b.head.length > 0 && <thead><TableRows rows={b.head} /></thead>}
                {b.body.length > 0 && <tbody><TableRows rows={b.body} /></tbody>}
              </table>
            );
          case "image":
            return (
              <figure key={idx} className="md-figure">
                <img alt={b.alt} src={resolveSrc(b.src, resolveUrl)} />
                {b.alt && <figcaption>{b.alt}</figcaption>}
              </figure>
            );
          case "list":
            return b.ordered ? (
              <ol key={idx}>{b.items.map((it, j) => <li key={j}>{renderInline(it, resolveUrl)}</li>)}</ol>
            ) : (
              <ul key={idx}>{b.items.map((it, j) => <li key={j}>{renderInline(it, resolveUrl)}</li>)}</ul>
            );
          case "hr":
            return <hr key={idx} />;
          default:
            return null;
        }
      })}
    </div>
  );
}
