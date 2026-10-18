#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作文批改网页 —— 拍照/上传作文 → AI 修改建议 + 修改后的文章
数据全部落在项目文件夹的 data/ 下。
"""
import os
import io
import re
import json
import time
import uuid
import base64
import hmac
import hashlib               # 登录口令哈希（PBKDF2-SHA256）
import tarfile               # 数据备份/导出打包
import threading
import urllib.request
import urllib.error
import urllib.parse          # 显式导入：api_report 用 quote，别靠 request 的隐式导入兜
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, render_template, send_from_directory, Response, session
from stream_json import extract_new_fields
# 作文文档正文抽取（.docx / 老 .doc / .txt）：零依赖，见 docx_text.py / doc_text.py
# ⚠️ 别名带 _mod 后缀：路由里有个局部变量也叫 docx_text（正文文本），别撞名
import doc_text as doc_text_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("ESSAY_DATA_DIR", os.path.join(BASE_DIR, "data"))
ESSAY_DIR = os.path.join(DATA_DIR, "essays")
IMG_DIR = os.path.join(DATA_DIR, "images")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

for _d in (DATA_DIR, ESSAY_DIR, IMG_DIR):
    os.makedirs(_d, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 24 * 1024 * 1024  # 24MB
# 页面内登录用 session —— 随机 secret key（重启后 session 全部失效，重新登录即可）
app.secret_key = os.urandom(32)
# 中文不要转义成 \uXXXX（体积膨胀约 3 倍，也不好调试）
app.json.ensure_ascii = False

DEFAULT_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.3,   # 结构化 JSON 输出，温度别高（0.6 时模型会随机吐裸引号弄坏 JSON）
    "grader_name": "AI 阅卷老师",
    "grade_mode": "fast",  # 改卷模式：fast=快速（关思考）/ deep=深度（少量思考+硬上限）
    # ---- 访问口令（setup.sh 首次运行时写入，PBKDF2 哈希）----
    # 空串 = 还没设口令，网站**整体锁定**，页面提示去终端跑 setup.sh。
    # 格式：sha256:<盐hex>.<派生键hex>
    "password_hash": "",
    # ---- 服务端口（setup.sh 里选的，记住下次直接用）----
    "port": 8083,
}

# ---------------------------------------------------------------- 配置

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    for key, env in (
        ("base_url", "ESSAY_BASE_URL"),
        ("api_key", "ESSAY_API_KEY"),
        ("model", "ESSAY_MODEL"),
    ):
        v = os.environ.get(env)
        if v:
            cfg[key] = v
    return cfg


def save_config(cfg):
    """写配置。**原子写**（照抄 save_essay 的三步）：config.json 里是 API key +
    多套配置存档，写到一半被杀/OOM 就留半截 JSON，load_config 会**静默吞异常**
    退回默认配置 —— 用户看到的是「key 凭空消失了」，其实是文件写坏了。"""
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # ⚠️ 新建的临时文件权限跟 umask 走（022 → 644），os.replace 会把这个权限
        #    一起带过去 —— 原来 600 的 config.json 只要在网页上存一次设置就掉回 644，
        #    而它里面存着 DeepSeek/mimo 两把 key（第二十九轮遗留 #3）。
        #    先 chmod 再替换，权限才落得下来。
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _norm_profiles(cfg):
    """API 配置「存档库」归一化：老配置文件没有 profiles → 把当前三字段收编成一套。

    ⚠️ 当前生效值永远是顶层 base_url/model/api_key —— call_llm/iter_llm_tokens
       一个字都不用改；profiles 只是存档，切换 = 把某套的值抄到顶层。
    """
    if not isinstance(cfg.get("profiles"), list):
        cfg["profiles"] = []
    if not cfg["profiles"]:
        cfg["profiles"] = [{"name": "默认配置",
                            "base_url": cfg["base_url"],
                            "model": cfg["model"],
                            "api_key": cfg.get("api_key", "")}]
    ap = cfg.get("active_profile")
    if not isinstance(ap, int) or not (0 <= ap < len(cfg["profiles"])):
        cfg["active_profile"] = 0
    return cfg


def mask_key(k):
    """API key 掩码。**短 key 全掩**：只掩中间几个字符的话，截个屏就泄了
    （key 常见长度 20~50，留 4+4 足够辨认是哪一套）。"""
    if not k:
        return ""
    n = len(k)
    if n <= 12:
        return "*" * n                       # 太短，露任何一段都等于泄露
    if n <= 20:
        return k[:2] + "*" * (n - 4) + k[-2:]
    return k[:4] + "*" * (n - 8) + k[-4:]


# ---------------------------------------------------------------- 存储

def _clean_surrogates(obj):
    """把**孤立代理字符**换成 U+FFFD（递归 dict/list/str）。

    ⚠️ 为什么要：模型偶尔吐 `\\ud83d` 这种没配对的高位转义，json.loads 会**顺利解出来**
       （不报错），但这个字符串 utf-8 编码时直接抛 `UnicodeEncodeError: surrogates
       not allowed` —— 落盘（json.dump ensure_ascii=False）和发 SSE 都在这一步炸，
       整条批改流失败 / 追问记录写不进去（2026-09-26 第二十六轮实锤）。
       合法 emoji 是成对的，早就合成一个字符了，不会被这条误伤。
    """
    if isinstance(obj, str):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in obj):
            return "".join("\ufffd" if 0xD800 <= ord(c) <= 0xDFFF else c for c in obj)
        return obj
    if isinstance(obj, dict):
        return {k: _clean_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_surrogates(v) for v in obj]
    return obj


def essay_path(eid):
    return os.path.join(ESSAY_DIR, eid + ".json")


def load_essay(eid):
    p = essay_path(eid)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_essay(doc):
    """写盘。**原子写**：先写同目录临时文件，fsync，再 os.replace 换过去。

    ⚠️ 绝不许退回 `json.dump(open(p, "w"))`：写到一半进程被 kill / OOM，
       盘上就留一个半截 JSON，这篇作文**永久打不开**。
       2026-09-25 版本历史删掉之后这是唯一一份数据，没有第二份可回滚了。

    不留快照、不留版本历史（用户要求；AI 批改只写 result 不动正文，风险可控）。
    """
    eid = doc.get("id")
    if eid and _is_deleted(eid):
        # 这篇已经被用户删了 —— 手里这份旧快照不许把它写回盘上（诈尸）。
        return
    doc = _clean_surrogates(doc)      # 孤立代理会让 utf-8 编码炸掉整次落盘
    p = essay_path(doc["id"])
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())      # 先落盘，再换名 —— 顺序反了等于没做
        os.replace(tmp, p)            # 同目录 rename，原子
    except BaseException:
        # 写失败 / 被中断 → 自己把半截临时文件清掉，别在目录里留垃圾。
        # （SIGKILL 抓不到，那种残留会在下次保存时被同名覆盖，不会越积越多。）
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge_user_changes(doc, cur=None):
    """整份写回之前，把「批改这段时间里用户在盘上做的改动」合并进来，别覆盖掉。

    - `qa` 是只增不改的追加型数据，两边取长的那个就是对的（用户完全可能
      「批改跑到一半去追问了一句」）。
    - 正文：盘上 `text_edited=True` 且文本与内存这份不同 = **批改期间用户手改过**，
      用户的算数（他改的字不能被批改落盘悄悄写回旧版）。

    取消/出错还原（restore_essay）和成功落盘都要过这一层 —— 第十六轮审查 #1。
    """
    try:
        if cur is None:
            cur = load_essay(doc.get("id"))
    except Exception:
        cur = None
    if not cur:
        return doc               # 读不到就当没有，写回优先
    if len(cur.get("qa") or []) > len(doc.get("qa") or []):
        doc["qa"] = cur["qa"]
    if cur.get("text_edited") and (cur.get("text") or "") != (doc.get("text") or ""):
        doc["text"] = cur.get("text") or ""
        doc["text_edited"] = True
    return doc


def restore_essay(snapshot):
    """把作文还原成批改前的快照（批改取消 / 出错 / 客户端断开时用）。

    ⚠️ 但用户在这期间改的东西要保住（追问、手改的正文）—— 整份还原会把
       那些一起吞掉。合并规则见 _merge_user_changes。
    """
    _merge_user_changes(snapshot)
    save_essay(snapshot)


def _anchor_annotations(text, highlights):
    """把 AI 给的 highlights 锚定到正文偏移。

    ⚠️ quote 必须**逐字**出现在原文里才收；找不到就丢掉。
    宁可少标一处，也不能让红标错位到别的句子上。
    """
    out, used = [], []
    for h in (highlights or []):
        quote = (h.get("quote") or "").strip()
        if len(quote) < 4:
            continue
        hint = h.get("start_hint")
        start = -1
        if isinstance(hint, int) and 0 <= hint < len(text):
            start = text.find(quote, max(0, hint - 20))
        if start < 0:
            start = text.find(quote)
        if start < 0:
            continue                      # AI 抄漏字/改了字 → 丢弃，绝不猜位置
        end = start + len(quote)
        if any(not (end <= s or start >= e) for s, e in used):
            continue                      # 与已收的重叠 → 丢弃
        used.append((start, end))
        out.append({
            "start": start,
            "end": end,
            "start_hint": start,
            "quote": quote,
            "issue": (h.get("issue") or "").strip(),
            "suggest": (h.get("suggest") or "").strip(),
            "para": h.get("para"),
        })
    out.sort(key=lambda x: x["start"])
    return out


def list_essays():
    out = []
    for name in os.listdir(ESSAY_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(ESSAY_DIR, name), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        out.append({
            "id": d.get("id"),
            "title": d.get("title") or "未命名作文",
            "category": d.get("category") or "",      # 文体类别（可空 = 未指定）
            "status": d.get("status", "draft"),
            "created_at": d.get("created_at", 0),
            "chars": _count_chars(d.get("text") or ""),   # 与报告字数同口径（非空白实测）
            "pages": len(_essay_images(d)),
            "has_image": bool(_essay_images(d)),
            "has_docx": bool(d.get("docx_name")),      # 正文来自 .docx（列表显示 📝）
            "has_document": bool(d.get("docx_name")),  # 与详情页同名字段（index.html 提示条用）
            "has_prompt": bool((d.get("prompt") or "").strip() or d.get("prompt_image")),
            "score": (d.get("result") or {}).get("score"),
            "level": (d.get("result") or {}).get("level"),
            "qa": len(d.get("qa") or []),      # 追问条数（列表页显示 💬 N）
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


# ---------------------------------------------------------------- 提示词

# 文章类别（文体）：批改时按对应文体的要求评，报告和详情页都会显示。
# —— 用户在新建时选；留空 = 让 AI 自己判断。
CATEGORIES = ["记叙文", "议论文", "说明文", "散文", "书信", "读后感", "演讲稿", "想象作文", "其他"]


def _norm_category(value, strict=False):
    """把用户传来的类别规整成白名单里的值。空/None → ''（让 AI 自己判断）。

    strict=True 时传了白名单外的值直接报错（api_update 用，避免悄悄丢改动）。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if v in CATEGORIES:
        return v
    if strict:
        raise ValueError("文章类别无效：%s" % v)
    return ""


def _genre_text(doc):
    """给批改提示词用的文体描述：选了类别就带一句要求，没选就让 AI 自己判断。"""
    cat = _norm_category(doc.get("category"))
    return cat if cat else "（未指定文体 —— 请根据题目要求和文章体征自行判断文体，按对应文体要求评判）"


# ---------------- 评分严格度（设置页可选，2026-09-26 加）----------------
# 用户反馈「给分有点高了」→ 实测 7 篇里 4 篇被判一类文（58/56/55/55），确实偏松。
# 中考里一类文是极少数尖子作文，普通作文常态在三类（42~48）。
# 根因：原提示词没有任何「严格度基线 / 硬伤否决 / 就低不就高」约束，模型天然爱表扬，
#       加上快速档关掉思考后更容易第一直觉定在高档。
# 所以默认 strict（严格，把真实阅卷尺度写死进提示词）；standard 保留原口径。
_STRICT_RULES = """【评分严格度：严格（按真实中考阅卷尺度，必须遵守）】
- **先看分布**：一类文（55~60）是极少数尖子作文（大致全市前 10%）；二类（49~54）是明显写得好的；
  **大多数普通作文应落在三类（42~48）**。不要因为「写完了、通顺、感情真实」就给一类或二类。
- **先列硬伤再定档**：band_reason 里**先逐条写出这篇的硬伤/问题**（错别字、病句、选材俗套、
  立意平庸、详略失当、结构不完整、语言平淡、口号化…），再写定档理由。
- **硬伤否决（出现任一条即不得进一类）**：
  · 错别字或病句 3 处以上；· 选材烂大街（扶老人、雨中送伞、考试失利后突然发奋之类）；
  · 立意停留在口号或表面；· 详略严重失当或结构不完整；· 语言平淡、几乎没有描写或修辞。
  若同时出现两条以上、或某一维度明显不合格 →直接判**三类及以下**。
- **就低不就高**：在两档之间拿不准时，取**低**的那一档。
- **不许鼓励式抬分**：写得真、字数够、没有大错，只是及格线以上的基本要求，不构成抬档理由。
- 四维度等第要有区分度：四个维度真的一样好才给同一档，说得出差别就给得出区分。
"""

_STANDARD_RULES = "【评分严格度：标准】按上面的评分标准正常把握尺度即可，不必刻意从严。\n"


def _strictness_text(cfg):
    """按设置返回评分严格度说明。缺省 / 非法值一律按 strict（严格）。"""
    mode = (cfg or {}).get("grade_strict")
    if mode == "standard":
        return _STANDARD_RULES
    return _STRICT_RULES


PROMPT = """你是一位资深中考语文阅卷老师，正在批改一篇学生作文。请严格按照下面这套《中考作文评分标准（60分）》批改。

==================== 评分标准（必须严格照此执行）====================

【评分维度】情境 · 内容 · 结构 · 语言（共 4 个维度）

【五档标准】

■ 一类（55~60 分）
  情境：情境把握准确／写作目的明确／读者对象分明／写作角色合理
  内容：切合题意／中心突出／感情真挚／选材新颖／内容充实
  结构：构思巧妙／条理清晰／详略得当／过渡自然
  语言：词汇丰富／手法多样／准确流畅

■ 二类（49~54 分）
  情境：情境把握准确／写作目的明确／读者对象较分明／写作角色较合理
  内容：符合题意／中心突出／感情真实／选材适当／内容具体
  结构：结构完整／条理清楚／详略得当／过渡较自然
  语言：词汇较丰富／手法较多样／准确通顺／错别字极少

■ 三类（42~48 分）
  情境：情境把握较准确／写作目的较明确／读者对象较分明／写作角色较合理
  内容：较符合题意／中心较突出／感情较真实／选材较合理／内容较具体
  结构：结构较完整／条理较清楚／详略较得当／过渡不够自然
  语言：词汇不够丰富／比较通顺／错别字较少

■ 四类（36~41 分）
  情境：情境把握基本准确／写作目的基本明确／读者对象不够分明／写作角色不够合理
  内容：基本符合题意／中心不太明确／感情不够真实／选材不够合理／内容不够具体
  结构：结构不够完整／条理不够清楚／详略不够得当／过渡不自然
  语言：不够通顺／表意不够清楚／错别字较多

■ 五类（0~35 分）
  情境：情境把握不准确／写作目的不明确／读者对象不分明／写作角色不合理
  内容：偏离题意／无写作中心／感情虚假／选材不合理／内容单薄
  结构：结构散乱／条理混乱／缺乏详略／没有过渡
  语言：不通顺／表意不清楚／错别字多

【扣分规则】
- 无标题扣 2 分；
- 字数不足 500 字，每少 50 字扣 1 分，扣满 3 分为止。

【打分方法】**先整体定档，再在该档区间内给具体分**，不是逐项相加。
然后**在档次分基础上额外扣分**（按上面的扣分规则）。

{strictness}

================================================================

【考试作文题目】（考试卷上给的完整题目，含材料、写作要求、命题）
{prompt}

【作文标题】{title}

【文章类别】{genre}
—— 批改时请结合该文体的常规要求（如演讲稿要有对象感和鼓动性、书信要有称呼和格式、
议论文要有明确论点和论证、说明文要准确有条理、记叙文要有六要素、读后感要读感结合等）；
文体特征不符要在「内容」或「情境」维度的 comment 里点出来。

【学生原文】
{text}

【系统实测数据】正文实测字数：{wc} 字（含标点）——这是系统数出来的准数，
word_count 字段直接照抄这个数字，不要自己数。

请**只输出一个 JSON 对象**（不要任何解释文字、不要 markdown 代码块）：

{{
  "band_reason": "定档依据：先对照四维度锚点逐维度判等，再说明为什么落在这一档，1~2 句",
  "base_score": 档次分（整数，落在 55~60 / 49~54 / 42~48 / 36~41 / 0~35 中对应档区间内）,
  "level": "一类文/二类文/三类文/四类文/五类文",
  "level_range": "对应档的分数区间，例如「49~54 分」",
  "word_count": 正文实测字数（直接照抄【系统实测数据】的数字）,
  "deductions": [
    {{"rule": "扣分项名称，如「无标题」或「字数不足 500 字」", "points": 扣的分数(整数), "note": "具体说明，如「实写 470 字，少 30 字，扣 1 分」"}}
  ],
  "score": 最终分（整数 = base_score 减去 deductions 的 points 之和，最低 0）,
  "summary": "总评，2~3 句话，先说亮点再说核心问题",
  "on_topic": "是否扣题的判断。若题目没给就写「未提供题目，暂不判断扣题」；给了题目就明确写：扣题/偏题/跑题 + 依据。",
  "dims": [
    {{"name": "情境", "level": "一类/二类/三类/四类/五类", "comment": "对照标准具体说明：情境把握、写作目的、读者对象、写作角色这四项各自如何"}},
    {{"name": "内容", "level": "同上", "comment": "对照标准说明：切合题意、中心、感情、选材、内容充实度"}},
    {{"name": "结构", "level": "同上", "comment": "对照标准说明：构思、条理、详略、过渡"}},
    {{"name": "语言", "level": "同上", "comment": "对照标准说明：词汇、手法、流畅度、错别字"}}
  ],
  "comments": [
    {{"para": 1, "quote": "该段原文摘录（不超过 20 字）", "good": "写得好的地方", "improve": "需要改的地方+具体改法", "risk": "偏题/逻辑风险，没有就空字符串"}}
  ],
  "highlights": [
    {{"para": 1, "quote": "原文里逐字摘录的一句话（不超过 25 字）", "issue": "这句话哪里可以改进", "suggest": "具体怎么改，最好直接给改好的句子", "start_hint": 这句话在原文里的字数位置(整数，从0数起，不确定就给0)}}
  ],
  "suggestions": ["提分建议1（具体可操作）", "提分建议2", "提分建议3"],
  "revised": "修改后的完整文章。保持学生的原意、年龄和口吻，不要写成成人范文；只把语言改通顺、结构理顺、细节补足、错别字病句改掉，删掉空洞口号。分段用 \\n 分隔。"
}}

要求：
- **先写 band_reason 再给分数**：先对照五档锚点逐维度判等（dims），综合定档，
  把定档依据写进 band_reason，然后才写 base_score。顺序不能反。
- **严格按上面的五档标准定档**，不要自己发明档位或分数段。
- **情境维度必须认真评**：写作目的是否明确？读者对象是谁、是否分明？写作角色（以什么身份写）是否合理？
  这是这套标准的核心，不能笼统带过。
- comments 要覆盖作文的每一个自然段（逐段批注）。
- highlights 挑 3~6 处**最值得改**的句子，这些句子会在原文里被标红给学生看。
  quote 必须**逐字照抄原文**（含标点，一个字都不能改、不能用省略号、不能自己重写），
  系统会用它在原文里定位，抄错了整条批注就作废。
  issue 说清问题类型（词不达意／啰嗦／病句／错别字／空洞口号／逻辑跳跃／搭配不当…），
  suggest 给具体改法，尽量直接写出改好的句子。不要为了凑数硬挑。
- 语言要像真实阅卷老师在写评语，不要 AI 腔。
- revised 必须是完整全文，不能只写片段。
- 如果给了考试题目，先审题再评「内容」维度的「切合题意」，并写进 on_topic。
- 如果题目里给了文体、字数等要求，要检查是否达标，在 comments 的 risk 里标注。
- deductions 只写两条机械扣分：无标题扣 2 分；字数不足 500 字每少 50 字扣 1 分
  （不满 50 字按 1 算，最多扣 3 分）。字数一律以【系统实测数据】为准。
  没有扣分就给空数组 []。系统会按规则复核重算，算错了会被自动纠正。
- score = base_score 减去 deductions 的 points 之和（最低 0 分）。系统同样会复核。
"""


def call_llm(cfg, prompt, images=None, system=None, think=None):
    """调用 OpenAI 兼容的 chat/completions 接口。

    images: 传入本地图片文件名列表 → 走多模态（image_url + base64）。
    think:  思考档位，见 _think_body。None = 不管（模型自由发挥）。
    """
    url = cfg["base_url"].rstrip("/") + "/chat/completions"

    if images:
        content = []
        for fname in images:
            data = _read_image_datauri(fname)
            if data:
                content.append({"type": "image_url",
                                "image_url": {"url": data}})
        content.append({"type": "text", "text": prompt})
    else:
        content = prompt

    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system or "你是一位资深语文阅卷老师，只输出严格合法的 JSON。"},
            {"role": "user", "content": content},
        ],
        "temperature": float(cfg.get("temperature", 0.3)),
        "stream": False,
    }
    body.update(_think_body(think, cfg))   # 思考控制（None → {} 不传参数；按服务商方言）
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
        method="POST",
    )
    _TLS.stream_break = ""      # 清掉本线程上一次流式调用留下的断流痕迹，别串台
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:400]
        raise RuntimeError("接口返回 %s：%s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("调用失败：%s" % e)

    content = data["choices"][0]["message"]["content"]
    return parse_json(content)


class _Cancelled(Exception):
    """用户主动取消批改。"""


# 「AI 输出流中途断了」的痕迹 —— 线程局部（gthread 下每个请求一个线程，不会串台）。
# 用途：半截内容拼不出 JSON 时，报错能说清是网络中断而不是格式问题。
_TLS = threading.local()


def _note_stream_break(err):
    _TLS.stream_break = str(err)


def _take_stream_break():
    v = getattr(_TLS, "stream_break", "")
    _TLS.stream_break = ""
    return v


def _err_msg(e):
    """报错文案：把「断流」和「JSON 真有问题」分开说。

    以前网络中途断了、半截内容拼不出 JSON，报的却是「JSON 解析不了」，
    排查方向被带偏到提示词/格式上（第十六轮审查 #13）。
    """
    msg = str(e)
    brk = _take_stream_break()
    if brk:
        msg += "（AI 输出流中途断了：%s；已收到部分内容但拼不成完整结果，重试一次通常就好）" % brk
    return msg


# 正在批改、且已被用户点了「取消」的作文 id。
# ⚠️ 用显式取消端点，而不是靠「客户端断开」—— 断开时机不确定，
#    服务端还原数据会有竞态，客户端也拿不到「已取消」的确认。
_CANCEL = set()

# 正在流式回答追问的作文 id → 开始时间（同一个作文不允许同时开两条追问流），
# 以及被点了「停止」的追问。
# ⚠️ 记时间是为了**自愈**：万一生成器一次都没被迭代（客户端刚发完就断了），
#    或者上游卡死、连接迟迟不断（urlopen 是 600 秒空等），finally 里的清理
#    根本没机会跑，这个 id 就永远卡在「正在回答」——用户看到的一直是 409。
# ⚠️ 上限要比「一次正常追问的最长寿命」大、比「让人等到烦」小：
#    追问的回答本身就 200 字以内，配合 _ASK_TIMEOUT 的空闲超时，
#    正常绝不会跑到 3 分钟；跑到了就是真的卡住，该放行让用户重问。
_ASKING = {}
_ASK_CANCEL = set()
_ASK_STALE = 180.0        # 秒：登记超过这个岁数就当过期作废

# 追问时给上游的空闲超时：每读到一块就算一次活动。
# 批改沿用原来的 600 秒；追问的回答短，90 秒还没动静就是接口卡了，
# 早点报错，别把并发槽位（和用户的时间）一起拖住。
_ASK_TIMEOUT = 90

# 正在流式批改的作文 id → 开始时间。
# ⚠️ 为什么必须有：gunicorn worker 被杀（300 秒超时）、容器重启、OOM 时，生成器
#    **没机会跑 finally**，盘上就永远留着 status="revising" —— 界面永远「批改中」，
#    用户再也没法重新批改（2026-09-25 14:30 真机踩到：部署重启掐断流 → 那篇卡死）。
#    内存表在重启后是空的 → 任何 revising 都是陈的，GET 时顺手复位。
_REVISING = {}
_REVISING_STALE = 420.0   # 秒：登记超过这么久仍没结束，就当那条流早死了

# 删除「墓碑」：删除接口记一笔，save_essay 拒绝再把这篇写回盘上。
# ⚠️ 为什么必须有：批改流手里捏着一份旧 doc，用户把这篇删了（JSON + 图片全删），
#    流跑完照样 save_essay(doc) → **被删的作文整份诈尸复活**，图片却是删掉的。
_DELETED = {}
_DELETED_TTL = 3600.0    # 墓碑只护一小时（流最长 420s，足够了），别无限攒


def _is_deleted(eid):
    t0 = _DELETED.get(eid)
    if t0 is None:
        return False
    if time.time() - t0 > _DELETED_TTL:
        _DELETED.pop(eid, None)
        return False
    return True


# 批改/追问登记的原子锁：检查→登记必须一体，否则两个标签页同时点批改，
# 两条流都能穿过 _revising_busy 检查（TOCTOU），又回到互相覆盖的老问题。
_REG_LOCK = threading.Lock()


def _revise_register(eid):
    """尝试登记一条批改流。占到坑 → True；这篇正忙 → False。原子，无竞态窗口。"""
    now = time.time()
    with _REG_LOCK:
        t0 = _REVISING.get(eid)
        if t0 is not None and (now - t0) < _REVISING_STALE:
            return False
        _REVISING[eid] = now
        return True


def _ask_register(eid):
    """追问同款原子登记（_ASKING 在 api_ask 附近定义，此处共用一把锁）。"""
    now = time.time()
    with _REG_LOCK:
        t0 = _ASKING.get(eid)
        if t0 is not None and (now - t0) < _ASK_STALE:
            return False
        _ASKING[eid] = now
        return True


def _revising_busy(eid):
    """这一篇是不是**正在**批改中（同一时刻只许一条批改流）。

    ⚠️ 两个标签页各点一次「批改」，两边各拿一份内存快照，最后整份写回 ——
       互相覆盖、谁的都不完整（第十六轮审查 #11）。登记超过 _REVISING_STALE
       就当那条流早死了（worker 被杀时生成器跑不到 finally），不挡用户重试。
    """
    t0 = _REVISING.get(eid)
    return t0 is not None and (time.time() - t0) < _REVISING_STALE

# 批改给上游的空闲超时（每读到一块算一次活动）。
# ⚠️ 原来是 600 秒 —— 上游卡住时用户干等 10 分钟，而 worker 超时才 300 秒，
#    等不到就被杀进程，连还原都跑不了。120 秒没动静就是真卡了，早报错早还原。
_REVISE_TIMEOUT = 120

# 思考（reasoning）档位 —— 实测思考占了单趟耗时的 60~80%（识别趟 14.1s/17.6s），
# 而且思考期间模型一个字不吐，界面死寂，用户体感就是「卡住/这么慢」。
# ⚠️ 实测「reasoning_effort=low」根本拦不住：一次批改思考了 88508 字、213.7 秒，
#    而关思考只要 15.4 秒、结论一模一样（同为 41 分三类文）→ 全部关思考。
_OCR_THINK = "off"
_GRADE_THINK = "off"
_ASK_THINK = "off"

# 改卷模式（设置页可选）：快速 = 全关思考（默认）；深度 = 批改趟开少量思考换批改深度。
# ⚠️ low 的思考量实测能失控到 88508 字 / 213.7 秒 —— 必须配 _THINK_CHAR_CAP 硬上限：
#    超限自动降级成「关思考」重跑一遍，绝不让用户对着「AI 思考中」干等几分钟。
_GRADE_THINK_DEEP = "low"
_THINK_CHAR_CAP = 3000


class _ThinkingRunaway(RuntimeError):
    """深度模式思考超过字数硬上限 —— 用来触发降级重跑，不是真的出错。"""


def require_result(result):
    """批改结果必须有分数，否则算失败。

    ⚠️ 少这一步就会「静默成功」：模型偶尔只回一个 `{}`（或 `{"score": null}`），
       parse_json 不抛异常，于是 status 被标成 done、result 却是空的 ——
       前端 `if (!result)` 直接把结果区清空，用户看到「点了批改，什么也没发生」，
       而且因为 status=done 连个红色错误提示都没有。
    """
    if not isinstance(result, dict) or result.get("score") is None:
        raise RuntimeError("AI 没返回完整的批改结果（缺分数），请再点一次「重新批改」")
    # ⚠️ 分数必须是数字：模型把 score 吐成字符串时 _local_scoring 会原样放过（它算不出
    #    base_score 就直接 return），前端 renderResult 又是 innerHTML 直插 —— 一个含
    #    HTML 的字符串就变成注入口（第二十六轮实锤）。这里直接判不合格，让它重跑。
    #    顺带把前端**未转义直插**的另外几个数值字段一起卡住（同一类洞，一起堵）。
    def _num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if not _num(result.get("score")):
        raise RuntimeError("AI 返回的分数不是数字，请再点一次「重新批改」")
    if result.get("base_score") is not None and not _num(result.get("base_score")):
        raise RuntimeError("AI 返回的档次分不是数字，请再点一次「重新批改」")
    if result.get("word_count") is not None and not _num(result.get("word_count")):
        raise RuntimeError("AI 返回的字数不是数字，请再点一次「重新批改」")
    for d in (result.get("deductions") or []):
        if isinstance(d, dict) and d.get("points") is not None and not _num(d.get("points")):
            raise RuntimeError("AI 返回的扣分不是数字，请再点一次「重新批改」")
    return result


# ---- 规则性分数本地精确计算（2026-09-25 第十五轮）--------------------
# 扣分规则完全机械（无标题 -2；不足 500 字每少 50 字 -1、上限 3），不需要模型发挥；
# 而 AI 报的字数和减法都不可信（实测 word_count 在 348/386 之间波动），
# 所以规则性分数一律本地算、本地复核，AI 只负责「定档」这个真正需要理解力的判断。

_LEVELS = ((55, 60, "一类文", "55~60 分"), (49, 54, "二类文", "49~54 分"),
           (42, 48, "三类文", "42~48 分"), (36, 41, "四类文", "36~41 分"),
           (0, 35, "五类文", "0~35 分"))


def _count_chars(text):
    """字数本地实测：非空白字符数（贴近方格数口径，含标点）。"""
    return len("".join((text or "").split()))


def _local_scoring(result, text=None, title=""):
    """把机械规则算准并覆盖 AI 的算术（准确性 + 让模型少算数）：
      · 无标题扣 2 分（title 为空）
      · 字数不足 500 字，每少 50 字扣 1 分（不满 50 字按 1 算），最多扣 3 分
      · score = base_score − 扣分和，钳制在 [0, 60]
      · level/level_range 一律由 base_score 落档推导，保证档位与分数互洽
    text 传实测正文（非空才覆盖 word_count）；title 传原始标题（空 = 没标题）。
    """
    if not isinstance(result, dict):
        return result
    raw_base = result.get("base_score")
    if raw_base is None:
        raw_base = result.get("score")      # 模型没给 base_score 时拿 score 兜底
    try:
        base = int(round(float(raw_base)))
    except (TypeError, ValueError):
        return result                        # 连分数都没有 → 交给 require_result 报错
    base = max(0, min(60, base))
    result["base_score"] = base
    for lo, hi, name, rng in _LEVELS:
        if lo <= base <= hi:
            result["level"] = name
            result["level_range"] = rng
            break

    # 四维度规范化：模型偶尔多输出一条重复维度（实测出现过
    # 「情境 / 内容 / 结构 / 内容 / 语言」5 条），前端按数组渲染就会重复显示。
    # 按名字去重（保留第一条），空名字的照旧保留，别把内容吃掉。
    if isinstance(result.get("dims"), list):
        seen, clean = set(), []
        for dd in result["dims"]:
            if not isinstance(dd, dict):
                continue
            nm = str(dd.get("name") or "").strip()
            if nm and nm in seen:
                continue
            if nm:
                seen.add(nm)
            clean.append(dd)
        result["dims"] = clean

    if text is not None and (text or "").strip():
        result["word_count"] = _count_chars(text)

    ded = []
    if not (title or "").strip():
        ded.append({"rule": "无标题", "points": 2, "note": "缺少作文标题"})
    wc = result.get("word_count")
    if isinstance(wc, int) and not isinstance(wc, bool) and wc < 500:
        pts = min(3, (500 - wc + 49) // 50)   # ceil((500-wc)/50)，上限 3
        ded.append({"rule": "字数不足 500 字", "points": pts,
                    "note": "实写 %d 字，少 %d 字，扣 %d 分" % (wc, 500 - wc, pts)})
    result["deductions"] = ded
    result["score"] = max(0, base - sum(d["points"] for d in ded))
    return result


def _read_image_datauri(fname):
    """图片文件 → data URI（供多模态接口）。找不到返回 None。"""
    if not fname:
        return None
    path = os.path.join(IMG_DIR, os.path.basename(fname))
    if not os.path.exists(path):
        return None
    ext = "png" if path.lower().endswith(".png") else "jpeg"
    with open(path, "rb") as f:
        return "data:image/%s;base64,%s" % (ext, base64.b64encode(f.read()).decode())


def _essay_images(doc):
    """统一取图片列表（兼容旧的单张 image 字段）"""
    if doc.get("images"):
        return list(doc["images"])
    if doc.get("image"):
        return [doc["image"]]
    return []


def _prompt_imgs(doc):
    """题目原图（`prompt_image`）→ 真正读得出来的文件名列表。

    ⚠️ 这张图以前**只存在、只在页面上展示，从来没进过任何一次模型调用** ——
       而新建页写的是「也可以只拍一张题目图，AI 会自己读」，等于空头支票
       （第三十二轮审查 #2）。批改时必须把它一起送过去，否则模型只能靠
       文字题目（常常是空的）审题。
    """
    f = doc.get("prompt_image")
    if not f:
        return []
    return [f] if os.path.exists(os.path.join(IMG_DIR, os.path.basename(f))) else []


def _prompt_text(doc, gimgs=None):
    """【考试作文题目】那段填什么字。

    - 有文字题目：照抄（有图就附一句「另有题目原图」）
    - 只有题目原图：明说「随附第一张图就是题目，请读图审题」——
      否则模型拿到（本次未提供考试题目）会直接跳过审题，
      「内容」维度的「切合题意」就成了瞎猜。
    """
    if gimgs is None:
        gimgs = _prompt_imgs(doc)
    t = (doc.get("prompt") or "").strip()
    if gimgs:
        return (t + "\n（另有题目原图随附，可与文字对照）" if t else
                "（文字题目没填；**随附的第一张图就是考试题目原图**，"
                "请先读图审题，再按评分标准批改）")
    return t or "（本次未提供考试题目）"


OCR_PROMPT = """这是学生作文纸的照片，可能有多张。请**按图片顺序**连成一篇完整作文抄下来。

只做这些处理，别的一律不要做：
- 按原文分段，段落用 \\n 分隔
- **保持作者原话**：不润色、不改写、不补写、不删减
- 识别不出来或残缺的字，用 □ 表示，**不要猜**
- 学生的错别字、病句**原样保留**，不要纠正（那是阅卷要评的）
- 如果原文有标题，单独放进 title；没有标题就给空字符串 ""

只输出一个 JSON 对象，不要任何解释：
{"title": "作文标题，没有就给空字符串", "text": "作文正文全文"}
"""


# ---------------------------------------------------------------- 继续追问（答疑）

# 追问的系统提示 = 老师人设 + **这篇作文的全部上下文**。
# ⚠️ 上下文必须真的带上（题目/正文/批改结论）：模型看不到页面，不给正文它只能泛泛而谈。
#    上一次踩过的坑就是「提示词定义好了却没传进请求」，打桩测试还抓不到。
QA_SYSTEM_HEAD = """你是刚批改完这篇作文的语文老师，现在学生（或家长）就这篇作文继续追问，你来答疑。

回答要求：
- 直接回答，别复述题目、别客套、别说「好的，我来帮你分析」这类话；
- 先说结论，再说理由。能引原文就直接引原句，讲清「这句为什么不好、怎么改」；
- 像老师坐在旁边讲话，口语化一点；不要小标题堆砌、不要「首先/其次/最后」的八股；
- **只输出纯文本**（可以用换行分段）；不要输出 JSON，也不要 markdown 标记（#、*、`）；
- 拿不准的地方直说「这处我也不敢断言」，绝不编造评分标准或分数；
- 默认控制在 200 字以内，学生明确要求详细展开时才写长。"""


def build_qa_prompt(doc, question, history):
    """拼一次追问的 (system, prompt)。

    system：老师人设 + 题目 + 标题 + 学生原文 + 批改结论（含四维度/逐段批注/标红句/修改稿）
    prompt：之前的追问记录 + 本次追问
    ⚠️ 别用 .format() 拼正文（正文里可能有花括号），这里全程用字符串拼接。
    """
    r = doc.get("result") or {}
    parts = ["\n\n================ 这篇作文 ================",
             "【考试作文题目】" + ((doc.get("prompt") or "").strip()
                                    or ("（题目是图片，随附的图就是原题）"
                                        if _prompt_imgs(doc) else "（本次未提供题目）")),
             "【作文标题】" + ((doc.get("title") or "").strip() or "（未提供）"),
             "【学生原文】\n" + (doc.get("text") or "")]

    if r:
        rp = ["【你已经给的批改结论】",
              "最终分 %s / 60（%s %s；档次分 %s；扣分明细 %s）" % (
                  r.get("score"), r.get("level") or "", r.get("level_range") or "",
                  r.get("base_score"), json.dumps(r.get("deductions") or [], ensure_ascii=False)),
              "总评：" + str(r.get("summary") or ""),
              "是否扣题：" + str(r.get("on_topic") or ""),
              "四维度等第：" + json.dumps(r.get("dims") or [], ensure_ascii=False),
              "逐段批注：" + json.dumps(r.get("comments") or [], ensure_ascii=False),
              "提分建议：" + json.dumps(r.get("suggestions") or [], ensure_ascii=False)]
        for a in (doc.get("annotations") or []):
            rp.append("标红句：「%s」问题：%s；建议：%s"
                      % (a.get("quote") or "", a.get("issue") or "", a.get("suggest") or ""))
        if (r.get("revised") or "").strip():
            rp.append("【你给的修改稿】\n" + str(r["revised"]))
        parts.append("\n".join(rp))
    else:
        parts.append("【批改结论】这篇还没有批改结论 —— 学生是先来问问题的。")
    system = QA_SYSTEM_HEAD + "\n" + "\n".join(parts)

    qs = ["【之前的追问记录】"]
    if history:
        for it in history:
            qs.append("学生：" + (it.get("q") or ""))
            qs.append("你：" + (it.get("a") or "（上次没答完）"))
    else:
        qs.append("（这是第一次追问）")
    qs.append("【本次追问】" + question)
    return system, "\n".join(qs)


def _find_json_object(s):
    """字符串感知地找最外层 {...}（忽略字符串里的花括号）"""
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def _repair_json(s):
    """修 LLM 常见的 JSON 瑕疵（只在 json.loads 失败后才调用）"""
    out = []
    in_str = False
    esc = False
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if esc:
            # ⚠️ 非法转义（\U \_ \a …、\u 非 4 位 hex）→ 把反斜杠**再转义**，
            #    JSON 就合法了，解出来是「反斜杠 + 原字符」原样保留 —— 与
            #    stream_json._decode_string_literal 同语义。少了这步，模型在
            #    comments/highlights 等**嵌套**值里写个 C:\Users 就让整条批改
            #    「解析不了」直接失败，重试还一样挂（第二十七轮探针实锤）。
            # ⚠️ 上面 `if c == "\\" and in_str` 已经把原反斜杠 append 过了，
            #    这里只需**再补一个**反斜杠（合计 \\ = 合法转义），别补成三个。
            if c not in '"\\/bfnrtu':
                out.append("\\")
                out.append(c); esc = False; i += 1; continue
            if c == "u":
                hex4 = s[i + 1:i + 5]
                if len(hex4) == 4 and all(h in "0123456789abcdefABCDEF" for h in hex4):
                    out.append("u"); esc = False; i += 1; continue
                out.append("\\u"); esc = False; i += 1; continue
            out.append(c); esc = False; i += 1; continue
        if c == "\\" and in_str:
            out.append(c); esc = True; i += 1; continue
        if c == '"':
            if not in_str:
                in_str = True; out.append(c); i += 1; continue
            # 字符串里的裸引号：看后面是不是真的结束符
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j >= n or s[j] in ":,}]":
                in_str = False; out.append(c); i += 1; continue
            out.append('\\"'); i += 1; continue     # 转义掉裸引号
        if in_str:
            # 字符串里的裸控制字符（换行/制表符）→ 转义
            if c == "\n":
                out.append("\\n"); i += 1; continue
            if c == "\r":
                out.append("\\r"); i += 1; continue
            if c == "\t":
                out.append("\\t"); i += 1; continue
            out.append(c); i += 1; continue
        # 字符串外
        if c == "/" and i + 1 < n and s[i + 1] == "/":       # 行注释
            while i < n and s[i] != "\n":
                i += 1
            continue
        out.append(c); i += 1

    s2 = "".join(out)
    # 尾随逗号
    import re as _re
    s2 = _re.sub(r",(\s*[}\]])", r"\1", s2)
    return s2


def parse_json(content):
    """从模型输出里抠出 JSON 对象。容忍 markdown 围栏、前后杂文、
    字符串里的裸引号/裸换行、尾随逗号等常见瑕疵。"""
    content = (content or "").strip()
    if content.startswith("```"):
        parts = content.split("```")
        # ```json\n{...}\n``` → parts = ['', 'json\n{...}\n', '']
        if len(parts) >= 2:
            content = parts[1]
            if content[:4].lower() == "json":
                content = content[4:]

    blob = _find_json_object(content)
    if blob is None:
        raise RuntimeError("模型没有返回 JSON：" + (content or "")[:200])

    try:
        return json.loads(blob)
    except Exception as e1:
        try:
            return json.loads(_repair_json(blob))
        except Exception as e2:
            raise RuntimeError("模型返回的 JSON 解析不了（%s；修过仍失败：%s）原文开头：%r"
                               % (e1, e2, blob[:200]))


# ---------------------------------------------------------------- 路由

def _b64_bytes(b64):
    """data:...;base64,xxx 或裸 base64 → bytes。"""
    if not b64:
        return b""
    if "," in b64 and b64[:40].lower().startswith("data:"):
        b64 = b64.partition(",")[2]
    try:
        return base64.b64decode(b64, validate=False)
    except Exception:
        return b""


def _save_b64_image(b64, stem):
    """data:image/...;base64,xxx → 落盘，返回文件名；空/非法返回 ''"""
    if not b64 or not b64.startswith("data:image"):
        return ""
    header, _, payload = b64.partition(",")
    ext = "png" if "png" in header else "jpg"
    fname = stem + "." + ext
    try:
        raw = base64.b64decode(payload)
    except Exception:
        return ""                # 坏 base64 → 当「不支持的图」，不许炸成 500
    with open(os.path.join(IMG_DIR, fname), "wb") as f:
        f.write(raw)
    return fname


# ---------------------------------------------------------------- 访问口令（页面内登录）
# 口令来自 config.json 的 password_hash（setup.sh 首次运行时写入），
# 或环境变量 ESSAY_PASSWORD（明文，优先级更高）。两者都没有 = 完全开放。
# 登录页直接嵌在 index.html 里（全屏遮罩），POST /api/login 校验后写 session。
# 放行 /api/health（监控用）。

# 登录速率限制：连续失败 5 次 → 锁 30 秒（内存计数，不阻塞线程）
_LOGIN_FAILS = {}          # key=客户端IP → {"n": 连续失败次数, "until": 解锁时间戳}
_LOGIN_LOCK = threading.Lock()
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SECS = 30


def _login_rate_ok(ip):
    """检查该 IP 是否被锁。"""
    with _LOGIN_LOCK:
        rec = _LOGIN_FAILS.get(ip)
        if rec and time.time() < rec.get("until", 0):
            return False
        return True


def _login_fail(ip):
    """记录一次失败；达到阈值则上锁。"""
    with _LOGIN_LOCK:
        rec = _LOGIN_FAILS.setdefault(ip, {"n": 0, "until": 0})
        rec["n"] += 1
        if rec["n"] >= _LOGIN_MAX_FAILS:
            rec["until"] = time.time() + _LOGIN_LOCK_SECS
            rec["n"] = 0                     # 锁过之后重新计数


def _login_ok_reset(ip):
    """登录成功，清掉失败计数。"""
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(ip, None)

def _hash_password(pw, salt=None):
    """PBKDF2-SHA256 口令哈希，格式 `sha256:<盐hex>.<派生键hex>`。

    39 万轮迭代 —— 就算 config.json 被拖走，离线爆破也要费点功夫。
    """
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 390000, dklen=32)
    return "sha256:%s.%s" % (salt.hex(), dk.hex())


def _verify_password(pw, stored):
    """校验口令。**格式坏就返回 False（fail-closed）** —— 绝不因为读不出哈希而放行。"""
    if not stored or not stored.startswith("sha256:"):
        return False
    try:
        salt_hex, dk_hex = stored[7:].split(".", 1)
        salt = bytes.fromhex(salt_hex)
        want = bytes.fromhex(dk_hex)
    except Exception:
        return False
    got = hashlib.pbkdf2_hmac("sha256", (pw or "").encode("utf-8"),
                              salt, 390000, dklen=32)
    return hmac.compare_digest(got, want)


# 环境变量口令的哈希缓存 —— PBKDF2 一轮约 100ms，
# 而 _password_hash() 在**每个请求**上都会被 _auth_required() 调用，
# 不缓存的话整站会慢到没法用。
_ENV_HASH_CACHE = {}


def _password_hash():
    """当前生效的口令哈希；空串 = 还没设口令（**网站整体锁定**，提示去终端 setup.sh）。

    两个来源，优先级从高到低：
      1. 环境变量 `ESSAY_PASSWORD`（明文，适合临时改口令 / 容器部署）
      2. `config.json` 的 `password_hash`（setup.sh 首次运行时写入）
    """
    env = (os.environ.get("ESSAY_PASSWORD") or "").strip()
    if env:
        if "h" not in _ENV_HASH_CACHE:
            _ENV_HASH_CACHE["h"] = _hash_password(env)
        return _ENV_HASH_CACHE["h"]
    return (load_config().get("password_hash") or "").strip()


def _setup_needed():
    """还没设口令 —— 站点锁死，页面提示去终端跑 setup.sh。"""
    return not _password_hash()


def _auth_required():
    """是否需要登录：设了口令且 session 未通过。"""
    if not _password_hash():
        return False                       # 没口令不是「放行」，是「锁站」（见 _require_auth）
    return not session.get("essay_auth")


def _check_password(pw):
    """校验口令。"""
    h = _password_hash()
    if not h:
        return False                       # 没口令时不许通过登录进来（锁站）
    return _verify_password(pw, h)


@app.route("/api/login", methods=["POST"])
def api_login():
    if _setup_needed():
        return jsonify({"ok": False, "error": "no_password",
                        "message": "还没设置访问口令，请在终端运行 setup.sh 设置"}), 403
    ip = request.remote_addr or "unknown"
    if not _login_rate_ok(ip):
        return jsonify({"ok": False, "error": "尝试次数过多，请 %d 秒后再试"
                        % _LOGIN_LOCK_SECS}), 429
    data = request.get_json(force=True, silent=True) or {}
    pw = data.get("password") or ""
    if _check_password(pw):
        _login_ok_reset(ip)
        session["essay_auth"] = True
        session.permanent = True
        return jsonify({"ok": True})
    _login_fail(ip)
    return jsonify({"ok": False, "error": "口令不正确"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})
@app.before_request
def _require_auth():
    if request.path == "/api/health":
        return None                            # 健康检查放行
    if request.path == "/api/login" and request.method == "POST":
        return None                            # 登录接口本身放行
    # 静态资源必须放行：登录遮罩页需要 CSS/JS 才能渲染
    if request.path.startswith("/static/"):
        return None
    if _setup_needed():
        # 还没设口令 = 整站锁定：API 给 403 让前端弹「去终端设置口令」提示；
        # 页面请求照常渲染（index.html 里 needs_setup 遮罩接管）
        if request.path.startswith("/api/"):
            return Response(json.dumps({"error": "no_password", "message":
                            "还没设置访问口令，请在终端运行 setup.sh 设置"},
                            ensure_ascii=False),
                            status=403, mimetype="application/json")
        return None
    if not _auth_required():
        return None                            # 已登录或 session 通过
    # 已登录（session）放行；API 请求返回 401 让前端跳登录页
    if request.path.startswith("/api/"):
        return Response(json.dumps({"error": "未登录"}, ensure_ascii=False),
                        status=401, mimetype="application/json")
    # 页面请求：直接渲染（登录遮罩由前端控制，index.html 自带）
    return None


@app.route("/")
def index():
    return render_template("index.html", needs_auth=_auth_required(),
                           needs_setup=_setup_needed())


@app.route("/api/essays", methods=["GET"])
def api_list():
    return jsonify(list_essays())


@app.route("/api/essays", methods=["POST"])
def api_create():
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    prompt_image_b64 = data.get("prompt_image") or ""
    category = _norm_category(data.get("category"))   # 文体类别，空 = 让 AI 自己判断

    # 作文纸：多张。兼容旧的单张 image 字段
    images_b64 = data.get("images") or []
    if isinstance(images_b64, str):
        images_b64 = [images_b64] if images_b64 else []
    if not images_b64 and data.get("image"):
        images_b64 = [data["image"]]

    # 作文文档：现代 .docx / 老 .doc / 记事本 .txt —— 正文直接从文档里抽，不用拍照识别
    docx_name = os.path.basename((data.get("docx_name") or "").strip())[:120]
    docx_text = ""
    if data.get("docx"):
        raw = _b64_bytes(data["docx"])
        if not raw:
            return jsonify({"error": "文档文件读取失败，请重新选一次"}), 400
        try:
            docx_text = doc_text_mod.extract_any(raw, docx_name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if not images_b64 and not docx_text:
        return jsonify({"error": "请上传作文纸照片，或一个作文文档（.docx / .doc / .txt）"}), 400

    eid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    doc = {
        "id": eid,
        "title": title,
        "prompt": prompt,
        "category": category,
        # 正文：Word 文档直接抽出来；没给文档就等 AI 从照片识别
        "text": docx_text,
        "text_edited": bool(docx_text),   # Word 来的正文已经是「确定的字」，别再让 AI 覆盖
        "docx_name": docx_name if docx_text else "",
        "images": [],
        "prompt_image": "",
        "status": "draft",
        "created_at": time.time(),
        "result": None,
    }

    for i, b64 in enumerate(images_b64):
        fn = _save_b64_image(b64, "%s.p%d" % (eid, i))
        if fn:
            doc["images"].append(fn)
    # ⚠️ 只有「没给 Word 文档」时才强制要有作文纸照片：
    #    传了 .docx 就已经有正文了，照片是可选的（比如同时想留一份手写原稿）
    if not doc["images"] and not docx_text:
        return jsonify({"error": "图片格式不支持"}), 400

    # 题目图单独存，别和正文图混在一起
    doc["prompt_image"] = _save_b64_image(prompt_image_b64, eid + ".q")

    save_essay(doc)
    return jsonify({"id": eid, "pages": len(doc["images"]),
                    "from_docx": bool(docx_text), "chars": len(docx_text)})


@app.route("/api/essays/<eid>", methods=["GET"])
def api_get(eid):
    doc = load_essay(eid)
    if not doc:
        return jsonify({"error": "找不到"}), 404
    # ⚠️ 陈旧「批改中」自愈：进程被杀会留下 status=revising，但内存登记表里没有它
    #    （或登记早就过期）→ 那条流已经死了，顺手复位，别让这篇作文永远点不动。
    if doc.get("status") == "revising":
        t0 = _REVISING.get(eid)
        if t0 is None or (time.time() - t0) > _REVISING_STALE:
            doc["status"] = "done" if doc.get("result") else "draft"
            save_essay(doc)
    doc["has_document"] = bool(doc.get("docx_name"))   # 与列表接口同名字段（正文来自 .docx）
    doc["has_docx"] = bool(doc.get("docx_name"))       # 与列表接口同名字段
    return jsonify(doc)


@app.route("/api/essays/<eid>", methods=["DELETE"])
def api_delete(eid):
    doc = load_essay(eid)
    if not doc:
        return jsonify({"error": "找不到"}), 404
    for fname in _essay_images(doc) + [doc.get("prompt_image"), doc.get("image")]:
        if fname:
            img = os.path.join(IMG_DIR, fname)
            if os.path.exists(img):
                os.remove(img)
    os.remove(essay_path(eid))
    _DELETED[eid] = time.time()      # 立墓碑：在跑的批改/追问流不许把它写回（诈尸）
    return jsonify({"ok": True})


@app.route("/api/essays/<eid>", methods=["PUT"])
def api_update(eid):
    """更新原文（OCR 后人工修正）"""
    doc = load_essay(eid)
    if not doc:
        return jsonify({"error": "找不到"}), 404
    data = request.get_json(force=True, silent=True) or {}
    text_changed = False
    if "text" in data:
        new_text = data["text"] or ""
        if new_text != (doc.get("text") or ""):
            text_changed = True
        doc["text"] = new_text
        # 手改过正文 → 重新批改时不再用 AI 识别覆盖
        doc["text_edited"] = True
    if "title" in data:
        # 归一化：None/数字/带空白都不许原样落盘（类型脏了报告和列表都要兜）
        doc["title"] = str(data["title"] or "").strip()
    if "prompt" in data:
        doc["prompt"] = str(data["prompt"] or "").strip()
    if "category" in data:
        # 类别同标题语义：改了不作废批改结果（要按新文体重评就再点一次批改）
        try:
            doc["category"] = _norm_category(data["category"], strict=True)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    # ⚠️ 只有**正文真变了**才作废批改结果：正文改了，旧结果就是对不上号的。
    #    改标题/题目（或正文没动只是点保存）把结果一起清掉 = 白丢一次花钱的批改
    #    —— 第十六轮审查 #3。
    if text_changed:
        doc["status"] = "draft"
        doc["result"] = None
    save_essay(doc)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 批改报告（TXT 下载）

def _wrap_line(text, width=48, indent="  "):
    """长段落在 TXT 里折行（中文按显示宽度≈2 算），手机/记事本里才好读。"""
    out, line, w = [], "", 0
    for ch in (text or ""):
        if ch == "\n":
            out.append(indent + line)
            line, w = "", 0
            continue
        line += ch
        w += 2 if ord(ch) > 0x2E7F else 1
        if w >= width * 2:
            out.append(indent + line)
            line, w = "", 0
    if line:
        out.append(indent + line)
    return "\n".join(out) if out else indent + "（无）"


def build_report_txt(doc):
    """把一篇作文的批改结果渲染成 TXT（基本情况 + 详细修改意见 + 修改后全文）。

    分数/字数/扣分一律用 result 里的本地复算值（_local_scoring 算过的），
    标红句优先用锚定好的 annotations（锚不上退回 highlights —— 与页面显示一致）。
    """
    r = doc.get("result") or {}
    anns = doc.get("annotations") or []
    text = doc.get("text") or ""
    wc = r.get("word_count") or _count_chars(text)
    base = r.get("base_score")
    final = r.get("score")
    level = r.get("level") or ""
    rng = r.get("level_range") or ""
    deds = r.get("deductions") or []

    imgs = _essay_images(doc)      # 走同一套取图逻辑（兼容老单张 image 字段）
    if doc.get("docx_name"):
        src = "文档《%s》" % doc["docx_name"]
        if imgs:
            src += "（另附 %d 张照片）" % len(imgs)
    elif imgs:
        src = "%d 张作文纸照片" % len(imgs)
    else:
        src = "文字"

    def s(v, dash="（无）"):
        v = "" if v is None else str(v).strip()
        return v if v else dash

    LINE = "=" * 52
    THIN = "-" * 52
    when = doc.get("revised_at") or doc.get("created_at") or time.time()
    L = [LINE, "            中考作文批改报告", LINE]

    L += ["", "【一、基本情况】",
          "  作文标题：%s" % s(doc.get("title"), "（没有标题）"),
          "  文章类别：%s" % s(doc.get("category"), "（未指定，AI 自行判断）"),
          "  考试题目：%s" % ((doc.get("prompt") or "").strip()
                              or ("（题目以图片形式提供，见题目原图）"
                                  if doc.get("prompt_image") else "（本次未提供）")),
          "  正文字数：%d 字%s" % (wc, "（要求 ≥500 字，达标）" if wc >= 500
                                   else "（要求 ≥500 字，缺 %d 字）" % (500 - wc)),
          "  原稿来源：%s" % src,
          "  批改时间：%s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(when)),
          "  作文编号：%s" % s(doc.get("id"), "—")]

    L += ["", "【二、成绩】",
          "  最终得分：%s / 60%s" % (final, "（%s %s）" % (level, rng) if level else ""),
          "  档次分：%s" % s(base)]
    if deds:
        L.append("  扣分明细：")
        for d in deds:
            L.append("    · %s −%s 分：%s" % (s(d.get("rule")), d.get("points"),
                                             s(d.get("note"), "")))
    else:
        L.append("  扣分明细：无")
    if r.get("band_reason"):
        L += ["  定档依据：", _wrap_line(r["band_reason"])]

    L += ["", "【三、四维度等第】"]
    for d in (r.get("dims") or []):
        L.append("  · %s：%s" % (s(d.get("name")), s(d.get("level"))))
        L.append(_wrap_line(d.get("comment"), indent="      "))

    L += ["", "【四、是否扣题】", _wrap_line(r.get("on_topic")),
          "", "【五、总评】", _wrap_line(r.get("summary"))]

    L += ["", "【六、逐段批注（详细修改意见）】"]
    if r.get("comments"):
        for c in r["comments"]:
            L.append("")
            L.append("  第 %s 段%s" % (s(c.get("para"), "?"),
                                     "　「%s」" % c["quote"] if c.get("quote") else ""))
            if c.get("good"):
                L.append("    【好】" + _wrap_line(c["good"], indent="      ").strip())
            if c.get("improve"):
                L.append("    【改】" + _wrap_line(c["improve"], indent="      ").strip())
            if c.get("risk"):
                L.append("    【注意】" + _wrap_line(c["risk"], indent="      ").strip())
    else:
        L.append("  （无）")

    L += ["", "【七、最值得改的句子】"]
    srcs = [{"quote": a.get("quote"), "issue": a.get("issue"), "suggest": a.get("suggest")}
            for a in anns] if anns else (r.get("highlights") or [])
    if srcs:
        for i, h in enumerate(srcs, 1):
            L.append("")
            L.append("  %d. 「%s」" % (i, s(h.get("quote"), "")))
            if h.get("issue"):
                L.append("     问题：" + _wrap_line(h["issue"], indent="       ").strip())
            if h.get("suggest"):
                L.append("     改法：" + _wrap_line(h["suggest"], indent="       ").strip())
    else:
        L.append("  （无）")

    L += ["", "【八、提分建议】"]
    sugs = r.get("suggestions") or []
    for i, sug in enumerate(sugs, 1):
        L.append("  %d. %s" % (i, sug))
    if not sugs:
        L.append("  （无）")

    if (r.get("revised") or "").strip():
        L += ["", "【九、修改后的全文】", THIN]
        L += ["  " + p for p in r["revised"].split("\n")]
        L.append(THIN)

    L += ["", LINE,
          "  报告由「作文批改」网页生成 —— 分数规则（字数/扣分/档位）为系统本地复算核对。",
          LINE]
    return "\n".join(L) + "\n"


@app.route("/api/essays/<eid>/report.txt", methods=["GET"])
def api_report(eid):
    """一键下载批改报告（TXT）。没批改过就没有报告 —— 返回 404 纯文本。"""
    doc = load_essay(eid)
    if not doc:
        return Response("找不到这篇作文\n", status=404, mimetype="text/plain")
    if not doc.get("result"):
        return Response("这篇作文还没批改，没有可下载的报告。\n先点「让 AI 老师批改」。\n",
                        status=404, mimetype="text/plain")
    txt = build_report_txt(doc)
    # 文件名：中文标题走 RFC 5987（filename*=UTF-8''…），再给个 ASCII 兜底
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "", (doc.get("title") or "").strip())[:30] or eid
    fname = "作文批改报告-%s.txt" % name
    ascii_f = "essay-report-%s.txt" % eid
    resp = Response(txt, mimetype="text/plain")
    resp.headers["Content-Disposition"] = (
        "attachment; filename=\"%s\"; filename*=UTF-8''%s"
        % (ascii_f, urllib.parse.quote(fname)))
    return resp


@app.route("/api/essays/<eid>/revise", methods=["POST"])
def api_revise(eid):
    doc = load_essay(eid)
    if not doc:
        return jsonify({"error": "找不到"}), 404

    imgs = _essay_images(doc)
    if not imgs and not (doc.get("text") or "").strip():
        return jsonify({"error": "没有作文纸照片也没有正文，没法批改"}), 400

    cfg = load_config()
    # 旧端点也登记：① 与流式端点共用并发互斥 ② 流式那边能看到「确实在跑」
    if not _revise_register(eid):
        return jsonify({"error": "这篇作文正在批改中，等它跑完再点"}), 409
    doc["status"] = "revising"
    save_essay(doc)

    if not cfg.get("api_key"):
        doc["status"] = "pending"
        save_essay(doc)
        _REVISING.pop(eid, None)
        return jsonify({
            "ok": False,
            "mode": "queue",
            "message": "还没配置 AI 接口。已存为「待批改」，可以直接找 AI 助手批改。",
        }), 202

    try:
        # ---- 阶段 1：AI 看照片识别正文 ----
        # 用户手改过正文（text_edited）就不再覆盖，直接用他改好的
        if imgs and not doc.get("text_edited"):
            ocr = call_llm(
                cfg,
                OCR_PROMPT,          # ⚠️ 必须是完整的抄写指令，别再传一句空指代的话
                images=imgs,
                system="你是 OCR 助手，把照片上的文字原样抄下来，只输出合法 JSON。",
                think=_OCR_THINK,
            )
            if isinstance(ocr, dict):
                new_text = (ocr.get("text") or "").strip()
                if new_text:
                    doc["text"] = new_text
                if not (doc.get("title") or "").strip() and (ocr.get("title") or "").strip():
                    doc["title"] = ocr["title"].strip()
                # 记住这组照片认过了 —— 否则「旧端点批完再来流式批」会重认一遍照片，
                # 白花一趟视觉调用（第十六轮审查 #8）
                doc["ocr_images"] = list(imgs)
                _merge_user_changes(doc)     # 识别期间用户改过正文的话，他的算数
                save_essay(doc)

        text = (doc.get("text") or "").strip()
        if not text:
            raise RuntimeError("AI 没能从照片里识别出文字，请换清晰一点的照片再试")

        # ---- 阶段 2：按评分标准批改 ----
        # 题目原图一起送过去（`_prompt_imgs`）；万一当前模型不认图（非视觉模型），
        # 退回纯文本重跑一次，别让「多送一张图」把批改整个搞挂。
        _gimgs, _gtext = _prompt_imgs(doc), _prompt_text(doc)

        def _grade(_imgs):
            return call_llm(
                cfg,
                PROMPT.format(
                    prompt=_gtext,
                    title=doc.get("title") or "（未提供）",
                    genre=_genre_text(doc),
                    text=text,
                    wc=_count_chars(text),
                    strictness=_strictness_text(cfg),     # 严格/标准（默认严格）
                ),
                images=_imgs or None,
                think=(_GRADE_THINK_DEEP if cfg.get("grade_mode") == "deep" else _GRADE_THINK),
                # 旧端点（冒烟用）没接思考硬上限，深度模式失控只能靠 socket 超时兜底；
                # 真正的用户路径 revise_stream 有 _THINK_CHAR_CAP。
            )

        try:
            result = _grade(_gimgs)
        except Exception:
            if not _gimgs:
                raise
            _gimgs = []
            result = _grade([])
        result = require_result(_local_scoring(result, text, doc.get("title") or ""))
    except Exception as e:
        msg = _err_msg(e)
        doc["status"] = "error"
        doc["error"] = msg
        # ⚠️ 出错落盘也要先合并用户改动（第二十九轮遗留 #2）：识别/批改这几秒里
        #    用户可能改了正文或追问过，整份写回会把它们吞掉。成功路径一直有这一步，
        #    只有出错路径漏了 —— 正好是最容易撞上的路径（模型抽风、断网）。
        _merge_user_changes(doc)
        save_essay(doc)
        return jsonify({"error": msg}), 500
    finally:
        _REVISING.pop(eid, None)      # 旧端点跑完就摘登记，别占着并发位

    doc["result"] = result
    # ⚠️ 整份写回前先合并「批改期间用户在盘上改的东西」（追问 / 手改正文），
    #    否则这一句就把它们覆盖没了 —— 第十六轮审查 #1
    _merge_user_changes(doc)
    # 标红批注：把 AI 挑出的「可改进句」锚定到正文偏移（对不上的自动丢弃）
    doc["annotations"] = _anchor_annotations(
        doc.get("text") or "", (result or {}).get("highlights")
    )
    doc["status"] = "done"
    doc["revised_at"] = time.time()
    doc["error"] = ""
    save_essay(doc)
    return jsonify({
        "ok": True,
        "result": result,
        "text": doc.get("text", ""),
        "annotations": doc["annotations"],
    })


def _sse(event, data):
    """打一行 SSE。中文原样发（json 不做 ASCII 转义），前端才好直接显示。

    ⚠️ 先清洗孤立代理：它对 json.dumps 无害，但写响应时 utf-8 编码会抛
       UnicodeEncodeError —— 而且是在 yield 里抛，整条流当场断。
    """
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(_clean_surrogates(data),
                                                         ensure_ascii=False))


def _think_body(level, cfg=None):
    """思考（reasoning）控制参数 —— **按服务商方言分流**，两种接口说的不是一套话：

    DeepSeek（V4.x，2026-09 官方「思考模式」文档）用 `thinking.type` 开关 +
    `reasoning_effort` 强度（low/high/max）：
    - "off"：`thinking={"type":"disabled"}` → 非思考（快速）模式，最快最便宜
    - "low"：`thinking={"type":"enabled"}` + `reasoning_effort=low`
    ⚠️ DeepSeek **默认开思考、强度 high** —— 不发参数就是让它慢慢想！
    以前发的 `chat_template_kwargs` 它根本不认识，「快速」档等于没关思考。

    mimo 等 vLLM/SGLang 系接口用另一套（实测 2026-09-25，mimo-v2.6-flash）：
    - "off"：`chat_template_kwargs.enable_thinking=false` → 思考 0 字，识别趟 17.6s→4.5s
    - "low"：`reasoning_effort=low` → 思考量大减但仍保留
    ⚠️ 这套 `reasoning_effort="minimal"` 会被 400 拒绝，别用（DeepSeek 倒是认，映射到 low）。

    - 其他/None：不传参数（模型自由发挥，思考可能拖几分钟）
    """
    if not level:
        return {}
    cfg = cfg or {}
    base = (cfg.get("base_url") or "").lower()
    model = (cfg.get("model") or "").lower()
    if "deepseek" in base or model.startswith("deepseek"):
        if level == "off":
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    if level == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"reasoning_effort": "low"}


class _Thinking(str):
    """iter_llm_tokens 对「思考内容」的包装：调用方 `isinstance(piece, _Thinking)`
    就能区分正文与思考 —— 思考期间也不断有产出，界面可以把「AI 在想」推给用户，
    不再几十秒死寂一片（体感「卡住/这么慢」的很大一部分就是这段静默期）。"""


def iter_llm_tokens(cfg, prompt, images=None, system=None, should_stop=None, timeout=None,
                    think=None):
    """调 OpenAI 兼容接口（stream=True），**逐个吐出 token**。

    这是个生成器，所以外层能直接 `for piece in ...` 边收边 yield ——
    别改回「回调」那套，回调里 yield 不了，会把生成器写乱。

    timeout：socket 空闲超时（读下一块最多等多久），默认 600 秒。
             追问传 _ASK_TIMEOUT，卡住时早点报错。
    think：  "off" 关思考 / "low" 少思考 / None 不管。思考内容以 _Thinking 包装
             一起从生成器里吐出（不含正文），别当正文拼进结果。
    """
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    timeout = 600 if timeout is None else timeout

    if images:
        content = []
        for fname in images:
            data = _read_image_datauri(fname)
            if data:
                content.append({"type": "image_url", "image_url": {"url": data}})
        content.append({"type": "text", "text": prompt})
    else:
        content = prompt

    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system or "你是一位资深语文阅卷老师，只输出严格合法的 JSON。"},
            {"role": "user", "content": content},
        ],
        "temperature": float(cfg.get("temperature", 0.3)),
        "stream": True,
    }
    body.update(_think_body(think, cfg))
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
        method="POST",
    )

    got_any = False
    _TLS.stream_break = ""      # 每趟调用先清掉上一次的断流痕迹
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if should_stop and should_stop():
                    raise _Cancelled()
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0].get("delta") or {}
                except Exception:
                    continue
                rz = delta.get("reasoning_content")
                if rz:
                    yield _Thinking(rz)   # 思考内容：调用方自己决定展示或丢弃
                piece = delta.get("content")
                if piece:
                    got_any = True
                    yield piece
    except _Cancelled:
        raise
    except Exception as e:
        # 流式中途断了别直接判死 —— 已经收到的还能用
        if not got_any:
            raise RuntimeError("调用失败：%s" % e)
        # ⚠️ 半截内容不一定拼得出合法 JSON。留个痕迹（线程局部，gthread 下不会串台），
        #    上层报错时就能说清是「网络中断」而不是让用户以为 AI 格式出错。
        _note_stream_break(e)




@app.route("/api/essays/<eid>/revise_stream", methods=["POST"])
def api_revise_stream(eid):
    """AI 批改，**边跑边把结果推给前端**（B 方案：批改过程实时可见）。

    事件：
      stage     阶段提示（「正在看照片抄正文…」/「正在按评分标准批改…」）
      text      识别出的正文（一抄完就推）
      field     结果的每一项（分数 / 四维度 / 扣分…）逐个冒出来
      ann       标红批注锚定好了
      done      全部完成
      error     出错
      cancelled 用户取消，已还原到批改前

    **取消不留脏数据**：开始前拍快照，用户点「取消」就整份还原到批改前。
    """
    doc = load_essay(eid)
    if not doc:
        return jsonify({"error": "找不到"}), 404

    imgs = _essay_images(doc)
    if not imgs and not (doc.get("text") or "").strip():
        return jsonify({"error": "没有作文纸照片也没有正文，没法批改"}), 400

    cfg = load_config()
    if not cfg.get("api_key"):
        doc["status"] = "pending"
        save_essay(doc)
        return jsonify({
            "ok": False, "mode": "queue",
            "message": "还没配置 AI 接口。已存为「待批改」，可以直接找 AI 助手批改。",
        }), 202

    before = json.loads(json.dumps(doc))      # 快照：取消/出错就还原它
    orig_status = before.get("status", "draft")
    if not _revise_register(eid):
        # 同一篇同时两条批改流 = 两份内存快照互相覆盖（第十六轮审查 #11）
        return jsonify({"error": "这篇作文正在批改中，等它跑完再点（或点取消）"}), 409
    _CANCEL.discard(eid)                      # 清掉上次可能残留的取消标志
    doc["status"] = "revising"
    save_essay(doc)

    def gen():
        # ⚠️ 必须有这行：OCR 阶段会重新拍快照（before = ...），
        #    没有 nonlocal 的话 before 会被当成 gen() 的局部变量，
        #    取消/出错路径读它就 UnboundLocalError —— 正好是唯一需要还原数据的那条路。
        nonlocal before
        finished = False       # 成功跑完
        restored = False       # 数据已经还原过（别再存一次）

        def should_stop():
            return eid in _CANCEL

        try:
            _t_start = time.time()
            # ---------- 阶段 1：AI 看照片抄正文 ----------
            # ⚠️ 照片没换、正文已在 → **不再重复 OCR**。识别是一整趟模型调用
            #    （十几秒 + 一次视觉计费），「重新批改」时白跑一趟是体感慢的主因。
            #    换了照片（文件名列表变了）才重认；用户手改过正文（text_edited）照旧不覆盖。
            _same_photos = list(imgs) == list(doc.get("ocr_images") or [])
            _need_ocr = bool(imgs) and not doc.get("text_edited") and (
                not (doc.get("text") or "").strip() or not _same_photos)
            if _need_ocr:
                yield _sse("stage", {"stage": "ocr", "msg": "正在看照片抄正文…"})
                buf, seen = [], set()
                for piece in iter_llm_tokens(
                        cfg, OCR_PROMPT, images=imgs,
                        system="你是 OCR 助手，把照片上的文字原样抄下来，只输出合法 JSON。",
                        should_stop=should_stop, timeout=_REVISE_TIMEOUT,
                        think=_OCR_THINK):
                    if isinstance(piece, _Thinking):
                        continue
                    buf.append(piece)
                    for k, v in extract_new_fields("".join(buf), seen,
                                                   fields=("text", "title")):
                        # 抄完就推，用户马上能看到正文
                        yield _sse("text", {"part": k, k: v})
                ocr = parse_json("".join(buf))
                if isinstance(ocr, dict):
                    doc["ocr_images"] = list(imgs)   # 记住这组照片认过了，重新批改别再认一遍
                    new_text = (ocr.get("text") or "").strip()
                    if new_text:
                        doc["text"] = new_text
                    if not (doc.get("title") or "").strip() and (ocr.get("title") or "").strip():
                        doc["title"] = ocr["title"].strip()
                    _merge_user_changes(doc)  # 识别期间用户改过正文/追问过，别覆盖
                    save_essay(doc)
                    # ⚠️ 重新拍快照：此刻 =「正文已抄好、还没批改」。
                    #    视觉识别是要花钱花时间的，取消**评分**不该把它一起冲掉；
                    #    真正要防的是「半截批改结果」，不是已识别好的正文。
                    before = json.loads(json.dumps(doc))
                    before["status"] = orig_status     # 别把 revising 状态一起存进去
                yield _sse("text", {"part": "all", "text": doc.get("text", ""),
                                    "title": doc.get("title", "")})

            text = (doc.get("text") or "").strip()
            if not text:
                raise RuntimeError("AI 没能从照片里识别出文字，请换清晰一点的照片再试")

            # ---------- 阶段 2：按评分标准批改，逐项推 ----------
            _t_ocr = time.time() - _t_start
            deep = (cfg.get("grade_mode") == "deep")   # 改卷模式：深度 = 少量思考
            yield _sse("stage", {"stage": "grade", "msg": "正在按评分标准批改…"})
            # 深度模式思考有硬上限（_THINK_CHAR_CAP）：超了就丢掉这批，降级成
            # 「关思考」重跑一遍 —— 快速模式本就关思考，模型不听话硬思考也只是丢弃超额。
            # 题目原图（_prompt_imgs）随批改一起送过去；当前模型要是不认图
            # （非视觉模型），退回纯文本重跑一次，别让一张图把批改整个搞挂。
            _gimgs, _gtext = _prompt_imgs(doc), _prompt_text(doc)
            _try, _img_dropped = 0, False
            while True:
                think_mode = _GRADE_THINK_DEEP if (deep and _try == 0) else _GRADE_THINK
                buf, seen = [], set()
                _th_n, _th_t = 0, 0.0     # 思考字数 / 上次推 think 事件的时刻（节流）
                try:
                    for piece in iter_llm_tokens(cfg, PROMPT.format(
                            prompt=_gtext,
                            title=doc.get("title") or "（未提供）",
                            genre=_genre_text(doc),
                            text=text,
                            wc=_count_chars(text),
                            strictness=_strictness_text(cfg),   # 严格/标准（默认严格）
                        ), images=_gimgs or None,
                            should_stop=should_stop, timeout=_REVISE_TIMEOUT,
                            think=think_mode):
                        if isinstance(piece, _Thinking):
                            # 思考期间也推事件：界面显示「AI 思考中…（N 字思路）」，
                            # 别让用户对着静止的界面干等。
                            _th_n += len(piece)
                            if _th_n > _THINK_CHAR_CAP:
                                if deep and _try == 0:
                                    raise _ThinkingRunaway()
                                continue          # 不重跑就丢弃超额思考，等正文
                            if time.time() - _th_t > 0.8:
                                _th_t = time.time()
                                yield _sse("think", {"n": _th_n})
                            continue
                        buf.append(piece)
                        for k, v in extract_new_fields("".join(buf), seen):
                            yield _sse("field", {"key": k, "value": v})
                    break                          # 正常跑完，不用重试
                except _Cancelled:
                    raise                          # 用户点了取消：交给外层还原，别在这重试
                except _ThinkingRunaway:
                    yield _sse("think", {"n": _th_n, "fallback": True})
                    _try += 1
                    continue                       # 降级重跑（关思考）
                except Exception:
                    if _gimgs and not _img_dropped and _try == 0:
                        _img_dropped = True
                        _gimgs = []
                        yield _sse("stage", {"stage": "grade",
                                             "msg": "题目原图读不了，改用文字题目批改…"})
                        continue                   # 退回纯文本再跑一次
                    raise

            result = require_result(_local_scoring(parse_json("".join(buf)),
                                                   text, doc.get("title") or ""))
            _t_grade = time.time() - _t_start - _t_ocr
            doc["result"] = result
            # ⚠️ 整份写回前先合并「批改期间用户在盘上改的东西」（追问 / 手改正文），
            #    否则这一句就把它们覆盖没了 —— 第十六轮审查 #1
            _merge_user_changes(doc)
            doc["annotations"] = _anchor_annotations(doc.get("text") or "",
                                                     (result or {}).get("highlights"))
            doc["status"] = "done"
            doc["revised_at"] = time.time()
            doc["error"] = ""
            save_essay(doc)
            finished = True
            try:
                with open(os.path.join(DATA_DIR, "timing.log"), "a", encoding="utf-8") as _f:
                    _f.write(json.dumps({"ts": time.time(), "eid": eid,
                                         "ocr_s": round(_t_ocr, 1),
                                         "grade_s": round(_t_grade, 1),
                                         "ocr_skipped": not _need_ocr},
                                        ensure_ascii=False) + "\n")
            except Exception:
                pass
            yield _sse("ann", {"annotations": doc["annotations"]})
            yield _sse("done", {"result": result, "text": doc.get("text", ""),
                                "annotations": doc["annotations"]})
        except _Cancelled:
            # ⚠️ 先还原数据，**再**发 cancelled ——
            #    生成器在 yield 处挂起时 finally 还没跑，
            #    前端一收到事件就去拉数据，会拉到没还原的半截状态。
            restore_essay(before)
            restored = True
            yield _sse("cancelled", {"msg": "已取消，改动没有保存"})
        except Exception as e:
            restore_essay(before)
            restored = True
            yield _sse("error", {"error": _err_msg(e)})
        finally:
            if not finished and not restored:
                # 客户端中途断开（GeneratorExit）→ 也得还原，绝不留半截结果
                restore_essay(before)
            _CANCEL.discard(eid)
            _REVISING.pop(eid, None)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             # ⚠️ 用 close 不用 keep-alive：SSE 流结束后浏览器若复用这条
                             #    连接发下一个请求，实测会卡住不回（半小时前那笔 GET 永远
                             #    不回来 → 界面卡在「批改中」，靠重试兜底）。关掉连接，
                             #    下一个请求必然新建 TCP，不再有复用竞态。
                             "Connection": "close"})


@app.route("/api/essays/<eid>/revise_cancel", methods=["POST"])
def api_revise_cancel(eid):
    """用户点「取消」→ 给正在跑的流式批改发停止信号。

    这里只打标志。真正的事由 revise_stream 那边完成：
    iter_llm_tokens 的 should_stop() 掐断 AI 调用，
    finally 把数据还原到批改前，再发 cancelled 事件。
    """
    _CANCEL.add(eid)
    return jsonify({"ok": True})


@app.route("/api/essays/<eid>/ask", methods=["POST"])
def api_ask(eid):
    """就这篇作文**继续追问**（答疑）—— 同样是 SSE，回答一个字一个字冒出来。

    事件：
      start      开始回答（回显问题）
      delta      回答的一个片段
      done       答完了（完整文本）
      cancelled  用户点了停止
      error      出错

    ⚠️ 和批改不同：追问**不改作文本身**，只往 doc["qa"] 里追加一条问答，
       所以不需要「快照 + 整份还原」那套。中断时把已经答出来的半截标上
       interrupted 存下来 —— 比整条丢掉更诚实（用户至少看得见答到哪儿了），
       但**一个字都没答出来**的就什么都不写，别留个空壳问题在页面上。
    """
    doc = load_essay(eid)
    if not doc:
        return jsonify({"error": "找不到"}), 404

    data = request.get_json(force=True, silent=True) or {}
    q = (data.get("q") or "").strip()
    if not q:
        return jsonify({"error": "问题不能为空"}), 400
    if len(q) > 300:
        q = q[:300]
    if not (doc.get("text") or "").strip():
        return jsonify({"error": "这篇还没有正文，先让 AI 把照片上的字抄出来再问"}), 400

    cfg = load_config()
    if not cfg.get("api_key"):
        return jsonify({"ok": False, "mode": "queue",
                        "message": "还没配置 AI 接口，暂时没法追问。"}), 202
    if not _ask_register(eid):
        return jsonify({"error": "上一个追问还在回答，等它说完再问"}), 409

    _ASK_CANCEL.discard(eid)
    # 只带最近 8 轮追问去当上下文：问得多了整个历史都塞进去，token 会越滚越大
    system, prompt = build_qa_prompt(doc, q, (doc.get("qa") or [])[-8:])

    def gen():
        buf = []
        finished = [False]        # 这一问一答已经落盘（只落一次）

        def should_stop():
            return eid in _ASK_CANCEL

        def persist(interrupted):
            """把这一问一答追加进 doc["qa"]；已落过盘就什么都不做。"""
            if finished[0]:
                return
            finished[0] = True
            ans = "".join(buf).strip()
            if not ans and interrupted:
                return                     # 一个字都没答出来 → 不留空壳
            # ⚠️ 重新从盘上读一份再追加，而不是直接用内存里的 doc：
            #    回答期间作文可能被改过（用户改正文 / 批改刚跑完），
            #    拿旧 doc 整份写回去会把那次改动覆盖掉。
            cur = load_essay(eid) or doc
            cur.setdefault("qa", [])
            cur["qa"].append({"q": q, "a": ans, "ts": time.time(),
                              "interrupted": bool(interrupted)})
            save_essay(cur)

        try:
            yield _sse("start", {"q": q})
            # 题目原图也带进追问上下文（以前只有文字题目，只拍图的题 AI 答问时是瞎的）；
            # 模型不认图就退回纯文本重来一次。
            _gimgs = _prompt_imgs(doc)
            for _itry in range(2):
                try:
                    for piece in iter_llm_tokens(cfg, prompt, system=system,
                                                 images=_gimgs or None,
                                                 should_stop=should_stop, timeout=_ASK_TIMEOUT,
                                                 think=_ASK_THINK):
                        if isinstance(piece, _Thinking):
                            continue      # 思考内容不是回答，别拼进答案
                        buf.append(piece)
                        yield _sse("delta", {"t": piece})
                    break                  # 正常答完
                except _Cancelled:
                    raise
                except Exception:
                    if not _gimgs:
                        raise
                    _gimgs, buf = [], []   # 退图重答（半截答案作废，别拼进去）
            if not "".join(buf).strip():
                raise RuntimeError("AI 没给出回答，请再问一次")
            persist(False)
            yield _sse("done", {"a": "".join(buf).strip()})
        except _Cancelled:
            persist(True)
            yield _sse("cancelled", {"msg": "已停止"})
        except Exception as e:
            persist(True)
            yield _sse("error", {"error": str(e)})
        finally:
            # 客户端中途断开（GeneratorExit）也会走到这儿 —— 已经答出来的那部分
            # 照样落盘，别让一次花了钱的调用凭空消失
            persist(True)
            _ASKING.pop(eid, None)
            _ASK_CANCEL.discard(eid)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             # 同上（批改流）：SSE 结束后别让浏览器复用这条连接
                             "Connection": "close"})


@app.route("/api/essays/<eid>/ask_cancel", methods=["POST"])
def api_ask_cancel(eid):
    """停止正在回答的追问。只打标志，收尾由 api_ask 的生成器做。"""
    _ASK_CANCEL.add(eid)
    return jsonify({"ok": True})


@app.route("/api/essays/<eid>/qa", methods=["DELETE"])
def api_qa_clear(eid):
    """清空这篇作文的追问记录（问答本身不属于作文正文，删了不影响批改）。"""
    doc = load_essay(eid)
    if not doc:
        return jsonify({"error": "找不到"}), 404
    doc["qa"] = []
    save_essay(doc)
    return jsonify({"ok": True})


@app.route("/api/images/<name>")
def api_image(name):
    if "/" in name or ".." in name:
        return jsonify({"error": "非法文件名"}), 400
    return send_from_directory(IMG_DIR, name)


# ⚠️ 本地 tesseract OCR 已移除（2026-09-25）：识别统一交给 AI 看图做，
#    见 api_revise 的「阶段 1」。别再把 tesseract 装回来。


@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = _norm_profiles(load_config())
    return jsonify({
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "has_key": bool(cfg.get("api_key")),
        "key_hint": mask_key(cfg.get("api_key", "")),
        "grade_mode": cfg.get("grade_mode", "fast"),
        "grade_strict": cfg.get("grade_strict", "strict"),   # 评分严格度：strict（默认）/ standard
        "active_profile": cfg["active_profile"],
        # ⚠️ 只给掩码 hint，绝不回传明文 key
        "profiles": [{"name": p.get("name", "未命名"),
                      "base_url": p.get("base_url", ""),
                      "model": p.get("model", ""),
                      "has_key": bool(p.get("api_key")),
                      "key_hint": mask_key(p.get("api_key", ""))}
                     for p in cfg["profiles"]],
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    cfg = _norm_profiles(load_config())
    data = request.get_json(force=True, silent=True) or {}

    # ---- 切换启用哪套 ----
    if "activate" in data:
        try:
            i = int(data.get("activate"))
        except Exception:
            i = -1
        if 0 <= i < len(cfg["profiles"]):
            p = cfg["profiles"][i]
            cfg["base_url"] = p.get("base_url", "")
            cfg["model"] = p.get("model", "")
            cfg["api_key"] = p.get("api_key", "")
            cfg["active_profile"] = i
            save_config(cfg)
        return jsonify({"ok": True, "active_profile": cfg["active_profile"]})

    # ---- 把「填好的一套」存为新配置（不改动当前套；没给的字段沿用当前值）----
    if "add_profile" in data:
        ap = data.get("add_profile") or {}
        name = (ap.get("name") or "").strip() or ("配置 %d" % (len(cfg["profiles"]) + 1))
        cfg["profiles"].append({"name": name,
                                "base_url": (ap.get("base_url") or cfg["base_url"]).strip(),
                                "model": (ap.get("model") or cfg["model"]).strip(),
                                "api_key": (ap.get("api_key") or cfg.get("api_key", "")).strip()})
        cfg["active_profile"] = len(cfg["profiles"]) - 1
        cfg["base_url"] = cfg["profiles"][-1]["base_url"]     # 存完就启用这套
        cfg["model"] = cfg["profiles"][-1]["model"]
        cfg["api_key"] = cfg["profiles"][-1]["api_key"]
        save_config(cfg)
        return jsonify({"ok": True, "active_profile": cfg["active_profile"]})

    # ---- 删除某套 ----
    if "delete_profile" in data:
        try:
            i = int(data.get("delete_profile"))
        except Exception:
            i = -1
        if 0 <= i < len(cfg["profiles"]):
            del cfg["profiles"][i]
            if not cfg["profiles"]:      # 删光了就拿当前值重建一套，别让存档库空着
                cfg["profiles"] = [{"name": "默认配置",
                                    "base_url": cfg["base_url"],
                                    "model": cfg["model"],
                                    "api_key": cfg.get("api_key", "")}]
            if cfg.get("active_profile", 0) >= len(cfg["profiles"]):
                cfg["active_profile"] = 0
            save_config(cfg)
        return jsonify({"ok": True, "active_profile": cfg["active_profile"]})

    # ---- 普通保存：改当前这套 ----
    for k in ("base_url", "model"):
        if data.get(k):
            cfg[k] = data[k].strip()
    if data.get("api_key"):
        cfg["api_key"] = data["api_key"].strip()
    if data.get("clear_key"):
        cfg["api_key"] = ""
    if data.get("grade_mode") in ("fast", "deep"):   # 只认这两档，别的值当没传
        cfg["grade_mode"] = data["grade_mode"]
    if data.get("grade_strict") in ("strict", "standard"):   # 评分严格度，同样只认这两档
        cfg["grade_strict"] = data["grade_strict"]
    # 改口令：传 new_password。⚠️ 留空不再等于「取消口令」——
    # 没口令 = 整站锁定（去终端 setup.sh 才能重开），网页里清空只会把自己锁外面。
    if "new_password" in data:
        npw = str(data.get("new_password") or "")
        if not npw.strip():
            return jsonify({"ok": False,
                            "error": "口令不能留空（留空会锁死网站）"}), 400
        cfg["password_hash"] = _hash_password(npw)
    if data.get("name"):
        cfg["profiles"][cfg["active_profile"]]["name"] = str(data["name"]).strip()
    # 编辑结果同步写回当前套的存档 —— 存档跟当前值永远一致，不会「切走再切回来就丢改动」
    p = cfg["profiles"][cfg["active_profile"]]
    p["base_url"], p["model"], p["api_key"] = cfg["base_url"], cfg["model"], cfg["api_key"]
    save_config(cfg)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 学情分析

ANALYZE_SYSTEM = (
    "你是一位资深语文教研员，擅长从多篇学生作文中提炼共性问题和教学建议。"
    "你只输出严格合法的 JSON，不输出任何解释文字。"
)


def _analyze_prompt():
    """收集所有已批改作文的摘要，构建学情分析 prompt。"""
    essays = []
    for fn in sorted(os.listdir(ESSAY_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(ESSAY_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        r = d.get("result") or {}
        if not r or r.get("score") is None:
            continue          # 只分析已批改的
        text = (d.get("text") or "").strip()
        # 摘要：标题、分数、档次、四维度、总评、扣题、逐段批注要点、提分建议
        parts = [
            "【标题】" + (d.get("title") or "未命名"),
            "【类别】" + (d.get("category") or "未指定"),
            "【分数】" + str(r.get("score")) + "（" + str(r.get("level") or "") + "）",
            "【字数】" + str(r.get("word_count") or _count_chars(text)),
            "【定档依据】" + str(r.get("band_reason") or ""),
            "【总评】" + str(r.get("summary") or ""),
            "【扣题】" + str(r.get("on_topic") or ""),
            "【四维度】" + "；".join(
                "%s=%s" % (dd.get("name"), dd.get("level"))
                for dd in (r.get("dims") or []) if isinstance(dd, dict)),
            "【扣分】" + "；".join(
                "%s(-%s)" % (dd.get("rule"), dd.get("points"))
                for dd in (r.get("deductions") or []) if isinstance(dd, dict)) or "无",
            "【提分建议】" + " | ".join(r.get("suggestions") or []),
        ]
        # 逐段批注里挑出「需要改」的要点（前 5 条）
        improve_pts = []
        for c in (r.get("comments") or []):
            if isinstance(c, dict) and c.get("improve"):
                improve_pts.append("P%s: %s" % (c.get("para", "?"), c["improve"][:80]))
            if len(improve_pts) >= 5:
                break
        if improve_pts:
            parts.append("【主要问题】" + " | ".join(improve_pts))
        # 正文前 300 字（让 AI 感受语言风格）
        if text:
            parts.append("【正文开头】" + text[:300])
        essays.append("\n".join(parts))

    if not essays:
        return None, 0

    body = (
        "以下是 %d 篇已批改的学生作文摘要：\n\n" % len(essays)
        + ("\n\n---\n\n".join(essays))
        + "\n\n---\n\n"
        + "请基于以上所有作文，输出一个 JSON 对象，结构如下：\n"
        + "{\n"
        + '  "overview": "整体学情概述，2~3 句话",\n'
        + '  "common_problems": [\n'
        + '    {"problem": "共性问题名称", "frequency": "出现篇数/比例", "detail": "具体表现和例子", "fix": "改进建议"}\n'
        + "  ],\n"
        + '  "excellent": [\n'
        + '    {"title": "作文标题", "score": 分数, "reason": "为什么好，好在哪里"}\n'
        + "  ],\n"
        + '  "needs_work": [\n'
        + '    {"title": "作文标题", "score": 分数, "reason": "主要问题是什么"}\n'
        + "  ],\n"
        + '  "teaching_tips": ["教学建议1", "教学建议2", "教学建议3"]\n'
        + "}\n"
        + "要求：\n"
        + "- common_problems 找 3~6 个最有价值的共性问题，按严重程度排序\n"
        + "- excellent 挑 1~3 篇精品作文（分数最高的或进步最明显的）\n"
        + "- needs_work 挑 1~3 篇最需要关注的作文\n"
        + "- 如果作文总数少于 3 篇，excellent 和 needs_work 各最多 1 篇\n"
        + "- teaching_tips 给 2~4 条可操作的教学建议\n"
        + "- 语言要像教研员写分析报告，具体、有数据、不说空话"
    )
    return body, len(essays)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """AI 学情分析：批量分析所有已批改作文，找共性问题、挑精品/劣质。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        return jsonify({"error": "没有配置 AI 接口 Key，无法分析"}), 400

    prompt, count = _analyze_prompt()
    if not prompt:
        return jsonify({"error": "还没有已批改的作文，先批改几篇再来"}), 400
    if count < 2:
        return jsonify({"error": "已批改的作文只有 %d 篇，至少需要 2 篇才能分析共性" % count}), 400

    try:
        result = call_llm(cfg, prompt, system=ANALYZE_SYSTEM, think="off")
    except Exception as e:
        return jsonify({"error": "AI 分析失败：%s" % e}), 502

    if not isinstance(result, dict):
        return jsonify({"error": "AI 返回格式不对"}), 502

    return jsonify({
        "count": count,
        "overview": str(result.get("overview") or ""),
        "common_problems": result.get("common_problems") or [],
        "excellent": result.get("excellent") or [],
        "needs_work": result.get("needs_work") or [],
        "teaching_tips": result.get("teaching_tips") or [],
    })


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    import os
    host = os.environ.get("HOST", "0.0.0.0")
    # 端口优先级：环境变量 PORT（临时指定）> config.json 的 port（setup.sh 里选的）> 8083
    try:
        port = int(os.environ.get("PORT") or load_config().get("port") or 8083)
    except (ValueError, TypeError):
        port = 8083
    print("作文批改已启动： http://%s:%d   （Ctrl+C 停止）" % (host, port))
    app.run(host=host, port=port, threaded=True)
