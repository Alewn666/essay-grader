#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从**现代 Word**（.docx / OOXML）里抽出作文正文。**零依赖**。

为什么不装 python-docx：
  ① .docx 本质就是个 zip，正文在 `word/document.xml`，抽纯文本用标准库足够
     （zipfile + xml.etree），NAS 上少一个依赖少一份构建/漏洞面；
  ② 我们只要「学生写的字」，不要样式、不要批注、不要页眉页脚，
     真上 python-docx 反而要写一堆「跳过什么」的判断。

⚠️ 老版 .doc（Word 97-2003，OLE2 复合文档）**不是 zip**，这里给的是明确的
   「请另存为 .docx」提示，而不是让用户看到一句「解析失败」。
"""
import io
import zipfile
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MAX_BYTES = 8 * 1024 * 1024        # 上传的 .docx 上限（学生作文用不上更大）
MAX_XML = 24 * 1024 * 1024         # 解压后 document.xml 上限（防 zip 炸弹）
MAX_CHARS = 30000                  # 正文上限，防粘贴一部长篇小说

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"     # 老 .doc / .xls 的签名


def _para_text(p):
    """一个段落里的文字：w:t 取文本，w:tab→制表符，w:br/w:cr→换行。

    用 iter() 递归，`w:hyperlink` 里的 w:r/w:t 也能一起收进来。
    """
    out = []
    for node in p.iter():
        tag = node.tag
        if tag == W + "t":
            out.append(node.text or "")
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def _cell_text(tc):
    """表格单元格：里面的段落用空格连起来（一行一格）。"""
    parts = [_para_text(p).strip() for p in tc.findall(W + "p")]
    return " ".join([x for x in parts if x])


def _tidy(text):
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    out, blank = [], 0
    for ln in lines:
        if ln.strip():
            blank = 0
            out.append(ln)
        else:
            blank += 1
            if blank <= 1:             # 连续空行压缩成一个
                out.append("")
    while out and not out[-1].strip():
        out.pop()
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out)


def extract_text(data):
    """bytes → 正文纯文本。出错一律抛 ValueError（带人话，直接能给用户看）。"""
    if not data:
        raise ValueError("文件是空的")
    if len(data) > MAX_BYTES:
        raise ValueError("Word 文件太大（上限 %d MB）" % (MAX_BYTES // (1024 * 1024)))
    if data[:8] == _OLE2_MAGIC:
        raise ValueError("这是老版 .doc（Word 97-2003）。请用 Word 打开后"
                         "「另存为」→ 选 .docx 再上传")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        raise ValueError("这不是 .docx 文件（打不开），请确认格式")

    try:
        names = zf.namelist()
        if "word/document.xml" not in names:
            # 常见误传：.doc 改了后缀、.txt、Excel/PPT 的 OOXML
            if any(n.startswith("xl/") for n in names):
                raise ValueError("这是 Excel 文件，不是作文")
            if any(n.startswith("ppt/") for n in names):
                raise ValueError("这是 PPT 文件，不是作文")
            raise ValueError("这个 .docx 里没有正文（不是 Word 文档？）")
        info = zf.getinfo("word/document.xml")
        if info.file_size > MAX_XML:
            raise ValueError("文档解压后过大，拒绝处理")
        xml = zf.read("word/document.xml")
    finally:
        zf.close()

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ValueError("Word 文档内容损坏（XML 解析失败）：%s" % e)

    body = root.find(W + "body")
    if body is None:
        raise ValueError("这个 .docx 里没有正文")

    blocks = []
    for el in list(body):
        if el.tag == W + "p":
            blocks.append(_para_text(el))
        elif el.tag == W + "tbl":
            # 表格：一行一行来，格子之间用制表符，读起来跟原文顺序一致
            for tr in el.findall(W + "tr"):
                cells = [_cell_text(tc) for tc in tr.findall(W + "tc")]
                blocks.append("\t".join(cells))
        # 其它（sectPr 等）忽略

    text = _tidy("\n".join(blocks))
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    if not text.strip():
        raise ValueError("这个 Word 文档里没有文字（是空白文档？）")
    return text
