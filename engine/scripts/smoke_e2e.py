"""进程级冒烟验收（M1「骨架跑通」的最终验收工具）。

pytest 里的端到端测试是**进程内**跑 runner，绕过了三样真实世界的东西：
READY 握手、Bearer 鉴权、SSE 流。这个脚本把它们补上 —— 真拉起 engine 子进程，
完全走 HTTP，最后优雅退出。CI 与本机排障都可以直接跑：

    cd engine && uv run python scripts/smoke_e2e.py

退出码 0 表示 M1 主链路（导入 → 解析 → 切片 → 导出）在真实进程边界下走通。

**打包产物模式**（``--exe``）跑的是同一套流程，但拉起的是 PyInstaller 出的 exe：

    cd engine && uv run python scripts/smoke_e2e.py --exe dist/engine/engine.exe

这一档不是锦上添花。源码模式下解释器能看见整个 src/ 树，打包产物只有 spec 显式
收进去的东西——两者的差异（漏收的包内数据文件、静态扫描看不见的延迟 import）
恰好是「本机全绿、装到用户机上起不来」那一类问题的全部来源。
实例：``migrations/*.sql`` 曾漏出 spec 的 datas，源码模式与 162 个单测全绿，
打包产物却在 ``_bootstrap`` 第一步 ``db.migrate()`` 就 FileNotFoundError 退 2。
所以出包之后必须再跑一遍这个脚本，把 exe 当成待验收的东西。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "engine"
FIXTURES_DIR = ROOT / "corpus" / "fixtures"

READY_TIMEOUT_S = 20.0     # 与主进程侧的 15s 启动超时同量级，留一点 CI 冷启动余量
TASK_TIMEOUT_S = 120.0

_step = 0


def step(message: str) -> None:
    global _step
    _step += 1
    print(f"[{_step:02d}] {message}", flush=True)


def fail(message: str) -> None:
    print(f"\n✗ 冒烟失败：{message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="smoke_e2e", description="DocFactory 进程级冒烟验收"
    )
    parser.add_argument(
        "--exe",
        default=None,
        help="改测 PyInstaller 打包产物（如 dist/engine/engine.exe）；缺省跑源码",
    )
    return parser.parse_args(argv)


def _engine_command(exe: str | None, token: str, data_dir: Path) -> tuple[list[str], str]:
    """返回 (命令行, 工作目录)。

    打包产物的 cwd 取 exe 所在目录：onedir 布局下 ``_internal`` 与 exe 同级，
    而 ``office_convert._bundled_candidates`` 也按 ``sys.executable`` 上溯找
    ``resources/libreoffice``——工作目录跟着 exe 走才与真实安装形态一致。
    """
    common = ["--port", "0", "--token", token, "--data-dir", str(data_dir)]
    if exe:
        path = Path(exe).resolve()
        if not path.is_file():
            fail(f"打包产物不存在：{path}（先跑 uv run pyinstaller docfactory.spec --noconfirm）")
        return [str(path), *common], str(path.parent)
    return [sys.executable, "-m", "docfactory.main", *common], str(ENGINE_DIR)


def wait_ready(proc: subprocess.Popen[str]) -> int:
    """读 stdout 等 READY 握手行（02 章 §1.1）。stdout 只允许这一行，其余都是异常信号。"""
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            fail(f"引擎在握手前退出，退出码 {proc.returncode}\n{proc.stderr.read() if proc.stderr else ''}")
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.05)
            continue
        line = line.strip()
        if line.startswith("READY "):
            payload = json.loads(line[len("READY "):])
            return int(payload["port"])
        print(f"     （引擎 stdout 非握手行，应当为空）：{line!r}")
    fail(f"{READY_TIMEOUT_S}s 内未收到 READY 握手")
    return 0


def poll_task(client: httpx.Client, task_id: str, label: str) -> dict[str, Any]:
    deadline = time.monotonic() + TASK_TIMEOUT_S
    while time.monotonic() < deadline:
        detail = client.get(f"/tasks/{task_id}").raise_for_status().json()
        if detail["status"] in ("done", "failed", "canceled", "interrupted"):
            if detail["status"] != "done":
                fail(f"{label} 任务未成功：{detail['status']} / {detail.get('error_code')}")
            return detail
        time.sleep(0.2)
    fail(f"{label} 任务在 {TASK_TIMEOUT_S}s 内未结束")
    return {}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if str(FIXTURES_DIR) not in sys.path:
        sys.path.insert(0, str(FIXTURES_DIR))
    from make_fixtures import generate

    workdir = Path(tempfile.mkdtemp(prefix="df-smoke-"))
    data_dir = workdir / "data-root"
    samples = workdir / "samples"
    samples.mkdir(parents=True, exist_ok=True)

    step(f"生成样本到 {samples}")
    generate(samples)
    sample = samples / "headings.docx"

    token = secrets.token_hex(16)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cmd, cwd = _engine_command(args.exe, token, data_dir)
    mode = f"打包产物 {args.exe}" if args.exe else "源码"
    step(f"拉起引擎子进程（{mode}，--port 0 随机端口）")
    proc = subprocess.Popen(
        cmd,
        cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    try:
        port = wait_ready(proc)
        step(f"READY 握手完成，端口 {port}")
        base = f"http://127.0.0.1:{port}"

        with httpx.Client(base_url=base, timeout=30.0, trust_env=False) as anon:
            # 鉴权闸：无凭据一律 401（/health 除外）
            if anon.get("/documents").status_code != 401:
                fail("无凭据请求 /documents 未被拒绝，鉴权闸失效")
            health = anon.get("/health").raise_for_status().json()
            step(f"鉴权闸生效；/health 免鉴权：引擎 {health['engine_version']} / API {health['api_version']}")

        client = httpx.Client(base_url=base, timeout=60.0, trust_env=False,
                              headers={"Authorization": f"Bearer {token}"})
        with client:
            step("导入文档（POST /documents/import）")
            imported = client.post(
                "/documents/import", json={"paths": [str(sample)]}
            ).raise_for_status().json()
            if not imported.get("imported"):
                fail(f"导入未产出文档：{imported}")
            entry = imported["imported"][0]
            doc_id, task_id = entry["doc_id"], entry["task_id"]
            step(f"已建档 {doc_id[:8]}…，解析任务 {task_id[:8]}…")

            # SSE：连上进度流，把事件读到终态（真实前端就是这么用的）
            step("订阅 SSE 进度流（GET /tasks/{id}/events）")
            seen: list[str] = []
            with client.stream("GET", f"/tasks/{task_id}/events",
                               headers={"Accept": "text/event-stream"}) as res:
                if res.status_code != 200:
                    fail(f"SSE 订阅失败：HTTP {res.status_code}")
                for line in res.iter_lines():
                    if line.startswith("event:"):
                        name = line.split(":", 1)[1].strip()
                        seen.append(name)
                        if name in ("done", "failed"):
                            break
            if "done" not in seen:
                fail(f"SSE 未收到 done 事件，实际事件：{seen}")
            step(f"SSE 收到 {len(seen)} 个事件：{', '.join(dict.fromkeys(seen))}")

            detail = poll_task(client, task_id, "解析")
            stages = [s["stage"] for s in detail.get("timeline", [])]
            step(f"解析完成，阶段时间线：{' → '.join(stages) or '（无）'}")

            doc = client.get(f"/documents/{doc_id}").raise_for_status().json()
            step(f"文档状态 {doc['status']}，级别 {doc['parse_level']}，"
                 f"覆盖率 {doc['text_coverage']}，页数 {doc['page_cnt']}")
            if doc["status"] not in ("ok", "warning"):
                fail(f"文档状态异常：{doc['status']}")

            ir_info = client.get(f"/documents/{doc_id}/ir").raise_for_status().json()
            chunks = client.get(f"/documents/{doc_id}/chunks").raise_for_status().json()
            step(f"IR {ir_info['node_count']} 个节点（{ir_info['ir_version']}），切片 {chunks['total']} 块")
            if ir_info["node_count"] < 1 or chunks["total"] < 1:
                fail("IR 或切片为空")

            step("导出六格式（POST /tasks type=export）")
            out_dir = workdir / "exports"
            export_task = client.post("/tasks", json={
                "type": "export",
                "payload": {"doc_ids": [doc_id],
                            "formats": ["md", "json", "csv", "alpaca", "sharegpt", "pdf"],
                            "out_dir": str(out_dir)},
            }).raise_for_status().json()["task_id"]
            poll_task(client, export_task, "导出")
            produced = sorted(p.name for p in out_dir.rglob("*") if p.is_file())
            step(f"导出产物 {len(produced)} 个：{', '.join(produced)}")
            if not any(p.endswith(".md") for p in produced):
                fail("未产出 Markdown")

            stats = client.get("/stats/dashboard").raise_for_status().json()
            logs = client.get("/logs").raise_for_status().json()
            step(f"仪表盘卡片 {len(stats.get('cards') or {})} 项；日志 {logs['total']} 条")

            step("优雅退出（POST /shutdown）")
            client.post("/shutdown").raise_for_status()

        proc.wait(timeout=20)
        if proc.returncode != 0:
            fail(f"引擎退出码非零：{proc.returncode}")
        step("引擎已优雅退出（退出码 0）")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    print(f"\n✓ M1 主链路冒烟通过（{mode}）："
          "READY 握手 → 鉴权 → 导入 → 解析 → SSE → 切片 → 导出 → 优雅退出")
    print(f"  数据目录：{data_dir}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
