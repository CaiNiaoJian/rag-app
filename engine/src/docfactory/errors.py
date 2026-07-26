"""错误码注册表（07 章 §4，全局唯一事实来源）。

同时服务：UI 文案映射、失败 TOP 统计、日志检索、用户手册。
降级事件 DGR-L1/DGR-L2 记 warning 级 task_events，不占用 E 码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    user_message: str  # 人话文案
    suggestion: str    # 建议操作


# E01~E07 冻结注册表
ERRORS: dict[str, ErrorInfo] = {
    "E01": ErrorInfo("E01", "文件似乎已损坏，无法读取", "用原程序打开确认后重新导入"),
    "E02": ErrorInfo("E02", "文件受密码保护", "先在原程序中解除密码再导入"),
    "E03": ErrorInfo("E03", "暂不支持此文件格式（或旧格式转换失败）", "查看支持格式清单；旧格式可另存为新格式后导入"),
    "E04": ErrorInfo("E04", "扫描质量较低，部分文字可能识别不准", "预览中检查黄色高亮区域；可尝试高精度 OCR 模组"),
    "E05": ErrorInfo("E05", "未能提取到有效内容（或超大表已截断）", "确认文件内容；超大表建议拆分"),
    "E06": ErrorInfo("E06", "处理超时或引擎异常，已自动重试", "重试；反复出现请导出诊断包反馈"),
    "E07": ErrorInfo("E07", "磁盘空间不足，无法继续", "清理磁盘或在设置中迁移数据目录"),
}

# 降级事件码（warning 级，03 章 §5.1）
DGR_L1 = "DGR-L1"
DGR_L2 = "DGR-L2"

# 本地模型接口桩错误（06 章 §4.2）
MODEL_NOT_INSTALLED = "MODEL_NOT_INSTALLED"


def error_payload(code: str, detail: str | None = None) -> dict[str, Any]:
    """错误三级呈现结构（FR-13）：人话文案 → 建议操作 → 技术详情。"""
    info = ERRORS.get(code)
    return {
        "error_code": code,
        "user_message": info.user_message if info else "发生未知错误",
        "suggestion": info.suggestion if info else "重试；反复出现请导出诊断包反馈",
        "detail": detail,
    }


class DocFactoryError(Exception):
    """携带错误码的业务异常；worker 捕获后写 tasks.error_code 与 task_events。"""

    def __init__(self, code: str, detail: str = "", *, page: int | None = None):
        self.code = code
        self.detail = detail
        self.page = page
        info = ERRORS.get(code)
        super().__init__(f"[{code}] {info.user_message if info else detail}")
