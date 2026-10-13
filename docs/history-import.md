# 历史补充、增量合并与恢复

## 在页面中使用

通过桌面启动入口打开本地工作空间。点击“数据来源与导入”，再点击“补充历史 / 查看导入批次”。选择本人有权使用的规范 JSON、JSONL 或 NDJSON 文件，点击“生成预览”，核对新增、重复、冲突和合并后范围，再点击“确认合并到本机”。

所选文件只传给回环地址上的本机服务，不上传云端。单批上限 512 MiB；更大的历史请分批。程序不能直接读取手机备份容器，也没有在页面中获取密钥或绕过客户端保护的功能。只有准备好的兼容归档能直接导入。

`primary` 表示当前账号、同一套稳定消息编号。选它时应明确核对账号与导出器。不同账号、不同导出器或不同编号体系请使用独立来源标识，例如 `old-export`。软件会给该来源的消息与会话加稳定前缀，不靠名字或相似文字猜测同一个人、同一条消息。

默认保留已存在的内容：旧文件能补上缺少的历史，但不能把新版本文字悄悄改回旧版本。选择“采用此次导入内容”才会更新同编号内容，旧内容仍在保留的规范输入快照中。相同消息编号若指向不同会话或时间，确认会被阻止；不能用更新策略绕过身份冲突。

预览本身不会替换当前聊天归档、索引、复核或草稿。预览文件会暂存在本机，最多同时保留四份待确认预览，有效期24小时；关闭导入窗口会尝试取消当前预览。关闭页面或浏览器意外退出后，可以在“月份覆盖与历史导入批次”中找到未确认预览并取消，也可用 `cancel-import` 明确清理。

## 命令行

先检查文件，再用返回的预览编号确认：

```sh
python3 personal_world.py import old-chat.jsonl --state /path/to/private-state --source-id primary --preview
python3 personal_world.py apply-import PLAN_ID --state /path/to/private-state
python3 personal_world.py cancel-import PLAN_ID --state /path/to/private-state
python3 personal_world.py history --state /path/to/private-state
```

`import` 不带 `--preview` 时按指定策略直接合并，默认仍为 `keep_existing`。`--policy prefer_incoming` 是明确采用输入版本；不要对来源和时效不确定的旧文件默认使用它。来源名称可用 `--label` 指定，只是本地显示文本。

已经配置微信本地数据库和可用密钥时：

```sh
python3 personal_world.py refresh --state /path/to/private-state --preview --no-open
python3 personal_world.py apply-import PLAN_ID --state /path/to/private-state
```

不带 `--preview` 的刷新会在完整核对后直接合并。刷新把已配置来源当作同一个 `primary` 账号的新快照，采用新快照内容；切换账号应使用独立工作目录，不要把另一账号的数据库直接配置进原账号目录。

## 哪一部分是增量

**合并和当前索引写入是增量，微信数据库采集仍是经过校验的完整快照。** 新快照仍须读取、解码并检查；输入验证索引不再重复构建全文搜索。发布索引通过 SQLite Backup API 复制当前一致版本，再只插入新增消息、更新发生变化的消息。全文搜索只更新新文字或变化文字，不把没有变化的消息重新插入搜索索引。

为保持可恢复和完整核对，规范集合文件仍完整生成，当前索引也仍需要一个独立副本。因此这不是“只读取新增数据库页”、常驻后台监听或零复制导入。没有启用后台定时任务。完全重复的输入确认后不会重写当前归档和索引。

新快照里没有出现的旧消息默认保留。**在微信里删除聊天，不会自动从 Personal World 的历史及来源快照中删除。** 本版尚无逐条遗忘、来源删除或到期清理功能。

## 原始来源与展示集合

当前 `archive.jsonl` 在首次合并后成为 `personal-world.collection.v1`：它保存统一展示字段，以及每条记录的来源批次和原编号。完整的规范输入（包括微信适配器的来源信息）分别保存在私人目录 `import-sources/<sha256>.jsonl`，不被后续导入覆盖。它们是经规范化的输入快照，不保证与用户选中的原 JSON 文件字节完全相同。

合并集合不能冒充某次微信数据库原始导出，所以不会伪造累计的逐库、逐表计数。原生微信输入先单独通过原来的逐表校验；集合再检查统一字段、搜索索引和保留的每条来源。`verify` 检查快照校验值，并逐条核对集合的来源对应。哈希用于检查内容一致，不证明消息内容真实，也不是签名或公证。

来源批次数量包含重叠，不能相加作为去重消息总数。页面月份覆盖以已导入的时间戳为准，日期使用北京时间；空白月份只表示没有已导入记录，不能推断这个月没有聊天。月份显示最多600个月，最近导入历史最多返回100项。

## 中断和并发

预览绑定生成时的当前归档及索引哈希。别的导入先完成后，旧预览不能覆盖它，必须重新检查。人工复核和草稿在独立工作数据库中，不跟随归档替换。

归档和索引替换前会写入持久发布日志并保留旧文件。中断后，下一次受控写操作或 `recover` 会核对两边哈希，完成已落盘的新版本或恢复旧版本；对不认识的外部改动会停止，不强行覆盖。更新标记存在时，数据读取会暂停而不是读取已知处于切换中的文件对。

```sh
python3 personal_world.py recover --state /path/to/private-state
python3 personal_world.py verify --state /path/to/private-state
```

合成测试包含进程在两个文件替换之间强制退出的情形，不等于所有操作系统、存储设备和真实断电情形已经审计。POSIX 使用既有权限控制；Windows ACL 与硬件的刷新保证仍有原来的平台边界。

实现参考：SQLite [Backup API](https://www.sqlite.org/backup.html)、[FTS5 外部内容表](https://www.sqlite.org/fts5.html#external_content_tables)、[原子提交说明](https://www.sqlite.org/atomiccommit.html)。本项目的双文件发布日志是应用层恢复机制，不把两个文件的重命名说成一次操作系统原子操作。
