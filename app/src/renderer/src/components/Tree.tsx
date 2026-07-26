/* IR 结构树的通用渲染：可折叠、单选联动、低置信黄点标记。 */

import { useState } from "react";

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

function TreeNode({ item, depth, selectedId, onSelect }: {
  item: TreeItem;
  depth: number;
  selectedId: string | null;
  onSelect: (item: TreeItem) => void;
}) {
  /* 顶部两层默认展开，深层默认收起，避免大文档树过长 */
  const [open, setOpen] = useState(depth < 2);
  const hasKids = item.children.length > 0;
  return (
    <div>
      <div
        className={`tree-row ${selectedId === item.id ? "tree-row-active" : ""}`}
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => onSelect(item)}
        title={item.label}
      >
        <span
          className={`tree-caret ${hasKids ? "" : "tree-caret-none"} ${open ? "tree-caret-open" : ""}`}
          onClick={(e) => {
            e.stopPropagation();
            if (hasKids) setOpen(!open);
          }}
        />
        <span className={`tree-icon ${item.iconClass}`}>{item.icon}</span>
        <span className="tree-label">{item.label}</span>
        {item.lowConfidence && <span className="tree-dot-low" title="低置信区域（<0.6），建议人工核对" />}
        {item.meta && <span className="tree-meta">{item.meta}</span>}
      </div>
      {hasKids && open && (
        <div>
          {item.children.map((c) => (
            <TreeNode key={c.id} item={c} depth={depth + 1} selectedId={selectedId} onSelect={onSelect} />
          ))}
        </div>
      )}
    </div>
  );
}

export function Tree({ items, selectedId, onSelect }: {
  items: TreeItem[];
  selectedId: string | null;
  onSelect: (item: TreeItem) => void;
}) {
  return (
    <div className="tree">
      {items.map((it) => (
        <TreeNode key={it.id} item={it} depth={0} selectedId={selectedId} onSelect={onSelect} />
      ))}
    </div>
  );
}
