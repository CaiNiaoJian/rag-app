/* IR 结构树的通用渲染：可折叠、单选联动、低置信黄点标记。
 *
 * 键盘可达性（DESIGN.md 的硬要求）：左栏是三栏对照的导航入口，键盘够不到它，
 * 整个预览页对键盘用户就是废的。这里照 WAI-ARIA tree 模式做「漫游 tabindex」——
 * 整棵树在 Tab 序列里只占一个站位，树内改用方向键移动。两种偷懒写法都不行：
 * 每行都给 tabIndex=0，大文档几百个节点会把 Tab 序列淹掉（要按上百次才能离开左栏）；
 * 只留一行 tabIndex=0 又不做方向键，焦点就永远卡在那一行，等于没修。
 *
 * 代价是展开状态与焦点落点必须从各节点提升到容器：只有容器算得出「此刻可见的行依次是哪些」，
 * 而方向键的上/下一行、Tab 落点是否还渲染着，全依赖这份序列。DOM 也随之拍平
 * （缩进本来就是 paddingLeft 画的，不靠嵌套），层级改由 aria-level/posinset/setsize 表达；
 * 保留嵌套反而有害——父行的焦点环会把整棵子树圈进去。
 */

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

export interface TreeItem {
  id: string;
  label: string;
  /* 单字类型徽标（章/段/表/图…），配合 CSS 着色 */
  icon: string;
  iconClass: string;
  /* confidence < 0.6 时为 true：树中黄点标记（07 章低置信提示） */
  lowConfidence?: boolean;
  meta?: string;
  children: TreeItem[];
}

/* 拍平后的一行。pos/size 是给 aria-posinset/aria-setsize 用的：DOM 没了嵌套，
 * 「同级第几个、共几个」只能由这里补回去，否则读屏念不出树的形状。 */
interface FlatRow {
  item: TreeItem;
  depth: number;
  pos: number;
  size: number;
  hasKids: boolean;
  open: boolean;
}

export function Tree({ items, selectedId, onSelect }: {
  items: TreeItem[];
  selectedId: string | null;
  onSelect: (item: TreeItem) => void;
}) {
  /* 只记「用户手动改过」的节点；没记录的沿用「顶部两层默认展开」——
   * 深层默认收起，避免大文档树一上来就几十屏长。 */
  const [openMap, setOpenMap] = useState<Record<string, boolean>>({});
  /* 漫游 tabindex 的落点：跟着最后获得焦点的行走，Tab 兜一圈回到树上时能回到原处 */
  const [focusId, setFocusId] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  /* 换文档必须清空：节点 id 只在单篇文档内唯一，留着上一篇的展开记录会让新树
   * 莫名其妙地自己展开几层。items 存在 Library 的 preview state 里，一次载入才换一次身份。 */
  useEffect(() => {
    setOpenMap({});
    setFocusId(null);
  }, [items]);

  const rows = useMemo(() => {
    const out: FlatRow[] = [];
    const walk = (list: TreeItem[], depth: number) => {
      list.forEach((item, i) => {
        const hasKids = item.children.length > 0;
        const open = openMap[item.id] ?? depth < 2;
        out.push({ item, depth, pos: i + 1, size: list.length, hasKids, open });
        if (hasKids && open) walk(item.children, depth + 1);
      });
    };
    walk(items, 0);
    return out;
  }, [items, openMap]);

  /* Tab 落点必须是「此刻真的渲染着」的行：焦点行、选中行都可能被祖先收起而消失，
   * 两个都不在就退回第一行。绝不能出现整棵树一个 tabIndex=0 都没有的状态——键盘会再也进不来。 */
  const tabbable =
    rows.find((r) => r.item.id === focusId) ?? rows.find((r) => r.item.id === selectedId) ?? rows[0];

  const setOpen = (id: string, open: boolean) => setOpenMap((prev) => ({ ...prev, [id]: open }));

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    /* 行的 DOM 顺序与 rows 严格一一对应，所以「焦点在第几行」查下标就够了；
     * 查不到说明按键来自行内的子元素（折叠箭头之类），不归树管。 */
    const els = rootRef.current ? Array.from(rootRef.current.querySelectorAll<HTMLElement>(".tree-row")) : [];
    const cur = els.findIndex((el) => el === e.target);
    if (cur < 0) return;
    const row = rows[cur];
    switch (e.key) {
      case "ArrowDown":
        els[Math.min(cur + 1, els.length - 1)]?.focus();
        break;
      case "ArrowUp":
        els[Math.max(cur - 1, 0)]?.focus();
        break;
      case "Home":
        els[0]?.focus();
        break;
      case "End":
        els[els.length - 1]?.focus();
        break;
      case "ArrowRight":
        /* 收起时先展开，已展开则进入第一个子节点（子节点必定紧邻其后） */
        if (row.hasKids && !row.open) setOpen(row.item.id, true);
        else if (row.hasKids) els[cur + 1]?.focus();
        break;
      case "ArrowLeft":
        /* 展开时先收起，否则退回父行——父行就是往上数第一个层级更浅的行 */
        if (row.hasKids && row.open) setOpen(row.item.id, false);
        else {
          for (let i = cur - 1; i >= 0; i--) {
            if (rows[i].depth < row.depth) {
              els[i]?.focus();
              break;
            }
          }
        }
        break;
      case "Enter":
      case " ":
        onSelect(row.item);
        break;
      default:
        return;
    }
    /* 走到这里说明按键已被树消费：空格会滚动外层 col-scroll，方向键同理，
     * 不挡掉的话焦点在树里移动的同时视口自己在乱跑。 */
    e.preventDefault();
  };

  return (
    <div className="tree" role="tree" aria-label="文档结构" ref={rootRef} onKeyDown={onKeyDown}>
      {rows.map((r) => (
        <div
          key={r.item.id}
          className={`tree-row ${selectedId === r.item.id ? "tree-row-active" : ""}`}
          style={{ paddingLeft: 8 + r.depth * 14 }}
          role="treeitem"
          tabIndex={r === tabbable ? 0 : -1}
          aria-level={r.depth + 1}
          aria-posinset={r.pos}
          aria-setsize={r.size}
          aria-selected={selectedId === r.item.id}
          aria-expanded={r.hasKids ? r.open : undefined}
          onFocus={() => setFocusId(r.item.id)}
          onClick={() => onSelect(r.item)}
          title={r.item.label}
        >
          <span
            className={`tree-caret ${r.hasKids ? "" : "tree-caret-none"} ${r.open ? "tree-caret-open" : ""}`}
            onClick={(e) => {
              e.stopPropagation();
              if (r.hasKids) setOpen(r.item.id, !r.open);
            }}
          />
          <span className={`tree-icon ${r.item.iconClass}`}>{r.item.icon}</span>
          <span className="tree-label">{r.item.label}</span>
          {r.item.lowConfidence && <span className="tree-dot-low" title="低置信区域（<0.6），建议人工核对" />}
          {r.item.meta && <span className="tree-meta">{r.item.meta}</span>}
        </div>
      ))}
    </div>
  );
}
