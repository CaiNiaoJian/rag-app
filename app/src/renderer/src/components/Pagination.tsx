/* 简单分页：与引擎 DAO 的固定页大小配合（文档/任务 50，日志 100）。 */

export function Pagination({ page, total, pageSize, onChange }: {
  page: number;
  total: number;
  pageSize: number;
  onChange: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (pages <= 1) return null;
  return (
    <div className="pagination">
      <button className="btn btn-sm" disabled={page <= 1} onClick={() => onChange(page - 1)}>
        上一页
      </button>
      <span className="pagination-info">
        第 {page} / {pages} 页 · 共 {total} 条
      </span>
      <button className="btn btn-sm" disabled={page >= pages} onClick={() => onChange(page + 1)}>
        下一页
      </button>
    </div>
  );
}
