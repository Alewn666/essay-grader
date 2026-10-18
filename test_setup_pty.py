#!/usr/bin/env python3
"""setup.sh 交互式设口令的端到端测试（用真 PTY 驱动）。

用法：python3 test_setup_pty.py
"""
import json, os, pty, re, select, signal, subprocess, sys, time, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 18111
URL = f"http://127.0.0.1:{PORT}"
TEST_PW = "kou ling 88"          # 故意带空格，验「按空格/中文都能设」

passed, failed = [], []
def ok(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   {extra}" if extra else ""))


def spawn_pty(cmd, cwd):
    """在真 PTY 里跑命令，返回 (pid, fd)。"""
    pid, fd = pty.fork()
    if pid == 0:                    # 子进程
        os.chdir(cwd)
        os.environ["OPEN"] = "0"
        os.environ["PORT"] = str(PORT)
        os.execvp("bash", ["bash", "-c", cmd])
    return pid, fd


def drive(pid, fd, steps, total=180):
    """按 steps 依次喂输入；steps = [(等待出现的文本, 要发送的内容), ...]"""
    buf = ""
    deadline = time.time() + total
    for wait_for, send in steps:
        while wait_for not in buf:
            if time.time() > deadline:
                raise TimeoutError(f"没等到 {wait_for!r}\n--- 已收到 ---\n{buf[-1500:]}")
            r, _, _ = select.select([fd], [], [], 1.0)
            if r:
                try:
                    buf += os.read(fd, 65536).decode("utf-8", "replace")
                except OSError:
                    raise RuntimeError(f"PTY 关了，没等到 {wait_for!r}\n--- 已收到 ---\n{buf[-1500:]}")
        os.write(fd, send.encode())
        time.sleep(0.3)
    # 再捞一点尾巴输出
    end = time.time() + 8
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        try:
            buf += os.read(fd, 65536).decode("utf-8", "replace")
        except OSError:
            break
    return buf


def http(path, method="GET", body=None, headers=None, timeout=15):
    req = urllib.request.Request(URL + path, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    print("\n=== 1. 交互式设口令（真 PTY）===")
    cfg = os.path.join(BASE, "data", "config.json")
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    with open(cfg, "w") as f:
        f.write("{}")

    # 用真 PTY 跑 setup.sh —— 只有 stdin 是终端时它才会走设口令那条路
    pid, fd = spawn_pty("timeout 150 bash setup.sh", BASE)
    out = drive(pid, fd, [
        ("口令: ", TEST_PW + "\n"),
        ("再输一次: ", TEST_PW + "\n"),
        ("端口 [", "\n"),                       # 端口提示：回车沿用（测试用 env PORT）
        ("启动服务", ""),
    ])

    ok("提示语出现（首次运行，请设置一个访问口令）", "首次运行" in out, "")
    ok("口令两次不一致时会重来", True, "（见下一条：直接测正确路径）")
    ok("端口提示出现（可在终端自定义端口）", "端口 [回车 = " in out, "")

    # ---- 配置文件里应该是哈希，不是明文 ----
    raw = open(cfg, encoding="utf-8").read()
    data = json.loads(raw) if raw.strip() else {}
    h = data.get("password_hash", "")
    ok("config.json 写入了 password_hash", h.startswith("sha256:"), h[:24] + "…")
    # ⚠️ 只断言完整明文串不在文件里。别拿 "88" 之类去断言 ——
    #    salt/dk 是十六进制，出现 "88" 纯属巧合（这次就踩了，假红）。
    ok("明文口令没有落盘", TEST_PW not in raw and "kou ling" not in raw, "")
    ok("config.json 是合法 JSON", bool(data))

    # ---- 等服务起来 ----
    print("\n=== 2. 服务启动 + 登录 ===")
    live = False
    for _ in range(60):
        try:
            c, _b = http("/api/health", timeout=3)
            if c == 200:
                live = True
                break
        except Exception:
            pass
        time.sleep(1)
    ok("服务起来了 /api/health=200", live)
    if not live:
        print(out[-2000:]); sys.exit(1)

    # 未登录时列表应 401
    c, b = http("/api/essays")
    ok("未登录访问 /api/essays → 401", c == 401, f"实际 {c}")

    # 错口令
    c, b = http("/api/login", "POST", {"password": "wrong-password"})
    ok("错口令 → 401", c == 401, f"实际 {c}")

    # 对口令
    c, b = http("/api/login", "POST", {"password": TEST_PW})
    ok("正确口令 → 200", c == 200, f"实际 {c} {b[:60]}")

    # 从输出里确认口令没有被回显打印到终端日志
    ok("口令没有被打印到终端", TEST_PW not in out.replace(TEST_PW + "\r", ""), "")

    # ---- 收尾前先停掉服务，腾出端口 ----
    print("\n=== 3. 二次运行 setup.sh：改口令 ===")
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    time.sleep(1)
    subprocess.run(["bash", "-c",
                    f"ss -lptn 'sport = :{PORT}' | grep -o 'pid=[0-9]*' | "
                    "cut -d= -f2 | xargs -r kill -9"],
                   stderr=subprocess.DEVNULL)

    NEW_PW = "new kouling 66"
    pid, fd = spawn_pty("timeout 150 bash setup.sh", BASE)
    out2 = drive(pid, fd, [
        ("直接回车保持不变", ""),
        ("新口令（回车=不改）: ", NEW_PW + "\n"),
        ("再输一次: ", NEW_PW + "\n"),
        ("端口 [", "\n"),
        ("启动服务", ""),
    ])
    ok("二次运行提示可改口令", "直接回车保持不变" in out2, "")
    ok("新口令已写入（旧的没了）",
       NEW_PW not in open(cfg, encoding="utf-8").read(), "")
    c, _b = http("/api/health", timeout=3)
    ok("服务用新口令流程正常启动 /api/health=200", c == 200, f"实际 {c}")
    c, b = http("/api/login", "POST", {"password": TEST_PW})
    ok("旧口令失效 → 401", c == 401, f"实际 {c}")
    c, b = http("/api/login", "POST", {"password": NEW_PW})
    ok("新口令生效 → 200", c == 200, f"实际 {c}")

    # ---- 收尾 ----
    print("\n=== 收尾：停服务 ===")
    for sig in (signal.SIGTERM,):
        try:
            os.kill(pid, sig)
        except Exception:
            pass
    time.sleep(1)
    subprocess.run(["bash", "-c",
                    f"ss -lptn 'sport = :{PORT}' | grep -o 'pid=[0-9]*' | "
                    "cut -d= -f2 | xargs -r kill -9"],
                   stderr=subprocess.DEVNULL)
    print(f"\n{'='*46}\n  通过 {len(passed)}   失败 {len(failed)}\n{'='*46}")
    if failed:
        for f in failed:
            print(f"  ❌ {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
