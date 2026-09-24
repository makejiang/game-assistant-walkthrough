# 游戏助手路书使用说明

> 此工具仅作演示用途，仅限个人使用，请勿商用。

## Skill 支持的游戏类型

### 核心功能针对3D第一/第三人称游戏进行了专项调优，表现更卓越。

## 1. 功能简介

- 自动部署本地游戏助手服务。
- 检测当前游戏进程。
- 自动下载并整理图文攻略（支持请求失败自动重试）。
- 将攻略导入本地游戏助手服务。
- 启动桌面攻略窗口，支持按游戏名直启或自动检测游戏。

参考素材与演示：

| 类型 | 链接 |
| --- | --- |
| 验证素材《识质存在》 | https://www.bilibili.com/video/BV1Ag7V6REP8 |
| Skill 使用演示《识质存在》 | https://www.bilibili.com/video/BV1rQbS6QEGe |
| 游戏中效果《识质存在》 | https://www.bilibili.com/video/BV1iw7G63EGQ/ |

## 2. 环境要求

- Windows。
- Python 3.9 或更高版本。
- `tkinter` 通常随 Windows Python 自带；如使用精简 Python 发行包，请确认 tkinter 可用。

安装依赖：

```powershell
pip install requests beautifulsoup4 pillow keyboard py7zr modelscope
```

## 3. 第一次使用：准备游戏助手服务

下载攻略和打开攻略的命令内置服务健康检查，服务未运行时会自动部署。

部署时会下载 AI 模型（首次约几十 GB），模型下载策略由 SKILL.md 的 `models` 字段控制：

- `all`：下载全部模型（LLM / Embedding / Rerank / MMR / ASR / OCR / Action / VLM / Splitter）。
- `skip`：跳过模型下载（视觉识别不依赖这些模型，可放心跳过）。
- `<模型名列表>`：只下载指定模型，如 `MMR,LLM`。

服务安装目录默认在 `%LOCALAPPDATA%\GameAssistant`。若 SKILL.md 声明了 `install_dir`（预装服务目录），则改在该路径部署：跳过服务包下载，模型按 `models` 策略补齐；该路径会被校验，不存在或无服务端 exe 会报错。

部署时弹出部署进度窗口，智能体会自动检测就绪。部署进度窗口显示完成倒计时后会自动关闭，并在服务就绪后自动检查并启用 Vision 视觉服务。

不需要时可输入"关闭服务"或"关掉服务"停止后台服务。

## 4. 下载攻略

### 指定游戏名

```text
下载识质存在攻略
```

会后台下载并导入该游戏攻略，智能体自动轮询进度并通知完成。已经下载过的攻略不会重复下载。

### 不指定游戏名

```text
下载攻略
```

智能体会尝试检测当前正在运行的游戏，再下载对应攻略。

### 下载失败重试

网页请求或图片下载失败时，会自动重试 10 次，每次间隔 5 秒。全部失败后下载任务中止。

## 5. 打开攻略

### 指定游戏名

```text
开启识质存在攻略
```

会直接打开攻略窗口，识别当前画面场景并展示对应攻略内容。

### 不指定游戏名

```text
开启攻略
```

如果有游戏正在运行，智能体会先检测游戏名，再打开攻略窗口。游戏退出时攻略窗口也会自动退出。

## 6. 关闭服务

在智能体中可输入：

```text
关闭服务
```

或：

```text
关掉服务
```

智能体会调服务关闭接口优雅关闭，失败则强制关闭。

如果用户只说"不玩了""退出""关掉"等模糊表述，智能体会提示："如果需要关闭后台服务，请说'关闭服务'或'关掉服务'"。

## 7. 目录说明

服务安装及日志目录（默认路径；若 SKILL.md 声明了 `install_dir`，则安装目录、日志和状态文件都在该路径下，仅当该路径不可写时才回落到此默认目录）：

```text
%LOCALAPPDATA%\GameAssistant
```

日志文件：

| 文件 | 用途 |
| --- | --- |
| `log\service_manager.log` | 服务管理操作日志（覆盖写入） |
| `log\deploy\deploy.log` | 服务部署过程日志 |
| `log\deploy\stdout.log` | 部署后台任务 stdout（覆盖写入） |
| `log\download\stdout.log` | 攻略下载后台任务 stdout（覆盖写入） |
| `task_status.json` | 后台任务状态，供智能体查询 |
| `game_client.pid` | 攻略窗口进程 PID |

项目目录下可能生成：

- `walkthrough\`：下载的攻略和中间文件。
- `logs\game_client-YYYY-MM-DD.log`：攻略窗口运行日志（按日分割）。
- `detected_processes.json`：进程名到游戏名的映射。

## 8. 目录结构

```text
game-assistant-walkthrough
├─ SKILL.md
├─ README.md
└─ scripts/
   ├─ service_manager.py              # 服务管理（部署/健康检查/关闭/下载/状态查询）
   ├─ deploy.py                       # 服务部署脚本
   ├─ game_client.py                  # 桌面攻略悬浮窗
   ├─ game_detection.py              # GPU 游戏进程检测
   ├─ game_walkthrough_downloader.py  # 游民星空攻略下载
   ├─ walkthrough_service_importer.py # 攻略导入知识/视觉服务
   ├─ download_and_import_walkthrough.py # 下载 + 导入编排（headless）
   ├─ download_with_progress.py       # 下载进度弹窗包装器
   ├─ detect_game_with_retries.py     # 游戏检测（含重试）
   └─ append_detected_process.py     # 进程→游戏名映射写入
```

## 9. 常见问题

### 9.1 部署后端口不通

弹窗显示完成但智能体检测到端口不可达，智能体会标为错误并结束。请查看 `%LOCALAPPDATA%\GameAssistant\log\deploy\deploy.log` 排查，或重新部署。

### 9.2 模型下载失败

常见原因是网络波动或 ModelScope 临时限流。部署脚本会自动重试；如果最终失败，可直接关闭当前弹窗，并重新执行之前的指令。若多次重试仍失败，请查看 %LOCALAPPDATA%\GameAssistant\log\deploy\deploy.log 排查具体原因。

### 9.3 检测不到游戏进程

- 确认游戏已启动并进入 3D 渲染场景。
- 检查权限是否一致，例如游戏以管理员权限运行时，Python 也可能需要管理员权限。

### 9.4 下载攻略时长时间无响应

网页或图片请求会自动重试 10 次（间隔 5 秒）。如果全部重试后仍失败，下载任务会中止，智能体会通知下载失败。此时可重试下载，已有页面会断点续传不重复下载。

### 9.5 后台任务状态异常

后台任务的状态文件位于 `%LOCALAPPDATA%\GameAssistant\task_status.json`，每次新任务启动时会自动清理。如果状态一直不正常，可直接删除该文件后重试。

## License

This Skill is licensed under the MIT License. The complete license text is included in [LICENSE](LICENSE).

SPDX-License-Identifier: MIT
