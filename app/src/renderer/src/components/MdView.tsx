/* 轻量 Markdown 渲染器（零依赖，覆盖引擎导出的 MD 子集）：
 * 标题 / 段落 / 列表 / 管道表 / HTML 表透传（合并单元格）/ 图片 / 引用块 / 代码块 / 分隔线。
 * 意图：只服务本工厂自产的 doc.md，不追求完整 CommonMark。
 * HTML 表透传前做保守清洗（去脚本与事件属性）；图片相对路径经 resolveUrl 转 file:// 地址。
 */

import { useMemo, type ReactNode } from "react";

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "para"; text: string }
  | { kind: "quote"; lines: string[] }
  | { kind: "code"; lang: string; lines: string[] }
  | { kind: "table"; header: string[]; rows: string[][] }
  | { kind: "html"; html: string }
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

    /* HTML 表透传（导出契约：含合并单元格的表用 HTML table） */
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
      blocks.push({ kind: "html", html: body.join("\n") });
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

/* 保守清洗透传 HTML：仅保留表格结构，去脚本/事件/危险协议 */
function sanitizeHtml(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
    .replace(/\son\w+\s*=\s*[^\s>]+/gi, "")
    .replace(/(href|src)\s*=\s*(["']?)\s*javascript:[^"'>\s]*\2/gi, "");
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
          case "html":
            /* 引擎自产的合并单元格表：清洗后透传 */
            return <div key={idx} className="md-html-table" dangerouslySetInnerHTML={{ __html: sanitizeHtml(b.html) }} />;
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
