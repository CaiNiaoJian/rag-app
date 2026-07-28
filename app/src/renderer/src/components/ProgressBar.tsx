/* 进度条：value 取 0~100；失败红色、完成绿色、进行中品牌蓝。 */

export function ProgressBar({ value, state = "run", slim }: {
  value: number;
  state?: "run" | "ok" | "err";
  slim?: boolean;
}) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div className={`progress ${slim ? "progress-slim" : ""}`}>
      <div className={`progress-fill progress-${state}`} style={{ width: `${v}%` }} />
    </div>
  );
}
