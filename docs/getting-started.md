# 运行与导入

本项目以源码运行。Python 3.10+ 即可启动人工演示和规范归档查看；此路径不需要安装第三方包。可选微信数据库与媒体功能另有依赖，见 [微信导入](wechat-import.md)。

## 人工演示

在源码目录执行：

```bash
python3 personal_world.py demo
```

这会导入 `examples/demo.json`、使用独立演示数据目录，并打开本机浏览器。示例的 `manifest.synthetic` 为 `true`，界面持续标注人工数据。已有真实数据目录不能被当作演示目录覆盖。

```bash
python3 personal_world.py demo --no-open
```

`demo --no-open` 只准备人工数据，不启动服务或浏览器。需要打开界面时，重新运行普通 `demo` 命令。默认端口是 `18792`，只在本机监听。请用 CLI 建立连接，不要把手动访问不带凭据的本机地址当作已经认证。

## 常用命令

以下参数应替换为你自己选定的位置；示例不含任何真实用户路径或密钥。

| 命令 | 作用 |
|---|---|
| `python3 personal_world.py demo` | 在独立目录导入并打开人工演示 |
| `python3 personal_world.py open` | 启动或重新打开个人工作空间 |
| `python3 personal_world.py open --no-open` | 启动本地服务而不自动打开浏览器 |
| `python3 personal_world.py serve` | 在当前终端前台运行本地服务 |
| `python3 personal_world.py import "/path/to/export.jsonl"` | 导入已有规范归档并打开界面 |
| `python3 personal_world.py doctor` | 检查运行环境、配置与可选依赖 |
| `python3 personal_world.py verify` | 核对归档与独立索引的一致性 |
| `python3 personal_world.py configure ...` | 配置已有微信数据库和可用密钥的位置 |
| `python3 personal_world.py refresh` | 根据已有配置重新建立本地快照与归档 |

`--state PATH` 可以放在子命令前或后。`open` 和 `serve` 可使用 `--port PORT`。`demo`、`import`、`refresh` 的 `--no-open` 表示完成数据处理后不启动服务；`open --no-open` 仍会启动或复用服务，只取消唤起浏览器。

## 默认数据目录

| 系统 | 默认位置 |
|---|---|
| macOS | 当前用户主目录下的 `Library/Application Support/AIMAN/PersonalWorld` |
| Windows | `%LOCALAPPDATA%/AIMAN/PersonalWorld` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/aiman-personal-world` |

默认演示目录在同一位置，以 `-demo` 为后缀。你也可以指定一个全新的演示目录；CLI 会拒绝把已有真实状态当作演示状态。

目录中的归档、索引、可用媒体、浏览器访问凭据及人工工作状态都属于私人数据。不要把目录放进源码仓库、公共同步目录、问题附件或发布 ZIP。

## 导入与更换数据

```bash
python3 personal_world.py import "/path/to/export.json" --state "/path/to/private-workspace"
python3 personal_world.py verify --state "/path/to/private-workspace"
python3 personal_world.py open --state "/path/to/private-workspace"
```

导入会生成新索引并进行检查，再替换原索引。空消息集、重复消息 ID、清单数量不一致或明确失败的导出不应被当作完整数据发布。使用同一工作空间重新导入时，先确认你选中了正确的来源与目录。

消息证据依赖稳定 ID。来自不同导出器的 ID 若改变，旧复核或草稿可能不再能定位原消息，应以当前证据是否仍存在为准，而不是把旧注释当成事实。

## 验证自己正在看什么

1. 看页面是否标注人工示例；演示内容不能当成个人数据导入成功。
2. 打开覆盖信息，查看消息类型、日期范围和附件状态。
3. 从候选进入证据，检查消息原文与上下文。
4. 检查“后续线索”提示的范围；它不是现实进展证明。
5. 用 `verify` 核对归档与索引，出错时保留原结果并检查报错类别。

终端、下载的导出文件和本地浏览器可能显示私人内容。分享问题报告时只提交人工复现数据及经过检查的状态信息。

## 环境与故障

不确定是否需要可选依赖时先运行 `doctor`。演示不需要微信密钥；看到密钥缺失提示时，不要把这解释成聊天记录为空。

端口被占用时先确认已有服务，或为自己的新服务显式选择另一个本地端口。不要将监听地址改成公网地址，也不要用端口转发分享真实个人工作空间。本版本没有面向多人或公网服务的权限模型。

关闭前台 `serve` 可以使用 `Ctrl+C`。其他启动方式请按当前 CLI 的状态提示管理已有进程；不要按名称批量终止无关程序。
