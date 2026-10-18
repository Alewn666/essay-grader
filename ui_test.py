#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开源版（essay-grader）真浏览器 UI 测试。

覆盖：登录遮罩 / 错口令提示 / 正确口令进入 / 设置页改口令卡 /
     三视口（手机·平板·电脑）无横向溢出 / 零 JS 报错。

用法：
    source /var/apps/com.dustinky.qwenpaw/home/venv/bin/activate
    PLAYWRIGHT_BROWSERS_PATH=/vol1/@appconf/com.dustinky.qwenpaw/.cache/ms-playwright \
        python3 ui_test.py
"""
import json, os, shutil, signal, socket, subprocess, sys, tempfile, time, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PW = "ui-test-pw-2026"                       # 测试专用口令，绝不用生产值

# Playwright 浏览器缓存不在默认位置时自己补上，别指望调用方记得 export。
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/vol1/@appconf/com.dustinky.qwenpaw/.cache/ms-playwright")

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("   " + str(extra)) if extra else ""))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_orphans():
    """先杀掉上次残留的服务（必须在**启动之前**做，否则会把自己的杀掉 —— 踩过）。"""
    out = subprocess.run(["bash", "-c",
                          "ss -lptn 2>/dev/null | grep 'essay-grader' | grep -o 'pid=[0-9]*' | cut -d= -f2"],
                         capture_output=True, text=True).stdout
    for pid in set(out.split()):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
            if "app.py" in cmd:
                os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass


def wait_up(url, tries=60):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ❌ 没装 playwright，先跑  pip install playwright && playwright install chromium")
        sys.exit(2)

    kill_orphans()
    port = free_port()
    url = f"http://127.0.0.1:{port}"

    tmp = tempfile.mkdtemp(prefix="ui_test_")
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    # 塞一把假 API key：没 key 后端会走队列模式，前端行为不一样
    with open(os.path.join(tmp, "data", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"base_url": "https://example.invalid/v1",
                   "api_key": "sk-fake-for-ui-test", "model": "fake"}, f)

    env = dict(os.environ,
               ESSAY_DATA_DIR=os.path.join(tmp, "data"),
               ESSAY_PASSWORD=PW,
               PORT=str(port), HOST="127.0.0.1", OPEN="0")
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=BASE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    js_errors = []

    def on_console(m):
        # 401/403 是**预期行为**：登录页 loadList() 必然 401；锁定页（未设口令）
        # 的 API 全 403 —— 都是设计如此，不是 bug（第一版把 401 算成假红过）。
        if m.type != "error":
            return
        if "401" in m.text or "401" in str(m.location or ""):
            return
        if "403" in m.text or "403" in str(m.location or ""):
            return
        js_errors.append(m.text)
    try:
        if not wait_up(url):
            print("  ❌ 服务没起来"); sys.exit(1)
        print(f"\n  服务就绪: {url}\n")

        with sync_playwright() as p:
            browser = p.chromium.launch()

            def new_page(vp):
                pg = browser.new_page(viewport=vp)
                pg.on("pageerror", lambda e: js_errors.append(str(e)))
                pg.on("console", on_console)
                return pg

            # ---------- 1. 登录遮罩 ----------
            print("=== 1. 登录遮罩 ===")
            pg = new_page({"width": 1280, "height": 800})
            pg.goto(url, wait_until="networkidle")

            ok("登录遮罩显示", pg.is_visible("#loginOverlay"))
            ok("口令输入框在遮罩里", pg.is_visible("#loginPw"))
            ok("错误提示初始隐藏", not pg.is_visible("#loginErr"))
            ok("设置口令遮罩已移除", pg.query_selector("#setPwOverlay") is None)
            ok("控制面板视图已移除", pg.query_selector("#view-panel") is None)
            ok("同步按钮已移除", pg.query_selector("#btnSyncPull") is None)

            # ---------- 2. 错口令 ----------
            print("\n=== 2. 错口令 ===")
            pg.fill("#loginPw", "definitely-wrong-pw")
            pg.click("#btnLogin")
            pg.wait_for_selector("#loginErr", state="visible", timeout=5000)
            ok("错口令后出现错误提示", pg.is_visible("#loginErr"))
            ok("仍然停在登录页", pg.is_visible("#loginOverlay"))

            # ---------- 3. 对口令 ----------
            print("\n=== 3. 正确口令 ===")
            pg.fill("#loginPw", PW)
            pg.click("#btnLogin")
            pg.wait_for_selector("#loginOverlay", state="hidden", timeout=8000)
            ok("登录成功，遮罩消失", not pg.is_visible("#loginOverlay"))
            pg.wait_for_selector("#view-list", state="visible", timeout=8000)
            ok("进入应用主视图", pg.is_visible("#view-list"))

            # ---------- 4. 设置页有改口令卡 ----------
            print("\n=== 4. 设置页 ===")
            pg.click("#btnCfg") if pg.query_selector("#btnCfg") else None
            pg.wait_for_timeout(800)
            ok("设置视图打开", pg.is_visible("#view-cfg"))
            ok("有「访问口令」卡", pg.is_visible("#cfgNewPw"))
            ok("有「改口令」按钮", pg.is_visible("#btnSavePw"))
            ok("没有同步地址输入框", pg.query_selector("#cfgSyncUrl") is None)

            # ---------- 5. 三视口不溢出 ----------
            print("\n=== 5. 三视口无横向溢出 ===")
            for name, vp in [("手机 390", {"width": 390, "height": 844}),
                             ("平板 768", {"width": 768, "height": 1024}),
                             ("电脑 1280", {"width": 1280, "height": 800})]:
                v = new_page(vp)
                v.goto(url, wait_until="networkidle")
                v.wait_for_timeout(600)
                if v.is_visible("#loginOverlay"):
                    # 已登录的 cookie 不共享到新 page，遮罩挡着 —— 先登进去
                    v.fill("#loginPw", PW)
                    v.click("#btnLogin")
                    v.wait_for_selector("#loginOverlay", state="hidden", timeout=8000)
                    v.wait_for_timeout(600)
                over = v.evaluate(
                    "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                ok(f"{name} 无横向溢出", over <= 1, f"溢出 {over}px")
                v.close()

            # ---------- 5.5 没设口令 → 锁定页（提示去终端 setup.sh） ----------
            print("\n=== 5.5 未设口令锁定页 ===")
            tmp2 = tempfile.mkdtemp(prefix="ui_test_nopw_")
            os.makedirs(os.path.join(tmp2, "data"), exist_ok=True)
            with open(os.path.join(tmp2, "data", "config.json"), "w", encoding="utf-8") as f:
                json.dump({}, f)                       # 空配置 = 没口令
            port2 = free_port()
            env2 = dict(os.environ, ESSAY_DATA_DIR=os.path.join(tmp2, "data"),
                        PORT=str(port2), HOST="127.0.0.1", OPEN="0")
            env2.pop("ESSAY_PASSWORD", None)            # 关键：不能带口令环境变量
            proc2 = subprocess.Popen([sys.executable, "app.py"], cwd=BASE, env=env2,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                if wait_up(f"http://127.0.0.1:{port2}"):
                    url2 = f"http://127.0.0.1:{port2}"
                    p2 = new_page({"width": 1280, "height": 800})
                    p2.goto(url2, wait_until="networkidle")
                    ok("锁定页 setupOverlay 显示", p2.is_visible("#setupOverlay"))
                    ok("锁定页有 setup.sh 提示", "setup.sh" in p2.inner_text("#setupOverlay"))
                    ok("锁定页没有口令输入框", p2.query_selector("#loginPw") is None)
                    ok("锁定页 API 返回 no_password",
                       p2.evaluate("() => fetch('/api/essays').then(r => r.status)") == 403)
                    p2.close()
                else:
                    ok("无口令实例没起来", False)
            finally:
                proc2.terminate()
                try:
                    proc2.wait(timeout=8)
                except Exception:
                    proc2.kill()
                shutil.rmtree(tmp2, ignore_errors=True)

            pg.close()
            browser.close()

        # ---------- 6. 零 JS 报错 ----------
        print("\n=== 6. JS 报错 ===")
        real = [e for e in js_errors if "favicon" not in e.lower()]
        ok("零 JS 报错", len(real) == 0, (real[:2] if real else ""))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        kill_orphans()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 50)
    print(f"  通过 {len(PASS)}    失败 {len(FAIL)}")
    print("=" * 50)
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
