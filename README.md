# Codex Training Monitor

让 Codex 确认训练正常启动后结束当前回合，由普通后台程序等待训练完成或异常，再向原聊天发送结果事件。运行期间不调用模型；启动检查、事件续接和结果分析仍会正常使用 Codex 额度。

Python 3.11+，运行时只有标准库。分发版自带完整代码，不需要保留开发仓库或配置源码路径。Windows 桌面 App 原聊天唤醒已实测，其他系统的 App 原生桥暂不支持；SSH Linux 训练由 Windows 桌面端接收结果。

## 安装

需要 **Python 3.11+** 和兼容的 **Windows Codex 桌面 App**。先确认 `python --version` 能正常运行；不需要安装第三方 Python 依赖。

有 Git 的用户，在终端执行：

```powershell
git clone https://github.com/hupochun/codex-monitor.git
cd codex-monitor
python scripts/install_skill.py
```

没有 Git：点击 [下载源码 ZIP](https://github.com/hupochun/codex-monitor/archive/refs/heads/main.zip)，解压后进入 `codex-monitor-main` 文件夹，在该目录打开终端，执行 `python scripts/install_skill.py`。

安装器将完整 Skill 和 Python 程序复制到当前用户的 `$CODEX_HOME/skills/codex-training-monitor`（未设置 CODEX_HOME 时为 `~/.codex/skills/codex-training-monitor`）。安装后可以删除克隆或解压的源码目录，不影响运行；不需要修改路径配置，也不会安装系统服务或开机启动项。

## 使用

在 Codex 的新聊天中说：

> 使用 $codex-training-monitor，在这个项目中运行我的训练脚本，确认训练正常后结束回合。训练结束后回到本聊天，分析结果并继续评估。

提供训练命令、项目目录，以及需要在结束后执行的步骤。SSH 训练还需提供服务器地址，并提前配置可用的非交互式 SSH 登录。让 Codex 从桌面 App 内启动监控，才能继承原聊天的消息入口；安装命令可以在普通终端执行。

安装目录只包含 `SKILL.md`、`installation.json` 和 `scripts/`。`installation.json` 保存版本与文件校验值，不记录开发机源码路径。运行时根据安装位置定位代码；任务状态默认存放在 `~/.codex-monitor`，与安装包分离。

## 更新

保留了克隆目录的用户，在源码目录执行：

```powershell
git pull --ff-only
python scripts/install_skill.py --upgrade
```

使用源码 ZIP 的用户重新下载、解压，在新目录运行 `python scripts/install_skill.py --upgrade`。安装器先保留旧版本备份，检测到自行修改的程序或 Skill 时拒绝覆盖。任务运行期间不要升级或移动已安装的 Skill。

## 快速验证（不发送 Codex 消息）

```powershell
python scripts/demo_spec.py --output .test-state/demo.json
python -m codex_monitor --state-dir .test-state submit --spec .test-state/demo.json
python -m codex_monitor --state-dir .test-state wait-ready <返回的任务ID> --timeout 20
python -m codex_monitor --state-dir .test-state status <返回的任务ID>
```

以上演示命令供源码仓库开发验证使用。演示进程打印 step 和 loss，默认几秒后结束。`submit` 立即返回；后台 worker 在 Windows 隐藏运行，在 POSIX 独立会话运行。任务记录位于 SQLite，日志和结果位于 `<state-dir>/jobs/<任务ID>/`。不要在任务仍运行时移动其运行代码或删除状态目录。

## 接入训练

复制生成的 JSON，修改 `command` 和 `cwd`。命令必须是 argv 数组，不是 shell 字符串；例如 Python 应使用 `-u`，避免日志缓冲妨碍健康判断。`cwd` 必须是目标机器上的绝对路径。

| 字段 | 用途 |
| --- | --- |
| `thread_id` | 原聊天 UUID；省略时提交器读取 `CODEX_THREAD_ID`。不使用 `--last` |
| `command` | 训练程序及独立参数，例如 `["python", "-u", "train.py"]` |
| `health.pattern` | 匹配进度行的正则，包含命名组 `step`，可含 `loss` |
| `health.min_advances` | 认定健康前至少观察到的 step 递增次数 |
| `health.startup_timeout` | 启动检查限时；根据加载模型/数据所需时间设置 |
| `health.stall_timeout` | 进度停止推进的期限；0 禁用 |
| `max_runtime` | 总运行期限；0 禁用 |
| `follow_up` | 训练结束后已授权 Codex 执行的步骤 |
| `bridge.enabled` | 是否发送真实 Codex 消息；演示默认 false |

没有进度正则时只能说明进程已运行，不能证明训练健康。OOM 等错误通过非零退出码和日志报告；匹配到的 loss 为 NaN/Inf 时视为异常。超过所配置的健康/运行期限会尝试停止本任务进程树并报告，不会自动重训。

## 原聊天自动继续

默认 `bridge.mode: "auto"` 使用桌面 App 通过 `CODEX_APP_TOOLS_PIPE_PATH` 提供的本地消息接口，调用 `send_message_to_thread` 将结果发回原聊天。后台 worker 继承启动时的 App 环境。找不到这个入口时保留待投递状态，不会悄悄改成另一个 CLI runtime。

这是与本机 App 插件桥接协议的兼容适配，不是承诺长期稳定的公开 HTTP API。发送前会检查工具是否存在；App 更新、关闭或重启可能让管道失效。配置中的 `bridge.app_pipe` 可指定当前有效管道，但不能把过期地址当成永久入口。第一版原生桥针对 Windows 命名管道。

只有显式设置 `bridge.mode: "cli"` 才使用官方 CLI：`codex queue --thread <UUID> --message <事件> --resume`。此时可通过 `bridge.codex` 指定二进制路径，通过 `bridge.remote` 指定可访问的 App Server；需要认证时用 `bridge.remote_auth_token_env` 指定环境变量名，不能把 token 放进任务 JSON。

**消息排队成功不等于桌面聊天已经恢复。** 首次使用必须验证 CLI 与桌面原聊天连接的是正确服务。监控器将投递状态区分为：

- `submitted`：App 已接受消息，或 CLI 已排队，仍等原聊天确认。CLI 恢复警告会保留在状态中。
- `confirmed`：目标聊天执行事件内的 `ack-delivery` 命令，确认收到了事件。
- `retryable`：尚未发送消息请求，可安全重试。
- `uncertain`：发送超时、异常返回或发送进程中断，不能确定是否已经排队；不会盲目重发。
- `disabled` / `blocked`：已禁用或配置无法发送。

原聊天收到的消息带任务 ID、事件 ID、结果文件路径和确认命令。正常结果分析只需读取摘要及必要日志，不需要把整个训练日志放进上下文。

从源码安装配套 Skill：`python scripts/install_skill.py`，升级旧的路径引用版用 `python scripts/install_skill.py --upgrade`。新安装自带代码，不生成 `monitor-location.json`；升级前的完整旧目录保存在 `skill-backups` 下。源码模板统一维护在 `codex_monitor/skill_templates`。

首次真实测试可以生成 `--notify --thread <原聊天UUID>` 的演示配置，再提交；这会发送真实消息并触发模型。演示的后续指令只确认收到事件，不启动更多训练。

桌面端离线或休眠期间不能保证即时续接。已排队事件不应反复重发；恢复服务后先核实队列和聊天状态。正在执行的回合是否自动消费排队消息、空闲聊天是否自动启动，需要通过实际部署环境验证。

## SSH Linux 服务器

设置 `backend: "ssh"`、`ssh_host: "user@server"`，`cwd` 和 `command` 使用远端路径。可设置 `ssh_python`（默认 `python3`）。使用系统 OpenSSH 的 BatchMode，提前配置好密钥认证和 known_hosts。

远端 wrapper 独立于 SSH 连接存活；本地 worker 通过 SSH 获取增量日志和退出状态。这些轮询不调用模型。任务 ID 用于避免重试启动重复训练。网络中断不能证明远端训练已经结束；停止请求失败时必须保留这一不确定性。

WSL 可通过 WSL 内的 Python 运行本地模式，再配置可用的 Codex 桥接入口。本版不包含 Slurm 和云平台专用适配器，也不声称本地 Windows worker 可自动管理任意平台任务。

## 限制与恢复

- 单个 worker 管理单个训练任务；不修改 Codex 内部数据库或桌面配置。
- SQLite 保存状态，但不是操作系统服务管理器。主机重启或 worker 被强制杀死后的任务要核实进程/远端状态，不会自动重训。
- 普通网络检查不消耗模型 token，但 SSH、CPU、日志和 GPU 仍有各自成本。
- 不提供自动无限重训。后续动作仅来自提交时的授权内容。
- 协议没有通用发送幂等键，无法声称跨任意崩溃窗口严格 exactly-once；不确定的投递保留供核实。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m compileall -q codex_monitor scripts examples
```

测试使用假的 Codex 调用，不会自动发送消息。SSH 单元测试使用模拟传输；真实服务器需要单独端到端验证。见 `VALIDATION.md` 记录实际执行结果与尚未验证的边界。

官方命令语义：[codex queue](https://learn.chatgpt.com/docs/developer-commands?surface=cli#codex-queue)。
