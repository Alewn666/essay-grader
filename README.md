# AI 作文批改

拍照上传学生作文，AI 按《中考作文评分标准(60 分)》给出分数、档次、逐段批注与修改后全文，支持追问并一键下载 TXT 报告。本仓库为 v1.0.3。

> 本仓库由 NAS 归档包整理发布。已剥离运行时数据、密钥、审计日志与大型二进制镜像；
> 文档中的真实口令与内网 IP 已脱敏。镜像请用仓库内构建脚本生成，或从 Releases 获取。

## 目录结构

```
├── essay-grader-v1.0.3.zip
├── sthing/
├── 发布版/
├── sthing/.gitignore
├── sthing/LICENSE
├── sthing/README.md
├── sthing/app.py
├── sthing/doc_text.py
├── sthing/docs/
├── sthing/docx_text.py
├── sthing/requirements.txt
├── 发布版/linux/
```

## 使用

请参考仓库内的说明文档与构建/部署脚本；首次运行前把示例地址、口令改成自己的值。

## 安全约定

- `data/`、`.env`、`env.*`、`*.pem`、`*.keystore`、`config.json`、`*.db`、`*.log` 均已在 `.gitignore` 中排除，切勿强制提交。
- 文档里的 `192.168.1.100` 与 `203.0.113.x` 是示例地址，`<REDACTED_PASSWORD>` 是脱敏占位符。
- 涉及管理后台的服务请只在可信局域网内使用。

## 许可证

MIT
