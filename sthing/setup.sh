#!/usr/bin/env bash
# ============================================================
#  作文批改 —— 一键启动脚本
#
#  用法：
#    双击这个文件，或者在终端里跑   sh setup.sh
#
#  它会依次做四件事：
#    1. 检查 Python
#    2. 建虚拟环境、装依赖（只有 Flask，第一次跑需要联网）
#    3. 首次运行：让你设置一个访问口令
#    4. 启动服务，给你一个网址
#
#  常用环境变量：
#    PORT=9000 sh setup.sh     改端口（默认 8083）
#    OPEN=0 sh setup.sh        不自动打开浏览器
# ============================================================

set -euo pipefail

# ---- 定位到脚本所在目录（双击运行时 cwd 不一定是这里）----
cd "$(dirname "$(readlink -f "$0")")"

# 端口这里**不给默认值**：优先级 = 环境变量 PORT > config.json 记住的 > 8083（脚本末尾统一解析）
PORT="${PORT:-}"
VENV_DIR="$PWD/.venv"
DATA_DIR="$PWD/data"
CONFIG="$DATA_DIR/config.json"
PY="$VENV_DIR/bin/python3"

# ---- 配色（不支持就退化成纯文本）----
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  B=$(tput bold); R=$(tput sgr0); G=$(tput setaf 2); Y=$(tput setaf 3); RD=$(tput setaf 1)
else
  B=""; R=""; G=""; Y=""; RD=""
fi

ok()   { printf '  %s✅ %s%s\n' "$G" "$*" "$R"; }
warn() { printf '  %s⚠️  %s%s\n' "$Y" "$*" "$R"; }
die()  { printf '\n%s❌ %s%s\n' "$RD" "$*" "$R"; printf '\n按回车键关闭…\n'; read -r _ 2>/dev/null || true; exit 1; }

printf '\n%s📝 作文批改%s —— 启动检查\n\n' "$B" "$R"

# ------------------------------------------------------------
# 1. 检查 Python
# ------------------------------------------------------------
PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PYTHON_BIN="$c"; break; fi
  done
fi
[ -n "$PYTHON_BIN" ] || die "找不到 Python 3。

  Ubuntu / Debian:  sudo apt install python3
  Fedora / RHEL:    sudo dnf install python3
  Arch:             sudo pacman -S python"

PYVER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null \
  || die "Python 版本太老（找到 $PYVER），需要 3.8 或更高。"
ok "Python $PYVER"

# ------------------------------------------------------------
# 2. 虚拟环境 + 依赖
# ------------------------------------------------------------
# 虚拟环境：建好了但没 pip（Debian 常见）→ 重建带 --system-site-packages，
# 直接用系统里已装的 Flask，不需要联网
if [ -x "$PY" ] && ! "$PY" -m pip --version >/dev/null 2>&1 && ! "$PY" -c 'import flask' >/dev/null 2>&1; then
  warn "虚拟环境没有 pip，改用系统已装的包…"
  rm -rf "$VENV_DIR"
fi

if [ ! -x "$PY" ]; then
  printf '  正在创建虚拟环境…\n'
  if "$PYTHON_BIN" -m venv --system-site-packages "$VENV_DIR" 2>/dev/null; then
    ok "虚拟环境已创建（可访问系统包）"
  elif "$PYTHON_BIN" -m venv "$VENV_DIR" 2>/dev/null; then
    ok "虚拟环境已创建"
  else
    die "建虚拟环境失败。Debian / Ubuntu 上通常是缺了 venv 组件：

    sudo apt install python3-venv

装好后再跑一次这个脚本。"
  fi
fi

if ! "$PY" -c 'import flask' >/dev/null 2>&1; then
  if "$PY" -m pip --version >/dev/null 2>&1; then
    printf '  正在安装依赖（需要联网，只有 Flask 一个包）…\n'
    "$PY" -m pip install --upgrade pip -q 2>/dev/null || true
    if ! "$PY" -m pip install -r requirements.txt -q; then
      die "装依赖失败。可以换国内源重试：

    $PY -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

或直接用系统包：  sudo apt install python3-flask"
    fi
    ok "依赖已装好"
  else
    die "Flask 没装，而且虚拟环境里没有 pip。

装一下就行：  sudo apt install python3-flask

装好后再跑一次这个脚本。"
  fi
else
  ok "依赖已就绪"
fi

# ------------------------------------------------------------
# 3. 访问口令 + 端口（首次设置 / 以后改口令也在这里）
# ------------------------------------------------------------
mkdir -p "$DATA_DIR"
[ -f "$CONFIG" ] || echo '{}' > "$CONFIG"

read_cfg="$("$PY" - "$CONFIG" <<'PYEOF'
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    cfg = {}
pw = "yes" if (cfg.get("password_hash") or "").strip() else "no"
port = cfg.get("port")
host = "yes" if cfg.get("host", "0.0.0.0") == "0.0.0.0" else "no"
print(pw, port if isinstance(port, int) and 0 < port < 65536 else "", host)
PYEOF
)"
has_pw="${read_cfg%% *}"
rest="${read_cfg#* }"
CFG_PORT="${rest%% *}"
CFG_HOST_LAN="${rest#* }"

# 写口令哈希 / 端口 / 访问范围进 config.json（原子写）
save_setting() {   # $1=口令明文（空=不改口令）  $2=端口（空=不改端口）  $3=访问范围（lan/local/空=不改）
  PW="${1:-}" PRT="${2:-}" ACCESS="${3:-}" "$PY" - "$CONFIG" <<'PYEOF'
import hashlib, json, os, sys, tempfile

path = sys.argv[1]
pw = os.environ.get("PW") or ""
prt = os.environ.get("PRT") or ""
acc = os.environ.get("ACCESS") or ""

try:
    cfg = json.load(open(path, encoding="utf-8"))
except Exception:
    cfg = {}

if pw:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 390000, dklen=32)
    cfg["password_hash"] = "sha256:%s.%s" % (salt.hex(), dk.hex())
if prt:
    cfg["port"] = int(prt)
if acc == "lan":
    cfg["host"] = "0.0.0.0"
elif acc == "local":
    cfg["host"] = "127.0.0.1"

# 原子写：先写临时文件再换名，写到一半断电也不会留半个坏 JSON
d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".cfg-")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.flush(); os.fsync(f.fileno())
os.replace(tmp, path)
print("ok")
PYEOF
}

if [ ! -t 0 ]; then
  # 非交互式（管道 / 定时任务）：不问口令，但没口令 = 网站锁死，必须说清楚
  if [ "$has_pw" = "no" ] && [ -z "${ESSAY_PASSWORD:-}" ]; then
    warn "当前不是交互式终端，跳过设口令 —— 网站将整体锁定（提示去终端设置口令）。"
    warn "想设口令请在终端里跑：  sh setup.sh"
  fi
else
  # ---- 口令：首次设置 / 以后修改 ----
  if [ "$has_pw" = "no" ]; then
    printf '\n%s首次运行，请设置一个访问口令%s\n' "$Y" "$R"
    printf '  （进网页、改设置都要用它；忘了可以再跑一次 sh setup.sh 重设）\n\n'
    while :; do
      printf '  口令: '; read -r pw1
      if [ -z "$pw1" ]; then warn "口令不能是空的，重来。"; continue; fi
      printf '  再输一次: '; read -r pw2
      [ "$pw1" = "$pw2" ] || { warn "两次不一样，重来."; continue; }
      break
    done
    save_setting "$pw1" ""
    unset pw1 pw2
    ok "口令已设置"
  else
    printf '\n  口令已设置。%s直接回车保持不变%s；想改口令就输入新口令\n' "$B" "$R"
    printf '  新口令（回车=不改）: '; read -r pw1
    if [ -n "$pw1" ]; then
      printf '  再输一次: '; read -r pw2
      if [ "$pw1" != "$pw2" ]; then
        warn "两次不一样，口令没改。"
      else
        save_setting "$pw1" ""
        ok "口令已修改"
      fi
    else
      ok "口令保持不变"
    fi
    unset pw1 pw2
  fi

  # ---- 端口：记住上次选的，回车沿用 ----
  DEF_PORT="${PORT:-${CFG_PORT:-8083}}"
  printf '  端口 [回车 = %s]: ' "$DEF_PORT"; read -r prt
  if [ -n "$prt" ]; then
    case "$prt" in
      *[!0-9]*) die "端口要是数字：$prt" ;;
    esac
    [ "$prt" -ge 1 ] && [ "$prt" -le 65535 ] || die "端口范围 1~65535"
    PORT="$prt"
    save_setting "" "$PORT" ""
    ok "端口已记住：$PORT"
  else
    PORT="$DEF_PORT"
  fi

  # ---- 访问范围：局域网 / 仅本机 ----
  if [ "$CFG_HOST_LAN" = "yes" ]; then
    DEF_ACCESS="lan"
  else
    DEF_ACCESS="local"
  fi
  printf '\n  访问范围： 1 = 局域网（推荐）  2 = 仅本机  [回车 = %s]: ' "$DEF_ACCESS"
  read -r acc_choice
  if [ "$acc_choice" = "1" ] || [ "$acc_choice" = "lan" ]; then
    save_setting "" "" "lan"
    HOST="0.0.0.0"
    ok "局域网可访问"
  elif [ "$acc_choice" = "2" ] || [ "$acc_choice" = "local" ]; then
    save_setting "" "" "local"
    HOST="127.0.0.1"
    ok "仅本机可访问"
  else
    # 回车 = 保持上次的
    if [ "$DEF_ACCESS" = "lan" ]; then
      HOST="0.0.0.0"
      ok "局域网可访问（保持不变）"
    else
      HOST="127.0.0.1"
      ok "仅本机可访问（保持不变）"
    fi
  fi
fi

# 环境变量 PORT 优先于 config.json（临时换端口不用改配置）
[ -n "${PORT:-}" ] || PORT="${CFG_PORT:-8083}"

# ------------------------------------------------------------
# 4. 启动
# ------------------------------------------------------------
URL="http://localhost:$PORT"
printf '\n%s启动服务…%s\n' "$B" "$R"
printf '  网址: %s%s%s\n' "$G" "$URL" "$R"
printf '  停止: 按 %sCtrl + C%s\n\n' "$B" "$R"

# 起服务前试着开一下浏览器（headless / 服务器上没浏览器就跳过）
if [ "${OPEN:-1}" = "1" ] && [ -t 1 ]; then
  for b in xdg-open sensible-browser x-www-browser firefox chromium google-chrome; do
    if command -v "$b" >/dev/null 2>&1; then "$b" "$URL" >/dev/null 2>&1 & break; fi
  done
fi

# HOST 由交互式步骤或环境变量决定；非交互式时从 config.json 读
if [ -z "${HOST:-}" ]; then
  HOST="$("$PY" -c "import json; print(json.load(open('$CONFIG')).get('host','0.0.0.0'))" 2>/dev/null || echo 0.0.0.0)"
fi

ESSAY_DATA_DIR="$DATA_DIR" PORT="$PORT" HOST="$HOST" exec "$PY" app.py
