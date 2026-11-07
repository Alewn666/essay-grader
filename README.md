# 📝 AI 作文批改

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)
![Release](https://img.shields.io/github/v/release/Alewn666/essay-grader?label=release)
![Flask](https://img.shields.io/badge/%E4%BE%9D%E8%B5%96-%E4%BB%85%20Flask-4B8BBE)

> 拍一张作文照片，就能拿到一份按《中考作文评分标准（60 分）》写成的批改报告：分数、档次、逐段批注、修改后全文与提分建议。一条命令启动，第三方依赖只有 Flask。

拍照上传或拖入文档，系统先提取正文（识别错了可以在界面里手动改），再交给 AI 按中考评分维度逐项批改。结果不是一句「写得不错」，而是能直接给学生看的东西——每一段哪里好、哪里该改，以及一版改好的全文。学生还能就同一篇作文继续追问，最后一键下载 TXT 报告。

这个项目是按「老师或家长在家自己用」做的：不需要数据库、不需要云服务账号、不需要手写配置，双击脚本就能跑。首次启动会问一次访问范围（局域网 / 仅本机），连同口令和端口一起记住。

## 它能做什么

| 环节 | 具体表现 |
| --- | --- |
| 输入 | 拍照上传、拖拽文档；`docx_text.py` / `doc_text.py` 用标准库 `zipfile` + `ElementTree` 解析 Word，无需安装 Office |
| 识别 | 先提取正文，界面里可人工修正后再提交，避免识别错字直接带进评分 |
| 批改 | 按 60 分制给出总分与档次、逐段批注、修改后全文和提分建议 |
| 追问 | `/api/essays/<id>/ask`、`/qa` 支持围绕同一篇作文继续提问 |
| 流式改写 | `/revise_stream` 边生成边显示，`/revise_cancel` 可随时中断，长文不用干等 |
| 导出 | `/report.txt` 一键下载 TXT 批改报告 |
| 更新 | `/api/update/check`、`/api/update/apply` 内置版本检查与在线更新 |

## 几个刻意的设计

- **依赖极简**：第三方包只有 `flask`，哈希、ZIP、XML、HMAC、流式 JSON 全部走 Python 标准库，装得快、离线也好维护。
- **默认锁定**：不设访问口令时网站整体锁定（API 返回 403、页面弹遮罩），只提示「请在终端运行 `sh setup.sh` 设置口令」，不存在「忘记配置谁都能进」的状态。口令经 PBKDF2-SHA256 **39 万轮**派生后落盘，不存明文；登录连错 5 次锁 30 秒。
- **配置可记忆**：首次启动问一次口令、端口和访问范围，之后回车即沿用；选择写进 `data/config.json`，`PORT` 环境变量可临时覆盖。
- **触屏优先**：手机、平板、电脑浏览器都能用，界面针对「拍照 → 交作业」这个动作做过适配。

## 快速开始

```bash
cd sthing
sh setup.sh
```

第一次运行会依次：建虚拟环境并装 Flask → 设置访问口令（输两遍）→ 选端口（回车即 **8083**）→ 启动并自动打开浏览器。之后再跑同一条命令，依赖与配置直接跳过。

```bash
PORT=9000 sh setup.sh        # 临时换端口，优先级高于记住的值
OPEN=0 sh setup.sh           # 不自动打开浏览器
```

想对整个局域网开放，在首次启动时选 `1 = 局域网（推荐）`，或改 `data/config.json` 的 `host` 字段（`0.0.0.0` = 局域网、`127.0.0.1` = 仅本机）；`setup.sh` 会读这个字段并传给服务。

想绕开脚本也可以直接起服务：

```bash
cd sthing
pip install -r requirements.txt
ESSAY_PASSWORD='你的口令' HOST=127.0.0.1 PORT=8083 python3 app.py
```

这种启动方式下 `HOST` / `PORT` / `ESSAY_PASSWORD` 环境变量直接生效；口令优先级为 `ESSAY_PASSWORD` > `config.json` 里的 `password_hash`。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8083`（或上次记住的值） | 监听端口，环境变量优先级最高 |
| `HOST` | `0.0.0.0`（app.py 默认） | 直接运行 `app.py` 时生效；用 `setup.sh` 时由 `config.json` 的 `host` 决定 |
| `ESSAY_PASSWORD` | — | 访问口令；也可由 `setup.sh` 交互式设置 |
| `ESSAY_DATA_DIR` | `./data` | 作文、报告与 `config.json` 的存放目录 |
| `OPEN` | `1` | 设为 `0` 不自动打开浏览器 |

## 目录结构

```
essay-grader/
├── sthing/                  # 完整可运行源码
│   ├── app.py               # Flask 服务（约 2500 行，含全部 API）
│   ├── setup.sh             # 一键启动：venv / 口令 / 端口 / 浏览器
│   ├── doc_text.py          # 正文提取
│   ├── docx_text.py         # docx 解析（zipfile + ElementTree）
│   ├── stream_json.py       # 流式响应解析
│   ├── templates/ static/   # 前端页面与样式
│   ├── test_grader.py       # 批改接口测试
│   ├── test_setup_pty.py    # 首次设置流程的 PTY 交互测试
│   ├── ui_test.py           # 界面测试
│   └── docs/                # 两轮检查与修复记录
├── 发布版/linux/            # 早期 deb 安装包
└── essay-grader-v1.0.3.zip  # 成品归档（见 Releases）
```

## 测试

```bash
cd sthing
python3 test_grader.py       # 批改接口
python3 test_setup_pty.py    # 模拟终端跑一遍首次设置流程
python3 ui_test.py           # 界面回归
```

## 说明与边界

- 批改质量取决于所接模型的返回；评分标准写在提示词里，可按自己的教学要求调整。
- `sthing/README.md` 与 `docs/检查与修复记录_第一轮_开源版剥离.md` 是早期归档文档，其中「默认绑 `127.0.0.1`、`HOST=0.0.0.0 sh setup.sh` 才对外」的描述**已过时**；当前以 `setup.sh` 的交互选择与 `app.py` 为准。
- `data/`、`.env`、`*.pem`、`*.db`、`*.log` 已在 `.gitignore` 中排除，公开仓库不含真实凭据；文档里的 IP 与口令均为示例值。
- 超大型成品镜像未纳入仓库（GitHub 单文件上限），请从 [Releases](https://github.com/Alewn666/essay-grader/releases) 获取。

## 版本

当前 **v1.0.3**（见 `app.py` 的 `__version__`）。早期归档 tag `essay-grader`（v1.0.0）保留不动，避免已分享的链接失效。

## 许可证

MIT — 见 [LICENSE](LICENSE)。本项目由 NAS 归档包整理后公开发布。
