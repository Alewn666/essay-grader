#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流式 JSON 逐字段提取器 —— 让 AI 批改过程「逐项冒出来」。

**为什么不逐字流**：模型吐的是一个大 JSON，逐字推给前端会大量出现
半截括号、半截引号，前端只能靠猜，极易渲染出破相的内容。
这里只在**某个字段的值已经括号配平**时才把它解析出来推走 —— 稳，观感也够实时。

用法：
    emitted = set()
    for token in stream:                 # 模型边吐
        buf += token
        for key, value in extract_new_fields(buf, emitted):
            push(key, value)             # 前端边冒出来
"""
import json

# 推送顺序 = 用户想看的顺序：先定档理由与分数档位，再四维度扣分，最后修改后的长文。
FIELDS = (
    "band_reason",
    "base_score", "level", "level_range", "word_count",
    "score", "deductions",
    "summary", "on_topic",
    "dims", "comments", "highlights", "suggestions",
    "revised",
)


def _decode_string_literal(raw):
    """json.loads 的容错版（raw 是带引号的完整字符串字面量）。

    模型输出里可能夹非法 JSON 转义（Windows 路径 `C:\\Users`、公式 `\\alpha`），
    直接 json.loads 会抛 `Invalid \\escape` —— 而 scan_value 里一抛就炸掉整条
    批改/识别流（重试还一样挂）。这里退化成「反斜杠 + 原字符」原样保留，
    正常转义照常解码。宁可解得糙一点，也不让流断掉。
    """
    try:
        return json.loads(raw)
    except Exception:
        pass
    body = raw[1:-1] if len(raw) >= 2 and raw[0] == '"' else raw
    out, i, n = [], 0, len(body)
    simple = {'"': '"', "\\": "\\", "/": "/", "n": "\n",
              "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt == "u" and i + 6 <= n:
            try:
                cp = int(body[i + 2:i + 6], 16)
            except Exception:
                out.append(ch)     # \u 后面不是 hex → 反斜杠原样保留
                i += 1
                continue
            # ⚠️ 高代理 + 紧跟的低位转义要**合成一个真实字符**。只 chr() 单个转义的话
            #    `\ud83d\ude00` 会变成两个孤立代理 —— 落盘时 utf-8 编码直接抛
            #    UnicodeEncodeError，整篇作文写不进去（第二十六轮实锤的回归）。
            if 0xD800 <= cp <= 0xDBFF and body[i + 6:i + 8] == "\\u" and i + 12 <= n:
                try:
                    lo = int(body[i + 8:i + 12], 16)
                except Exception:
                    lo = -1
                if 0xDC00 <= lo <= 0xDFFF:
                    out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)))
                    i += 12
                    continue
            # 孤立代理（没配对的）→ 换 U+FFFD：留着它必炸编码，宁可显示成「�」
            out.append("\ufffd" if 0xD800 <= cp <= 0xDFFF else chr(cp))
            i += 6
        else:
            out.append(ch)         # 非法转义（\U \a \_ …）→ 反斜杠原样保留
            i += 1
    return "".join(out)


def fix_invalid_escapes(s):
    """JSON 文本里**字符串内**的非法转义（\\U \\_ \\a、\\u 非 4 位 hex…）→
    把反斜杠再转义（变 \\\\），json.loads 就能过，解出来是「反斜杠 + 原字符」。

    顶层字符串值由 _decode_string_literal 容错；这里补的是**嵌套值**那半边 ——
    comments/highlights 里写个 C:\\Users 就解析不出的对象/数组，兜一层再解。
    合法转义（\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX）原样不动。"""
    out, i, n = [], 0, len(s)
    in_str = False
    while i < n:
        ch = s[i]
        if in_str:
            if ch == "\\":
                if i + 1 >= n:
                    out.append("\\\\"); i += 1; continue
                nxt = s[i + 1]
                if nxt == "u":
                    hex4 = s[i + 2:i + 6]
                    if len(hex4) == 4 and all(h in "0123456789abcdefABCDEF" for h in hex4):
                        out.append("\\u"); i += 2; continue
                    out.append("\\\\u"); i += 2; continue
                if nxt in '"\\/bfnrt':
                    out.append(ch); out.append(nxt); i += 2; continue
                out.append("\\\\"); out.append(nxt); i += 2; continue
            if ch == '"':
                in_str = False
            out.append(ch); i += 1; continue
        if ch == '"':
            in_str = True
        out.append(ch); i += 1
    return "".join(out)


def _loads_tolerant(s):
    """json.loads 的容错版（整段 JSON 文本）：先直解，失败再修非法转义重解。"""
    try:
        return json.loads(s)
    except Exception:
        return json.loads(fix_invalid_escapes(s))


def scan_value(s, i):
    """从 s[i] 起扫一个 JSON 值。完整 → (值, 结束下标)；还没吐完 → (None, None)。

    不用 json.JSONDecoder.raw_decode，因为**它遇到不完整输入会抛异常**，
    而我们这里「不完整」是常态，必须能安静地返回 (None, None)。
    """
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    if i >= n:
        return None, None

    c = s[i]

    # ---- 字符串：要处理转义 ----
    if c == '"':
        j = i + 1
        while j < n:
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == '"':
                # 走容错解码：非法转义（C:\Users、\alpha）不许炸掉整条流
                return _decode_string_literal(s[i:j + 1]), j + 1
            j += 1
        return None, None            # 引号还没闭合

    # ---- 对象 / 数组：括号配平，且忽略字符串里的括号 ----
    if c in "{[":
        depth = 0
        j = i
        in_str = False
        while j < n:
            ch = s[j]
            if in_str:
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        try:
                            # 容错直解：嵌套值里的非法转义（C:\Users）不许让整段丢掉
                            return _loads_tolerant(s[i:j + 1]), j + 1
                        except Exception:
                            return None, None
            j += 1
        return None, None            # 还没配平

    # ---- 字面量 / 数字：扫到分隔符为止 ----
    j = i
    while j < n and s[j] not in ",}] \t\r\n":
        j += 1
    if j >= n and c not in "tfn":
        # 数字吐到一半（比如 "12" 可能是 "123" 的开头）→ 不确定就不推
        return None, None
    tok = s[i:j]
    if tok == "true":
        return True, j
    if tok == "false":
        return False, j
    if tok == "null":
        return None, j
    try:
        return json.loads(tok), j
    except Exception:
        return None, None


def extract_new_fields(buf, emitted, fields=None):
    """从当前缓冲区里捞出「新完成」的字段。emitted 会被就地更新做去重。

    fields 不传就用默认的 FIELDS（批改结果）；
    传 ("text", "title") 就是抄正文阶段的字段表。
    """
    out = []
    for key in (FIELDS if fields is None else fields):
        if key in emitted:
            continue
        needle = '"%s"' % key
        i = buf.find(needle)
        if i < 0:
            continue
        j = buf.find(":", i + len(needle))
        if j < 0:
            continue
        val, _end = scan_value(buf, j + 1)
        if _end is None:
            continue                  # 这个字段还没吐完，下一轮再试
        emitted.add(key)
        out.append((key, val))
    return out
