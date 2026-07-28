"""许可证红线扫描（03 章 §6，CI 门禁）。

闭源商用分发的硬约束：**引入 GPL/AGPL 即阻断构建**。两道检查互补：

1. **禁用组件清单**：按包名精确拦截已知的 copyleft/工程排雷对象（PyMuPDF、MinerU、
   doclayout_yolo、ultralytics、camelot、paddlepaddle 等）。名单命中即失败，无论
   它自报的 License 字段写了什么——上游改字段不该让红线失效。
2. **许可证白名单**：只放行 MIT / BSD / Apache-2.0 / MPL-2.0 / PSF / ISC / Unlicense 等
   与闭源分发相容的许可；出现 GPL/AGPL/LGPL(静态链接场景) 或无法判定的许可即失败，
   由人工登记到 ALLOWLIST_OVERRIDES 后才放行（登记要写理由，见文件尾）。

用法：
    python scripts/license_scan.py                # 扫描当前环境，失败时退出码 1
    python scripts/license_scan.py --notices out.txt   # 顺带产出 THIRD-PARTY-NOTICES

实现上不依赖 pip-licenses 的输出格式（它跨版本会变），直接读 importlib.metadata，
这样在 CI 与本机行为一致，也省一个构建期依赖。
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from typing import Any

# ---------------------------------------------------------------- 红线

# 禁用组件（03 章 §6 表格）：包名小写，命中即阻断
BANNED_PACKAGES: dict[str, str] = {
    "pymupdf": "AGPL-3.0（商业授权未购买前禁用）",
    "fitz": "PyMuPDF 的 import 名，同 AGPL-3.0",
    "pymupdf4llm": "依赖 PyMuPDF，同 AGPL-3.0",
    "mineru": "AGPL-3.0 / 自定义许可含强制署名与自动终止条款",
    "magic-pdf": "MinerU 旧包名，同上",
    "doclayout-yolo": "AGPL-3.0",
    "doclayout_yolo": "AGPL-3.0",
    "ultralytics": "AGPL-3.0",
    "camelot-py": "ghostscript 后端为 AGPL",
    "ghostscript": "AGPL",
    "paddlepaddle": "工程原因禁用：Windows CPU 轮子 500MB+ 且 PyInstaller 兼容性差",
    "paddleocr": "同上（其 OCR 模型经 RapidOCR ONNX 化后使用）",
}

# 许可证白名单（与闭源商用分发相容）。匹配为「许可证字符串里出现该关键词」，
# 因此这里写的是**片段**而非完整名称。
ALLOWED_LICENSE_PATTERNS: tuple[str, ...] = (
    "mit", "bsd", "apache", "mpl", "mozilla public",
    "psf", "python software foundation", "isc", "unlicense",
    "public domain", "zlib", "cc0", "0bsd", "apache software license",
)

# 明确阻断的许可证关键词（优先于白名单判定：含 GPL 的字符串一律拦下再人工判定）
DENIED_LICENSE_PATTERNS: tuple[str, ...] = ("gpl", "affero", "copyleft", "sspl", "commons clause")

# LGPL 特例：动态链接场景下可用，但需登记理由。默认仍拦截。
# 人工放行清单：包名 → 放行理由（评审记录，PR 里要求写清楚）
ALLOWLIST_OVERRIDES: dict[str, str] = {
    # 例：Python 标准发行版本身带 GPL-with-linking-exception 的组件在此登记
}

# 构建期依赖排除集：只在出包链路上运行、**自身不进任何发布产物**的工具。
# 红线口径是「分发物中的第三方组件」（03 章 §6），构建工具不在口径内——
# 前提是它确实不进包，登记时必须写清依据：
# - PyInstaller 采用 GPL-2.0 **带官方打包例外**（Bootloader Exception：允许用它
#   打包并分发任意许可的程序）；且进入发布包的 bootloader 部分是 Apache-2.0，
#   PyInstaller 本体不随包分发。
# 注意：BANNED_PACKAGES 优先于本清单——被点名禁用的组件不能借「构建期」豁免。
BUILD_ONLY_PACKAGES: dict[str, str] = {
    "pyinstaller": "GPL-2.0 带 Bootloader Exception；仅构建期使用，本体不进发布包",
    "pyinstaller-hooks-contrib": "同 PyInstaller，hook 集合仅在打包时执行",
}

# 本产品自有包：Proprietary 是预期值，不参与第三方许可判定，也不进 NOTICES
SELF_PACKAGES: frozenset[str] = frozenset({"docfactory-engine"})


def _license_text(dist: metadata.Distribution) -> str:
    """许可证字符串：优先 License-Expression（PEP 639）→ Classifier → License 字段。

    单看 License 字段不可靠——现代包大多留空，真信息在 Trove classifier 里。
    """
    meta: Any = dist.metadata
    parts: list[str] = []

    expr = meta.get("License-Expression")
    if expr:
        parts.append(str(expr))

    for classifier in meta.get_all("Classifier") or []:
        if str(classifier).startswith("License ::"):
            parts.append(str(classifier).split("::")[-1].strip())

    legacy = meta.get("License")
    if legacy and len(str(legacy)) < 200:  # 有些包把整段许可全文塞进这个字段
        parts.append(str(legacy))

    return " | ".join(dict.fromkeys(p for p in parts if p)) or "UNKNOWN"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def scan() -> tuple[list[dict[str, str]], list[str]]:
    """返回 (全部依赖清单, 违规说明列表)。"""
    rows: list[dict[str, str]] = []
    violations: list[str] = []

    for dist in metadata.distributions():
        raw_name = dist.metadata.get("Name") or ""
        if not raw_name:
            continue
        name = _normalize(raw_name)
        if name in SELF_PACKAGES:
            continue
        lic = _license_text(dist)

        if name in BANNED_PACKAGES:
            rows.append({"name": raw_name, "version": dist.version or "", "license": lic})
            violations.append(f"禁用组件 {raw_name}=={dist.version}：{BANNED_PACKAGES[name]}")
            continue
        # 构建期工具不进发布包 → 不参与红线判定，也不进 NOTICES（NOTICES 只声明随包分发的组件）
        if name in BUILD_ONLY_PACKAGES:
            continue

        rows.append({"name": raw_name, "version": dist.version or "", "license": lic})
        if name in ALLOWLIST_OVERRIDES:
            continue

        low = lic.lower()
        if any(p in low for p in DENIED_LICENSE_PATTERNS):
            violations.append(f"许可证红线 {raw_name}=={dist.version}：{lic}")
        elif not any(p in low for p in ALLOWED_LICENSE_PATTERNS):
            violations.append(
                f"许可证无法判定 {raw_name}=={dist.version}：{lic}"
                "（确认相容后登记到 ALLOWLIST_OVERRIDES 并写明理由）"
            )

    rows.sort(key=lambda r: r["name"].lower())
    return rows, violations


def write_notices(rows: list[dict[str, str]], path: str) -> None:
    """产出随发布包附带的第三方许可声明（03 章 §6 要求）。"""
    lines = [
        "DocFactory 第三方组件许可声明",
        "=" * 60,
        "",
        "本产品包含以下第三方开源组件，各自许可条款归其原作者所有。",
        "LibreOffice（MPL-2.0）以独立进程方式调用，其源码获取方式见产品文档。",
        "",
    ]
    for r in rows:
        lines.append(f"- {r['name']} {r['version']} —— {r['license']}")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文报告会乱码；CI 日志与本机都强制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="许可证红线扫描（CI 门禁）")
    parser.add_argument("--notices", help="同时写出 THIRD-PARTY-NOTICES 文件到该路径")
    parser.add_argument("--list", action="store_true", help="打印完整依赖清单")
    args = parser.parse_args(argv)

    rows, violations = scan()

    if args.list:
        for r in rows:
            print(f"{r['name']:<32} {r['version']:<12} {r['license']}")

    if args.notices:
        write_notices(rows, args.notices)
        print(f"已写出第三方许可声明：{args.notices}")

    if violations:
        print(f"\n许可证扫描不通过（{len(violations)} 项）：", file=sys.stderr)
        for v in violations:
            print(f"  ✗ {v}", file=sys.stderr)
        return 1

    print(f"许可证扫描通过：{len(rows)} 个依赖全部相容于闭源商用分发")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
