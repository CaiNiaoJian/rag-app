"""token 计数（04 章 §3.2「token 计数」小节）。

**设计取舍（M2 现状）**：规格要求本地打包 Qwen tokenizer（Apache-2.0），让 token
估计与未来 4B/8B 本地模型对齐。但 tokenizers/transformers 依赖此刻既没进
pyproject，也没做离线安装包体积与许可证审计，所以本模块走「双轨」：

- **真 tokenizer 在位**：``models/qwen-tokenizer/tokenizer.json`` 存在且 ``tokenizers``
  可 import 时，用它编码，结果与训练侧完全一致；
- **否则回退启发式**：中文按字计（1 token/字）、拉丁词按 4 字符/token、数字按 3 位
  /token、标点逐个计、换行单独计。

启发式刻意**偏保守（倾向高估）**：切片层拿它做长度预算，高估只会让块偏小一点，绝不会
产出超过 ``max_tokens`` 的块 —— 这是宁可牺牲装填率也要守住的一条。

**M3 替换路径**：把 tokenizer.json 放进 ``models/qwen-tokenizer/``、在 pyproject 加
``tokenizers`` 依赖，本模块自动切换；chunking 层与 chunks 表结构都不用动，只需对已有
文档触发一次重切（token_count 数值会变，char_count 不变）。

离线纪律：只用 ``Tokenizer.from_file()`` 读本地文件，**绝不调用 from_pretrained()**
（那条路会去 Hugging Face 拉模型，违反完全离线约束）。
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

# 本地 tokenizer 的约定位置：paths.models / TOKENIZER_SUBDIR / tokenizer.json
TOKENIZER_SUBDIR = "qwen-tokenizer"
TOKENIZER_FILE = "tokenizer.json"

BACKEND_LOCAL = "qwen-local"
BACKEND_HEURISTIC = "heuristic"

# 启发式的分类扫描：一次正则遍历替代逐字符 Python 循环（几 MB 的文档也只要几十毫秒）。
# 分支顺序有意义 —— cjk 先吃掉表意文字/假名/谚文，剩下的字母才落到 word 分支。
# cjk 字符类的区间依次为：CJK 扩展 A（3400-4DBF）、统一表意文字（4E00-9FFF）、
# 兼容表意文字（F900-FAFF）、日文假名（3040-30FF）、谚文音节（AC00-D7AF）、扩展 B~G（星平面）。
_SCAN = re.compile(
    r"(?P<cjk>[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
    r"\U00020000-\U0003134f]+)"
    r"|(?P<num>[0-9]+)"
    r"|(?P<word>[^\W\d_]+)"
    r"|(?P<nl>\n+)"
    r"|(?P<sym>[^\s\w]+|_+)"
    r"|(?P<ws>[^\S\n]+)"
)

_CHARS_PER_WORD_TOKEN = 4   # 英文平均 ~4 字符/token（BPE 经验值）
_DIGITS_PER_TOKEN = 3       # Qwen 系 BPE 把数字按 1~3 位一组切

_lock = threading.Lock()
_models_dir: Path | None = None
_tokenizer: Any | None = None
_probed_dir: str | None = None      # 已探测过的目录（同一目录不重复探盘）
_backend: str = BACKEND_HEURISTIC


def configure_models_dir(models_dir: Path | None) -> None:
    """告知本地模型根目录（一般是 ``paths.models``），由切片/解析入口调用一次。

    做成显式配置而不是 import 时探测：count_tokens 的签名是冻结契约（不带 paths），
    但目录只有运行时才知道；目录变更时清缓存，便于用户装完 tokenizer 模组后重切生效。
    传 None 表示「不指定」，此时退回默认数据目录（见 _default_models_dir）。
    """
    global _models_dir, _tokenizer, _probed_dir, _backend
    with _lock:
        new = Path(models_dir) if models_dir is not None else None
        if _models_dir is not None and new is not None and str(new) == str(_models_dir):
            return
        _models_dir = new
        _tokenizer = None
        _probed_dir = None
        _backend = BACKEND_HEURISTIC


def load_local_tokenizer(models_dir: Path) -> Any | None:
    """尝试加载本地 Qwen tokenizer；任何一环缺失都返回 None（调用方回退启发式）。

    不抛异常：token 计数是切片的辅助设施，缺它只是估得糙一点，不该让整篇文档处理失败。
    """
    path = Path(models_dir) / TOKENIZER_SUBDIR / TOKENIZER_FILE
    if not path.is_file():
        return None
    try:
        from tokenizers import Tokenizer  # 依赖未安装时优雅降级（硬约束 3）
    except ImportError:
        return None
    try:
        return Tokenizer.from_file(str(path))
    except Exception:
        return None


def tokenizer_backend() -> str:
    """当前实际生效的后端（诊断包与日志用）：qwen-local | heuristic。"""
    _ensure_tokenizer()
    return _backend


def count_tokens(text: str) -> int:
    """文本的 token 数。真 tokenizer 可用时用真值，否则用保守启发式估算。"""
    if not text:
        return 0
    tok = _ensure_tokenizer()
    if tok is not None:
        try:
            return len(tok.encode(text, add_special_tokens=False).ids)
        except Exception:
            # 真 tokenizer 出错（版本不匹配/文件损坏）：永久降级，避免逐块反复抛异常
            _disable_tokenizer()
    return _estimate_tokens(text)


def reset_cache() -> None:
    """清空缓存实例（单测与模组安装后重新探测用）。"""
    global _tokenizer, _probed_dir, _backend
    _default_models_dir.cache_clear()
    with _lock:
        _tokenizer = None
        _probed_dir = None
        _backend = BACKEND_HEURISTIC


# ---------------------------------------------------------------- 内部实现


@lru_cache(maxsize=1)
def _default_models_dir() -> Path | None:
    """没人调用过 configure_models_dir 时的兜底目录：标准数据目录下的 models\\。

    为什么需要它：``count_tokens`` 的签名里没有 paths，而调用方分两类 —— rechunk 任务
    会先调 configure_models_dir，解析流水线却是直接调 chunk_document（拿不到 paths）。
    若不兜底，同一篇文档「解析时切」和「重切」会落到两套 token 口径上，token_count 前后
    对不上。这里按 02 章 §3 的固定布局自行推出目录（只读本地路径，不触网）。
    """
    try:
        from docfactory.config import Paths, default_data_root

        return Paths(root=default_data_root()).models
    except Exception:
        return None


def _ensure_tokenizer() -> Any | None:
    """模块级缓存：首次调用探盘加载，之后直接复用（tokenizers 的实例是线程安全的读操作）。"""
    global _tokenizer, _probed_dir, _backend
    with _lock:
        models_dir = _models_dir if _models_dir is not None else _default_models_dir()
        if models_dir is None:
            return None
        key = str(models_dir)
        if _probed_dir == key:
            return _tokenizer
        _probed_dir = key
        _tokenizer = load_local_tokenizer(models_dir)
        _backend = BACKEND_LOCAL if _tokenizer is not None else BACKEND_HEURISTIC
        return _tokenizer


def _disable_tokenizer() -> None:
    global _tokenizer, _backend
    with _lock:
        _tokenizer = None
        _backend = BACKEND_HEURISTIC


def _estimate_tokens(text: str) -> int:
    """启发式估算：按字符类别分段计费，对拼接近似可加（切片层依赖这一点做预算）。"""
    total = 0
    for m in _SCAN.finditer(text):
        kind = m.lastgroup
        span = m.end() - m.start()
        if kind == "cjk":
            total += span                                        # 中日韩：1 token/字
        elif kind == "num":
            total += (span + _DIGITS_PER_TOKEN - 1) // _DIGITS_PER_TOKEN
        elif kind == "word":
            total += (span + _CHARS_PER_WORD_TOKEN - 1) // _CHARS_PER_WORD_TOKEN
        elif kind == "sym":
            total += span                                        # 标点/符号逐个计（保守）
        elif kind == "nl":
            total += 1                                           # 连续换行合成一个 token
        # ws（行内空白）不单独计费：已并进相邻词的 4 字符/token 里
    return total
