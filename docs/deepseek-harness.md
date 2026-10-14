# DeepSeek Harness 原生插件 · 本机工作台版

包名 `@aiman-world/dsh-personal-world`，当前插件版本0.5.0（本机Python核心仍为0.4.0）；适配并锁定本机实际验证的 DeepSeek Harness 0.2.0-rc.2。没有为未经验证的版本提供兼容性豁免。

## 包含什么

这不是只有一份提示词或Skill的包。npm包包含完整Python本机服务、微信兼容归档适配器、聊天HTML、时间线、关系图、个人画像、日程、行动工作台、草稿、历史累积导入、验证程序、文档和手写示例。原聊天、密钥、附件缓存、个人画像状态、行程状态和机器配置不在包中。

Host入口使用真实Cordis `apply` / `inject` / `ctx.tools.register` / `ctx.webServer.register`；Client入口使用真实 `__ModuleLoader__` 与 `sidebar.footer.action`，显示“个人世界”。点击后打开本机模块导航，不侵入或猜测Harness的页面结构，也不关闭原工作台的鉴权或iframe保护。

八个模块：总览、微信历史与搜索、时间线、关系图、个人画像、日程与行程、行动工作台与草稿、数据来源与历史导入。模块在独立的本机浏览器页面中运行，使用原工作台的登录会话和人工确认流程。

## 模型权限边界

0.5.0已增加四个真正返回所选消息资料的原生工具：`personal_world_selection_info`、`personal_world_search_messages`、`personal_world_message_context`、`personal_world_timeline`。原有状态/导航工具仍不含私聊。

本人从工作台导出一个会话，在“选择聊天交给Agent”页面预览日期和条数后建立选区。每次模型读取经Harness原生approval服务授权；无授权不返回消息。Agent可据此做需求分析、约定变更核对及证据引用。

**不是原始微信库直连：本版只读取本人明确选择并提交给插件内存的导出范围。** 无全库默认开放、无任意文件或SQL、无自动发消息或写外部日历。导入前文件仅在本机浏览器预览，Agent读出的内容可能进入当前模型服务商和DSH日志。撤销不能抹去已披露内容。完整步骤、上限、宿主自动审核边界和测试见[Agent所选私聊说明](agent-selected-chat.md)。

## 安装与启动

安装无网络运行时依赖、无postinstall/prepare脚本的本地发布包：

```sh
dsh plugin --profile web add /path/to/aiman-world-dsh-personal-world-0.5.0.tgz --ignore-scripts
```

插件需要Python3.10+运行打包的本机应用。首次体验应使用手写示例，不要覆盖自己的真实归档：

```sh
python3 /path/to/installed/package/personal_world.py demo --port 18792
```

已有归档使用 `open --state /path/to/owned/state --port 18792`；这是打开独立归档，不是重新取钥、破解或读取腾讯服务器。若同一状态目录已有其他版本的服务，原启动器会要求先正常停止它，插件不会自动杀进程或绕过版本检查。

默认工作台地址 `http://127.0.0.1:18792/`。不同端口可在插件Cordis配置中设置 `workspaceUrl`，或通过 `PERSONAL_WORLD_URL` 启动环境设置。只允许纯loopback HTTP根地址；不接受远程主机、用户名密码、查询参数或任意路径。配置不包含数据库密钥或个人数据。

卸载使用官方包管理入口 `dsh plugin --profile web remove @aiman-world/dsh-personal-world`。卸载插件不删除私人数据，也不改变已导出的文件和外部日历副本。如果桌面启动入口指向已安装包，卸载前先正常停止该包的本机服务、把入口改回保留的独立源码版本；版本不匹配时不要强行复用服务。此次本机安装前后的配置和桌面入口另有私人备份。

## 日程验收的实际边界

本轮已有规则模块对真实本机归档的汇总读取成功：这仅证明读取链路工作，不证明候选已成立。逐项原文核对仍受工具调用限制，不能把缺时间的候选标为已确认日程。插件没有用另一个模型、隐藏数据通道或批量授权绕过这一限制。

没有执行真实日历写入、发邀请或订票。日程页仍要求本人核对关系、日期、时间、地点及可能取消改期后确认。相对日期按原消息时间解释，缺结束时间不默认补一小时，未确认记录不能导出为正式安排。

## 测试

`node --test dsh-plugin/test/*.test.js` 检查真实入口形状、原生工具注册、侧栏槽位、页面链接、禁止远程/跨源访问、受限健康探测、完整核心文件和卸载清理。Python及浏览器用例继续使用手写数据。安装验证与服务验证需分别报告；源码测试通过不等于插件已经在当前Harness进程中加载。

客户端声明等待官方renderer/sidebar模块及slots服务后再注册入口，避免宿主尚未准备好时静默跳过。完整安装包没有修改DeepSeek Harness内核，也没有绕过其插件加载机制。
