"""书籍导入纯逻辑：文本归一、txt 解码、章节切分与分块（v1.27.0，#35）。

零插件依赖、零 I/O：函数只接收 bytes/str，返回结果或抛类型化异常，
测试可同步运行、不起事件循环（防卡死硬约束，见设计文档 §6）。
epub 解析在前端完成（宿主上传通道仅收图片）；服务端只校验提交的文本。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

CHUNK_CHARS = 65536          # 单块 action 字符数防御值（宿主 RPC 上限未实测，见设计文档 §7）
MAX_BOOK_CHARS = 2_000_000   # 单书字符上限默认值；config.creation.book_import_max_chars 可收紧


class BookImportError(ValueError):
    """书籍导入类型化错误；code 供前端展示与测试断言。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class BookParse:
    title: str = ""
    author: str = ""
    chapters: list[dict] = field(default_factory=list)  # [{"title","text"}]
    total_chars: int = 0
    sha256: str = ""
    warnings: list[str] = field(default_factory=list)


def normalize_text(text: str) -> str:
    """空白归一：与去重、内容指纹同一口径。"""
    return " ".join(str(text or "").split())


def content_sha256(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def decode_txt(raw: bytes) -> tuple[str, list[str]]:
    """UTF-8 严格解码 → GB18030 回退；返回 (文本, 告警列表)。"""
    warnings: list[str] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        warnings.append("已去除 UTF-8 BOM")
    try:
        return raw.decode("utf-8"), warnings
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode("gb18030")
        warnings.append("文件不是 UTF-8，已按 GB18030 解码")
        return text, warnings
    except UnicodeDecodeError as exc:
        raise BookImportError("encoding", f"无法识别文本编码：{exc}") from exc


_CHAPTER_RE = re.compile(
    r"^[ \t]*(第[0-9一二三四五六七八九十百千零两]+[章节回卷集部篇][^\n]{0,40}"
    r"|Chapter\s+\d+[^\n]{0,40}|CHAPTER\s+\d+[^\n]{0,40})[ \t]*$",
    re.M,
)


def _split_chapters(text: str) -> list[dict]:
    """按章节标题行启发式切分；无匹配整本作为一章。"""
    matches = list(_CHAPTER_RE.finditer(text))
    if not matches:
        return [{"title": "全文", "text": text}]
    chapters: list[dict] = []
    if matches[0].start() > 0:
        preface = text[:matches[0].start()].strip()
        if preface:
            chapters.append({"title": "前言", "text": preface})
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chapters.append({"title": match.group(1).strip()[:60], "text": text[match.end():end].strip()})
    return chapters


def parse_txt(raw: bytes, *, title: str = "") -> BookParse:
    """服务端 txt 兜底解析（前端已解析时只走 validate_submission）。"""
    if not raw or not raw.strip():
        raise BookImportError("empty", "文件为空")
    text, warnings = decode_txt(raw)
    text = text.replace("\x00", "")
    if not text.strip():
        raise BookImportError("empty", "解码后没有可读文本")
    return BookParse(title=title, chapters=_split_chapters(text), total_chars=len(text),
                     sha256=content_sha256(text), warnings=warnings)


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    size = max(1, int(size))
    return [text[i:i + size] for i in range(0, len(text), size)]


def validate_submission(*, title: str, total_chars: int, max_chars: int) -> None:
    """begin/commit 服务端校验：书名非空、字数为正且不超上限。"""
    if not str(title or "").strip():
        raise BookImportError("title", "书名不能为空")
    if int(total_chars) <= 0:
        raise BookImportError("empty", "书籍内容为空")
    if int(total_chars) > int(max_chars):
        raise BookImportError("too_large", f"书籍超过 {int(max_chars)} 字符上限")