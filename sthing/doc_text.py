#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""老 Word（.doc）和记事本（.txt）的正文抽取。**零依赖**（和 docx_text.py 同一风格）。

.doc（Word 97-2003）= OLE2 复合文档：
  ① 先解 OLE2 容器（头 + FAT + 目录 + mini 流），取出 WordDocument 流和
     1Table/0Table 流 —— 这里手写解析，不装 olefile；
  ② WordDocument 头（FIB）里拿到 nFib（版本）、fWhichTblStm（用哪个 Table 流）、
     fcClx/lcbClx（文字索引 CLX 在 Table 流里的位置）；
  ③ CLX 里的 PlcPcd 就是「文字分片表」（piece table）：每片 8 字节 PCD，
     FC 最高位 0x40000000 表示压缩（8-bit ANSI），否则 UTF-16LE。
     Word 快速保存过的文档会有多个分片，必须按表拼接，不能只读一段。
  参考：[MS-DOC] FibRgFcLcb97.fcClx / PlcPcd / Pcd.Fc。

.txt 就是编码嗅探：BOM → UTF-8 → GB18030 → UTF-8 替换。

出错一律抛 ValueError（带人话，直接能给用户看）。
"""
import struct

import docx_text
from docx_text import MAX_BYTES, MAX_CHARS

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENOFCHAIN = 0xFFFFFFFE
_MAX_STREAM = 32 * 1024 * 1024        # 单个流的合理上限（防环状 FAT 越读越大）


# ---------------------------------------------------------------- .txt

def extract_txt(data):
    """bytes → 正文。编码：BOM → UTF-8 → GB18030 → UTF-8 替换。"""
    if not data:
        raise ValueError("文件是空的")
    if len(data) > MAX_BYTES:
        raise ValueError("文本文件太大（上限 %d MB）" % (MAX_BYTES // (1024 * 1024)))
    # 有 BOM 按 BOM 来（UTF-16/32 里天然带 NUL，别当二进制拒了）
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"),
                     (b"\xff\xfe\x00\x00", "utf-32"),
                     (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"),
                     (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            try:
                text = data.decode(enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                text = None
    else:
        text = None
    if text is None:
        if b"\x00" in data[:4096]:
            raise ValueError("这看起来不是文本文件（二进制内容），请确认格式")
        # 没 BOM：UTF-8 → GB18030（记事本「ANSI」= GBK，GB18030 是它的超集）
        for enc in ("utf-8", "gb18030"):
            try:
                text = data.decode(enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                text = None
        if text is None:
            text = data.decode("utf-8", errors="replace")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = docx_text._tidy(text)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    if not text.strip():
        raise ValueError("这个文本文件里没有文字")
    return text


# ---------------------------------------------------------------- .doc（OLE2 容器）

def _ole_streams(data):
    """解 OLE2 容器 → {流名: bytes}。只做读正文需要的最小实现。"""
    if len(data) < 512:
        raise ValueError("老 Word 文档解析失败（文件太小，可能损坏）")
    sector_shift = struct.unpack_from("<H", data, 0x1E)[0]
    if sector_shift not in (9, 12):
        raise ValueError("老 Word 文档解析失败（扇区大小不对，可能损坏）")
    ss = 1 << sector_shift                       # 512 / 4096
    mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
    n_fat = struct.unpack_from("<I", data, 0x2C)[0]
    dir_start = struct.unpack_from("<I", data, 0x30)[0]
    mini_fat_start = struct.unpack_from("<I", data, 0x3C)[0]

    def sector_off(n):
        return (n + 1) * ss

    total_sectors = (len(data) - 512) // ss

    def read_sector(n):
        off = sector_off(n)
        if n >= total_sectors or off + ss > len(data):
            raise ValueError("老 Word 文档解析失败（扇区越界，文件可能损坏）")
        return data[off:off + ss]

    # FAT 扇区清单：文件头里 109 个 + DIFAT 链（超大文件才用得到）
    fat_sectors = []
    for i in range(109):
        v = struct.unpack_from("<I", data, 0x4C + i * 4)[0]
        if v != 0xFFFFFFFF:
            fat_sectors.append(v)
    difat_start = struct.unpack_from("<I", data, 0x44)[0]
    guard = 0
    while difat_start not in (0xFFFFFFFE, 0xFFFFFFFF) and guard < total_sectors:
        blk = read_sector(difat_start)
        for i in range(ss // 4 - 1):
            v = struct.unpack_from("<I", blk, i * 4)[0]
            if v != 0xFFFFFFFF:
                fat_sectors.append(v)
        difat_start = struct.unpack_from("<I", blk, ss - 4)[0]
        guard += 1

    fat = []
    for s in fat_sectors[:128]:                  # 8MB 文件用不了这么多 FAT 扇区
        blk = read_sector(s)
        fat.extend(struct.unpack("<%dI" % (ss // 4), blk))

    def chain(start):
        """沿 FAT 链读扇区号，带环状保护。"""
        out, cur, seen = [], start, set()
        while cur not in (0xFFFFFFFE, 0xFFFFFFFF):
            if cur in seen or cur >= total_sectors or len(out) > total_sectors:
                raise ValueError("老 Word 文档解析失败（存储链损坏）")
            seen.add(cur)
            out.append(cur)
            cur = fat[cur] if cur < len(fat) else 0xFFFFFFFE
        return out

    def read_chain(start, size):
        buf = b"".join(read_sector(s) for s in chain(start))
        return buf[:size]

    # 目录项（线性扫就够，不用管红黑树）
    entries = {}
    root = None
    dir_buf = read_chain(dir_start, total_sectors * ss)
    for off in range(0, len(dir_buf) - 127, 128):
        name_len = struct.unpack_from("<H", dir_buf, off + 0x40)[0]
        etype = dir_buf[off + 0x42]
        if etype not in (1, 2, 5) or name_len < 2:
            continue
        name = dir_buf[off:off + name_len - 2].decode("utf-16-le", errors="replace")
        start = struct.unpack_from("<I", dir_buf, off + 0x74)[0]
        size = struct.unpack_from("<Q", dir_buf, off + 0x78)[0] & 0xFFFFFFFF
        if etype == 5:
            root = (start, size)
        elif etype == 2:
            entries[name] = (start, size)

    # mini 流（< mini_cutoff 的小流挂在根目录的 mini 流里）
    mini_fat = []
    if mini_fat_start not in (0xFFFFFFFE, 0xFFFFFFFF):
        mf = read_chain(mini_fat_start, total_sectors * ss)
        mini_fat = list(struct.unpack("<%dI" % (len(mf) // 4), mf[:len(mf) // 4 * 4]))
    mini_stream = read_chain(root[0], root[1]) if root and root[1] else b""

    def read_stream(name):
        start, size = entries[name]
        if size > _MAX_STREAM:
            raise ValueError("老 Word 文档解析失败（流大小不合理）")
        if size < mini_cutoff and mini_fat:
            out, cur, seen = b"", start, set()
            while cur not in (0xFFFFFFFE, 0xFFFFFFFF):
                if cur in seen or len(out) > size + 64:
                    raise ValueError("老 Word 文档解析失败（mini 存储链损坏）")
                seen.add(cur)
                out += mini_stream[cur * 64:(cur + 1) * 64]
                cur = mini_fat[cur] if cur < len(mini_fat) else 0xFFFFFFFE
            return out[:size]
        return read_chain(start, size)

    return entries, read_stream


def extract_doc(data):
    """老 .doc（Word 97-2003）bytes → 正文。"""
    if not data:
        raise ValueError("文件是空的")
    if len(data) > MAX_BYTES:
        raise ValueError("Word 文件太大（上限 %d MB）" % (MAX_BYTES // (1024 * 1024)))
    if data[:8] != _OLE2_MAGIC:
        raise ValueError("这不是老版 .doc 文件（签名不对）")

    try:
        entries, read_stream = _ole_streams(data)
    except ValueError:
        raise
    except Exception:
        raise ValueError("老 Word 文档解析失败（文件可能损坏）")

    if "WordDocument" not in entries:
        if any(n in entries for n in ("Workbook", "Book")):
            raise ValueError("这是 Excel 文件，不是作文")
        if any(n in entries for n in ("PowerPoint Document", "Pictures")):
            raise ValueError("这是 PPT 文件，不是作文")
        raise ValueError("打不开这个 .doc（里面没有 Word 正文，可能损坏）")

    wd = read_stream("WordDocument")
    try:
        return _doc_text_from(wd, entries, read_stream)
    except (struct.error, IndexError, KeyError, UnicodeError):
        # 畸形/恶意文件：unpack 越界不是 ValueError，别让它变成 500
        raise ValueError("老 Word 文档解析失败（文件可能损坏）")


def _doc_text_from(wd, entries, read_stream):
    if len(wd) < 0x1AA:
        raise ValueError("老 Word 文档解析失败（文件头太短）")

    n_fib = struct.unpack_from("<H", wd, 2)[0]
    if n_fib < 193:
        raise ValueError("这个 .doc 太老了（Word 6/95 格式）。请用 Word 打开后"
                         "「另存为」→ 选 .docx 再上传")
    lid = struct.unpack_from("<H", wd, 6)[0]
    flags = struct.unpack_from("<H", wd, 0x0A)[0]
    tbl_name = "1Table" if (flags & 0x0200) else "0Table"
    fc_clx = struct.unpack_from("<I", wd, 0x1A2)[0]
    lcb_clx = struct.unpack_from("<I", wd, 0x1A6)[0]
    if tbl_name not in entries or not lcb_clx:
        raise ValueError("老 Word 文档解析失败（找不到文字索引）")

    tbl = read_stream(tbl_name)
    if fc_clx + lcb_clx > len(tbl):
        raise ValueError("老 Word 文档解析失败（文字索引越界）")
    clx = tbl[fc_clx:fc_clx + lcb_clx]

    # CLX：先跳过 Prc（0x01 开头），再找 Pcdt（0x02 开头）
    pos = 0
    while pos < len(clx) and clx[pos] == 0x01:
        cb = struct.unpack_from("<H", clx, pos + 1)[0]
        pos += 3 + cb
    if pos >= len(clx) or clx[pos] != 0x02:
        raise ValueError("老 Word 文档解析失败（文字索引结构不对）")
    lcb = struct.unpack_from("<I", clx, pos + 1)[0]
    plc = clx[pos + 5:pos + 5 + lcb]
    n = (len(plc) - 4) // 12
    if n < 1 or n > 200000:
        raise ValueError("老 Word 文档解析失败（分片表不对）")

    cps = struct.unpack_from("<%dI" % (n + 1), plc, 0)
    pcd_base = 4 * (n + 1)
    # 压缩分片是 8-bit ANSI，代码页看 FIB 的语言 ID（中文文档基本是 UTF-16 分片）
    # LANGID：低 10 位=主语言，高 6 位=子地区（0x0804=简中，&0x3FF 后是 0x0004）
    cp_map = {0x0804: "gbk", 0x1004: "gbk", 0x0404: "big5", 0x0C04: "big5",
              0x1404: "big5", 0x0411: "cp932", 0x0412: "cp949",
              0x0004: "gbk", 0x0011: "cp932", 0x0012: "cp949"}
    ansi = cp_map.get(lid, cp_map.get(lid & 0x03FF, "cp1252"))

    out = []
    for i in range(n):
        cp0, cp1 = cps[i], cps[i + 1]
        nchars = cp1 - cp0
        if nchars <= 0 or nchars > MAX_CHARS * 2:
            continue
        fc = struct.unpack_from("<I", plc, pcd_base + i * 8 + 2)[0]
        try:
            if fc & 0x40000000:                      # 压缩：8-bit
                off = (fc & 0x3FFFFFFF) >> 1
                raw = wd[off:off + nchars]
                out.append(raw.decode(ansi, errors="replace"))
            else:                                     # UTF-16LE
                off = fc & 0x3FFFFFFF
                raw = wd[off:off + nchars * 2]
                out.append(raw.decode("utf-16-le", errors="replace"))
        except Exception:
            continue

    text = "".join(out)
    # Word 控制符 → 人能看的样子
    text = (text.replace("\r\n", "\n").replace("\r", "\n")
                .replace("\x07", "\n").replace("\x0b", "\n")
                .replace("\x0c", "\n").replace("\x1e", "-").replace("\x1f", "")
                .replace("\xa0", " "))
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    text = docx_text._tidy(text)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    if not text.strip():
        raise ValueError("这个 Word 文档里没有文字（是空白文档？）")
    return text


# ---------------------------------------------------------------- 统一入口

def extract_any(data, name=""):
    """按**内容**分流（Word 互相改名也能认对）；认不出格式时看扩展名。

    报错统一带上文件名，用户一眼知道是哪个文件的问题。
    """
    if not data:
        raise ValueError("文件是空的")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        if data[:8] == _OLE2_MAGIC:
            return extract_doc(data)                     # 老 .doc / .xls / .ppt
        if data[:2] == b"PK":
            return docx_text.extract_text(data)          # .docx（OOXML）
        if data[:5] == b"{\\rtf":
            raise ValueError("这是 RTF 文件。请用 Word 打开后「另存为」.docx，"
                             "或复制文字存成 .txt 再传")
        # 内容不是任何 Word 格式：按扩展名收，别把乱码当正文
        if ext == "doc":
            raise ValueError("这不是老版 .doc 文件（签名不对）。如果里面只是文字，"
                             "把后缀改成 .txt 再传")
        if ext != "txt":
            raise ValueError("这不是 .docx 文件（打不开），请确认格式")
        return extract_txt(data)
    except ValueError as e:
        raise ValueError(("%s：%s" % (name, e)) if name else str(e))
