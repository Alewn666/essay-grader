#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开源版（essay-grader）后端测试。

覆盖：登录口令门 / 未登录的各种行为 / 建作文 / 流式批改（打桩 AI）/
     报告下载 / 改口令 / 配置不泄露 key / 已删除的同步与控制面板接口确实是 404。

用法：  python3 test_grader.py
打桩，不联网、不花钱。
"""
import io, json, os, shutil, sys, tempfile, uuid

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

TMP = tempfile.mkdtemp(prefix="grader_test_")
os.environ["ESSAY_DATA_DIR"] = TMP
os.environ["ESSAY_PASSWORD"] = "test-pw-12345"        # 测试专用口令，绝不用生产值
for k in ("ESSAY_BASE_URL", "ESSAY_API_KEY", "ESSAY_MODEL"):
    os.environ.pop(k, None)

# 先塞一把**假的** API key：没 key 时后端会走「待批改」队列模式，
# 根本不调 AI —— 那样批改相关的断言全都会假红。
os.makedirs(TMP, exist_ok=True)
with open(os.path.join(TMP, "config.json"), "w", encoding="utf-8") as f:
    json.dump({"base_url": "https://example.invalid/v1", "api_key": "sk-fake-for-test",
               "model": "fake-model"}, f)

import app as srv                                    # noqa: E402  (环境变量要先设好)

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("   " + str(extra)) if extra else ""))


# ------------------------------------------------------------------ 打桩 AI
CALLS = []                                           # 记录每次调用，供断言

OCR_TEXT = "难忘的一天\n\n今天我和妈妈去了公园，看到了美丽的花。我们玩得很开心，这真是难忘的一天。"

GRADE_JSON = {
    "score": 46, "base_score": 46, "level": "三类文", "level_range": "42~48",
    "band_reason": "选材真实但详略失当，语言平淡。",
    "word_count": 40, "title_ok": True,
    "deductions": [{"item": "字数不足 500 字", "points": -3}],
    "dims": [{"name": "情境", "score": 11, "comment": "有真实情境"},
             {"name": "内容", "score": 10, "comment": "素材单薄"},
             {"name": "结构", "score": 12, "comment": "结构完整"},
             {"name": "语言", "score": 13, "comment": "语言平实"}],
    "comments": [{"para": 1, "text": "开头平淡，可以更抓人。"}],
    # ⚠️ highlights 必须是**对象数组**，不是字符串数组 ——
    #    后端 _anchor_annotations() 会取 h.get("quote")，给字符串直接
    #    `AttributeError: 'str' object has no attribute 'get'`（踩过）。
    #    quote 还要**逐字**出现在正文里，否则整条被丢弃（宁可少标不猜位置）。
    "highlights": [{"quote": "这真是难忘的一天。", "issue": "结尾空洞，喊口号"}],
    "revised": "修改后的全文……",
    "suggestions": ["多写细节", "结尾要点题"],
    "common_problems": ["流水账"],
    "excellent": ["感情真挚"], "needs_work": ["缺少细节描写"],
    "teaching_tips": ["多练细节描写"],
    "overview": "整体尚可，需要加强细节。",
}


def stub_iter_llm_tokens(cfg, prompt, images=None, system=None, should_stop=None,
                         timeout=None, think=None):
    """假 AI。识别趟（system 带 OCR）抄正文；批改趟吐打分 JSON。

    ⚠️ 签名必须和真函数对齐（timeout=/think= 是后加的参数，
       桩漏了就会 TypeError 把整条流搞死 —— 以前踩过）。
    """
    CALLS.append({"system": system, "has_images": bool(images),
                  "prompt_len": len(prompt or "")})
    if system and "OCR" in system:
        payload = json.dumps({"text": OCR_TEXT, "title": "难忘的一天"},
                             ensure_ascii=False)
    else:
        payload = json.dumps(GRADE_JSON, ensure_ascii=False)
    # 分块吐，模拟流式
    for i in range(0, len(payload), 12):
        yield payload[i:i + 12]


srv.iter_llm_tokens = stub_iter_llm_tokens

client = srv.app.test_client()
PW = os.environ["ESSAY_PASSWORD"]


def login(pw):
    """登录。werkzeug test client 会自动带上种下的 session cookie。"""
    return client.post("/api/login", json={"password": pw})


def main():
    print("\n=== 1. 登录口令门 ===")
    ok("GET /api/health 放行", client.get("/api/health").status_code == 200)
    ok("未登录 GET /api/essays → 401", client.get("/api/essays").status_code == 401)
    ok("未登录 GET / → 200（渲染登录遮罩）", client.get("/").status_code == 200)
    ok("登录遮罩确实出现在页面里",
       b"loginOverlay" in client.get("/").data)
    ok("设置口令遮罩已移除", b"setPwOverlay" not in client.get("/").data)
    ok("控制面板视图已移除", b"view-panel" not in client.get("/").data)
    ok("同步卡已移除", b"btnSyncPull" not in client.get("/").data)
    ok("静态资源放行", client.get("/static/js/app.js").status_code == 200)
    ok("错口令 → 401", login("wrong-password-x").status_code == 401)

    r = login(PW)
    ok("正确口令 → 200", r.status_code == 200, r.get_json())

    # werkzeug 的 test client 会自动带上上面那次请求种的 cookie
    ok("登录后 GET /api/essays → 200", client.get("/api/essays").status_code == 200)
    ok("登录后列表是空数组", client.get("/api/essays").get_json() == [])

    print("\n=== 2. 已删除的接口必须 404 ===")
    for path, method in [("/api/sync", "POST"), ("/api/sync/import", "POST"),
                         ("/api/panel", "GET"), ("/api/settings/login", "POST")]:
        r = client.open(path, method=method, json={})
        ok(f"{method} {path} → 404", r.status_code == 404, r.status_code)

    print("\n=== 3. 建作文（用 .txt 文档，跳过识别趟）===")
    txt = (OCR_TEXT + "\n").encode("utf-8")
    import base64
    doc_b64 = "data:text/plain;base64," + base64.b64encode(txt).decode()
    r = client.post("/api/essays", json={
        "title": "难忘的一天", "prompt": "以难忘的一天为题", "docx": doc_b64,
        "docx_name": "zuowen.txt"})
    ok("建作文成功", r.status_code in (200, 201), r.status_code)
    eid = r.get_json()["id"]
    doc = client.get(f"/api/essays/{eid}").get_json()
    ok("正文从文档抽出来了", "难忘的一天" in doc["text"], doc["text"][:24])
    ok("text_edited=True（文档正文不再让 AI 覆盖）", doc["text_edited"] is True)
    ok("状态是 draft", doc["status"] == "draft")

    print("\n=== 4. 流式批改（打桩 AI）===")
    CALLS.clear()
    r = client.post(f"/api/essays/{eid}/revise_stream")
    ok("revise_stream 返回 200", r.status_code == 200)
    # ⚠️ 必须用 iter_encoded() 把流读完 —— 生成器是惰性的，
    #    get_data() 在流式响应上读不到内容（第一版就这么假红过）。
    body = b"".join(r.iter_encoded()).decode("utf-8", "replace")
    ok("桩 AI 真的被调用了", len(CALLS) > 0, f"{len(CALLS)} 次")
    # SSE 的行格式是 `event: done`（冒号后有空格），别写成 "event:done"
    ok("流里有 done 事件", "event: done" in body)
    ok("流里推了 field 事件", "event: field" in body)
    ok("流里没漏出 _Thinking", "_Thinking" not in body)
    if "event: done" not in body:
        print("    —— 流尾部诊断 ——")
        print("    " + body[-700:].replace("\n", "\n    "))

    doc = client.get(f"/api/essays/{eid}").get_json()
    ok("批改后状态是 done", doc["status"] == "done")
    ok("分数落盘", (doc.get("result") or {}).get("score") is not None)
    ok("修改后全文落盘", bool((doc.get("result") or {}).get("revised")))
    ok("文档来的正文没被识别趟覆盖", "难忘的一天" in doc["text"])

    print("\n=== 5. 报告下载 ===")
    r = client.get(f"/api/essays/{eid}/report.txt")
    ok("报告 200", r.status_code == 200)
    txt_out = r.get_data(as_text=True)
    ok("报告是 attachment", "attachment" in (r.headers.get("Content-Disposition") or ""))
    ok("报告有实质内容", len(txt_out) > 200, f"{len(txt_out)} 字")
    # 分数以落盘值为准 —— 本地会按字数重算扣分，别把 AI 报的数字写死在断言里
    real_score = str((doc.get("result") or {}).get("score"))
    ok("报告带分数", real_score in txt_out, f"实际分数 {real_score}")

    print("\n=== 6. 配置接口不泄露 key ===")
    r = client.get("/api/config")
    ok("GET /api/config → 200", r.status_code == 200)
    cfg = r.get_json()
    ok("响应里没有明文 key 字段", "api_key" not in json.dumps(cfg))
    ok("响应里没有 password_hash", "password_hash" not in json.dumps(cfg))
    ok("响应里没有 sync / panel 字段",
       not any(k.startswith("sync") or k.startswith("control") for k in cfg))

    print("\n=== 7. 没设口令 = 整站锁死（提示去终端 setup.sh）===")
    # ⚠️ ESSAY_PASSWORD 环境变量优先级高于 config.json（设计如此，方便容器部署）。
    #    不先撤掉的话，config 里的口令根本不会生效 —— 第一版就这么假红过。
    os.environ.pop("ESSAY_PASSWORD", None)
    srv._ENV_HASH_CACHE.clear()
    client.delete_cookie("session")
    r = client.get("/api/essays")
    ok("撤掉 env 口令且 config 无口令 → 403 锁站（不再免登录）",
       r.status_code == 403 and r.get_json().get("error") == "no_password",
       r.status_code)
    ok("锁定页出现 setupOverlay", b"setupOverlay" in client.get("/").data)
    ok("锁定时不渲染登录输入框", b"loginPw" not in client.get("/").data)
    r = client.post("/api/login", json={"password": "anything"})
    ok("锁站时登录接口也 403", r.status_code == 403, r.status_code)
    r = client.post("/api/config", json={"new_password": "x"})
    ok("锁站时不能从网页设口令（必须走 setup.sh）", r.status_code == 403)

    # 模拟 setup.sh 写入口令（直接写哈希，与 setup.sh 同款 PBKDF2）
    stored = json.load(open(os.path.join(TMP, "config.json"), encoding="utf-8"))
    stored["password_hash"] = srv._hash_password("set-pw-888")
    with open(os.path.join(TMP, "config.json"), "w", encoding="utf-8") as f:
        json.dump(stored, f, ensure_ascii=False)

    r = client.get("/api/essays")
    ok("setup.sh 设完口令 → 恢复 401 门", r.status_code == 401)
    r = client.get("/")
    ok("设完口令后渲染登录遮罩而不是锁定页",
       b"loginOverlay" in r.data and b"setupOverlay" not in r.data)
    ok("新设的口令能登录", login("set-pw-888").status_code == 200)

    r = client.post("/api/config", json={"new_password": "new-pw-999"})
    ok("网页里改口令 200", r.status_code == 200)
    stored = json.load(open(os.path.join(TMP, "config.json"), encoding="utf-8"))
    ok("落盘的是哈希不是明文",
       stored.get("password_hash", "").startswith("sha256:")
       and "new-pw-999" not in json.dumps(stored))

    client.delete_cookie("session")                  # 模拟换浏览器重新登录
    ok("旧口令失效 → 401", login("set-pw-888").status_code == 401)
    ok("新口令生效 → 200", login("new-pw-999").status_code == 200)

    print("\n=== 8. 改口令不允许留空（留空 = 锁死自己）===")
    r = client.post("/api/config", json={"new_password": ""})
    ok("空口令 → 400", r.status_code == 400, r.status_code)
    stored = json.load(open(os.path.join(TMP, "config.json"), encoding="utf-8"))
    ok("password_hash 原样还在",
       stored.get("password_hash", "").startswith("sha256:"))
    client.delete_cookie("session")
    ok("原口令仍可登录", login("new-pw-999").status_code == 200)

    test_update()   # 第 9 节：软件更新

    print("\n" + "=" * 50)
    print(f"  通过 {len(PASS)}    失败 {len(FAIL)}")
    print("=" * 50)
    for f in FAIL:
        print(f"  ❌ {f}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


# ---------------------------------------------------------------- 更新测试
def _mk_github_mock(latest_ver="9.9.9"):
    """monkeypatch _gh_latest_release，避免真联网"""
    srv._gh_latest_release = lambda: {
        "version": latest_ver, "name": "v" + latest_ver,
        "body": "test release notes", "tarball_url": "https://example.invalid/x.tar.gz"
    }


def test_update():
    print("\n=== 9. 软件更新 ===")
    client = srv.app.test_client()

    # 未登录 → 401
    r = client.get("/api/update/check")
    ok("未登录查更新 401", r.status_code == 401, r.status_code)

    # 登录后正常用的当前口令（第 8 节已改成 new-pw-999）
    r = client.post("/api/login", json={"password": "new-pw-999"})
    ok("登录成功", r.status_code == 200)

    # 轻量版本接口（不连 GitHub，页面/设置页加载用）
    r = client.get("/api/version")
    d = r.get_json()
    ok("/api/version 返回当前版本", r.status_code == 200
       and d.get("version") == srv.__version__, d.get("version"))
    client.delete_cookie("session")
    r = client.get("/api/version")
    ok("未登录 /api/version 401", r.status_code == 401, r.status_code)
    r = client.post("/api/login", json={"password": "new-pw-999"})
    ok("重新登录", r.status_code == 200)

    # _parse_ver 纯函数：tag 不是版本号（本仓库 tag="essay-grader"）时回退 name
    ok("_parse_ver('v1.0.2')='1.0.2'", srv._parse_ver("v1.0.2") == "1.0.2")
    ok("_parse_ver('essay-grader')=None", srv._parse_ver("essay-grader") is None)
    ok("_parse_ver('1.0.0')='1.0.0'", srv._parse_ver("1.0.0") == "1.0.0")
    ok("_parse_ver('')=None", srv._parse_ver("") is None)
    ok("_parse_ver(None)=None", srv._parse_ver(None) is None)

    # 当前版本号字段
    _orig_gh = srv._gh_latest_release
    _mk_github_mock("9.9.9")
    r = client.get("/api/update/check")
    d = r.get_json()
    ok("查更新返回 ok", d.get("ok") is True)
    ok("有 current 字段", d.get("current") == srv.__version__,
       d.get("current"))
    ok("有 latest 字段", d.get("latest") == "9.9.9")
    ok("has_update=True（9.9.9 > 当前）", d.get("has_update") is True)

    # 已是最新 → has_update=False
    _mk_github_mock(srv.__version__)
    r = client.get("/api/update/check")
    d = r.get_json()
    ok("同版本 has_update=False", d.get("has_update") is False)

    # 旧版本 → has_update=False
    _mk_github_mock("0.0.1")
    r = client.get("/api/update/check")
    d = r.get_json()
    ok("旧版本 has_update=False", d.get("has_update") is False)

    # apply：已是最新 → 400
    _mk_github_mock(srv.__version__)
    r = client.post("/api/update/apply")
    ok("已是最新 apply 400", r.status_code == 400, r.status_code)
    ok("错误信息含'最新'", "最新" in r.get_json().get("error", ""))

    # apply：需要更新但下载失败 → 502（tarball_url 指向假地址）
    _mk_github_mock("9.9.9")
    r = client.post("/api/update/apply")
    ok("下载失败 502", r.status_code == 502, r.status_code)

    # apply：未登录 → 401
    client.delete_cookie("session")
    r = client.post("/api/update/apply")
    ok("未登录 apply 401", r.status_code == 401, r.status_code)

    # 还原真实函数
    srv._gh_latest_release = _orig_gh


if __name__ == "__main__":
    main()
