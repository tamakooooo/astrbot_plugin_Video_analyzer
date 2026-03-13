---
name: video-analyzer
description: 当用户要分析 B站/抖音视频、生成结构化总结、并可同步发布到飞书知识库时使用。输入视频链接，调用 run.py 或 skill_main 执行完整流程（下载→字幕/必剪转写→LLM总结→图片渲染→飞书发布）。
homepage: https://github.com/tamakooooo/Video_analyzer
metadata: { "openclaw": { "emoji": "🎥", "requires": { "anyBins": ["python3", "python"], "bins": ["ffmpeg"] }, "primaryEnv": "OPENAI_API_KEY", "install": [ { "id": "brew", "kind": "brew", "formula": "ffmpeg", "bins": ["ffmpeg"], "label": "Install ffmpeg (brew)" } ] } }
---

# Video Analyzer

## 概述

本 skill 复用 `tamakooooo/Video_analyzer` 的核心能力，支持：
- B站视频总结
- 抖音视频总结（依赖 douyin-downloader）
- B站二维码登录（返回二维码图片路径，便于上层发送给用户）
- 抖音二维码登录（返回二维码图片路径，便于上层发送给用户）
- 必剪转写兜底
- 飞书知识库发布（默认开启）
- 总结图片渲染

## 何时使用

以下请求应触发本 skill：
- “帮我总结这个 B站视频”
- “分析这个抖音链接并给重点”
- “视频总结后发布到飞书知识库”

## 执行入口

优先使用 CLI：

```bash
python3 {baseDir}/run.py --url "<VIDEO_URL>"
```

首次运行建议先安装依赖：

```bash
python3 -m pip install -r {baseDir}/requirements.txt
```

或在代码中调用：

```python
from openclaw_main import skill_main
result = skill_main(url="<VIDEO_URL>")
```

## 抖音二维码登录（给用户发图）

### 第一步：生成二维码

```bash
python3 {baseDir}/run.py --action douyin_login_start --config ./config.json
```

返回字段中包含：
- `session_id`
- `qr_path`（发给用户）
- `debug_path`（排障图）

### 第二步：轮询登录状态

```bash
python3 {baseDir}/run.py --action douyin_login_poll --session-id "<SESSION_ID>" --config ./config.json
```

当返回 `login_status=success` 时，Cookie 已自动写入 `config.json`，可直接用于抖音总结。

## B站二维码登录（给用户发图）

### 第一步：生成二维码

```bash
python3 {baseDir}/run.py --action bili_login_start
```

返回字段中包含：
- `session_id`
- `qr_path`（发给用户）

### 第二步：轮询登录状态

```bash
python3 {baseDir}/run.py --action bili_login_poll --session-id "<SESSION_ID>"
```

当返回 `login_status=success` 时，Cookie 已自动写入 `data/bili_cookies.json`，后续 B站总结会自动使用。

## 参数说明（最常用）

- `url`：必填，B站/抖音视频链接
- `action`：`summarize|douyin_login_start|douyin_login_poll|bili_login_start|bili_login_poll`
- `--config`：配置文件路径，默认 `./config.json`
- `--session-id`：轮询抖音登录状态时必填
- `--note-style`：`concise|detailed|professional`
- `--download-quality`：`fast|medium|slow`
- `--no-feishu`：禁用飞书发布
- `--douyin-runner-path`：抖音下载器 `run.py` 绝对路径

## 典型命令

```bash
# B站：仅生成总结，不发飞书
python3 {baseDir}/run.py --url "https://www.bilibili.com/video/BV1uDAkzFEkM/" --no-feishu

# 抖音：指定 downloader
python3 {baseDir}/run.py \
  --url "https://www.douyin.com/video/xxxxxxxxxxxxxxx" \
  --douyin-runner-path "/opt/douyin-downloader/run.py" \
  --douyin-python "/mnt/AstrBot/.venv/bin/python"

# 抖音登录：获取二维码（把 qr_path 图片发给用户）
python3 {baseDir}/run.py --action douyin_login_start

# 抖音登录：轮询状态（登录后会写入 cookie）
python3 {baseDir}/run.py --action douyin_login_poll --session-id "<SESSION_ID>"

# B站登录：获取二维码（把 qr_path 图片发给用户）
python3 {baseDir}/run.py --action bili_login_start

# B站登录：轮询状态（登录后会写入 data/bili_cookies.json）
python3 {baseDir}/run.py --action bili_login_poll --session-id "<SESSION_ID>"
```

## 配置要求

在 `config.json` 中至少提供：

1) LLM 配置（用于总结）
- `llm.api_key`
- `llm.base_url`（可选，默认 OpenAI）
- `llm.model`

2) 飞书配置（发布知识库时）
- `feishu_app_id`
- `feishu_app_secret`
- `feishu_wiki_space_id`
- `feishu_parent_node_token`（可选）
- `feishu_domain`（`feishu` 或 `lark`）

3) 抖音支持（抖音链接时）
- `douyin_downloader_runner_path`（推荐 `/opt/douyin-downloader/run.py`）
- `douyin_downloader_python`（如 `/mnt/AstrBot/.venv/bin/python`）
- 可选 Cookie：`douyin_cookie_ttwid`、`douyin_cookie_odin_tt`、`douyin_cookie_ms_token`、`douyin_cookie_passport_csrf_token`、`douyin_cookie_sid_guard`

## 输出约定

返回结构化 JSON，关键字段：
- `success`：是否成功
- `note_text`：总结正文（Markdown）
- `note_image`：图片路径（若开启渲染）
- `feishu_publish`：飞书发布结果
- `error`：失败原因

## 故障排查

- 抖音失败且提示缺 cookie：先补全抖音 cookie 或执行扫码登录流程。
- 飞书发布失败：检查 app 凭据与 space_id 是否正确。
- 总结失败：检查 LLM key/base_url/model 是否可用。
