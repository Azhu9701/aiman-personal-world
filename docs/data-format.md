# 规范聊天归档格式

本地应用接收 JSON 或 JSONL。格式只保存可解释的消息与来源信息，不要求用户在归档里加入数据库密钥。完整人工示例见 [examples/demo.json](../examples/demo.json)。

## JSON 与 JSONL

JSON 文件包含 `manifest`、`conversations` 和 `messages` 三个主要字段。也兼容单会话 `conversation` 加 `messages` 的导出形式。JSONL 每个非空行是一个对象，使用 `record_type` 区分：

```json
{"record_type":"conversation","id":"demo:format:chat","name":"人工格式示例","is_group":false}
{"record_type":"message","id":"demo:format:message:001","conversation_id":"demo:format:chat","sender_name":"虚构发送者","is_self":false,"type":"text","text":"这是一条手写格式示例。","timestamp":1704067200}
{"record_type":"manifest","synthetic":true,"label":"人工格式示例","complete":true,"message_count":1,"conversation_count":1,"failed_databases":[],"failed_tables":[]}
```

这是格式示例，不能复制 `synthetic: true` 到真实归档中。本项目自己的微信导出器将清单写在最后；媒体增强器要求这种顺序，以保证最终统计覆盖全部记录。

## 会话

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | 非空字符串 | 来源范围内稳定的会话标识 |
| `name` | 字符串 | 显示名；未知时可用稳定标识，不丢弃记录 |
| `is_group` | 布尔值 | 是否群聊 |
| `avatar_url` | 可选字符串 | 仅允许本机 `/api/media/<单个文件名>` 引用 |

索引根据导入消息重新计算 `last_message`、`last_message_at` 和 `message_count`，不把输入摘要当作数量依据。没有显式会话记录的消息可建立标识形式的会话；建议导出器始终写出会话记录，以保留可读名称。

## 消息

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | 非空、全局唯一字符串 | 最重要的证据标识；同一来源记录重新导出时保持稳定 |
| `conversation_id` | 非空字符串 | 所属会话标识 |
| `sender_name` | 字符串 | 归档中可确定的发送者显示名；未知可为空 |
| `is_self` | `true` / `false` / `null` | 本人发出、他人发出或方向未知；不要猜测 |
| `type` | 字符串 | 如 `text`、`image`、`voice`、`video`、`file`、`sticker`、`system`、`quote`、`card` |
| `text` | 字符串 | 可解释的正文、引用内容或类型说明；不放原始下载凭据 |
| `timestamp` | Unix 秒 | 推荐整数；未知为 `0`，不能当作真实时间事件 |
| `raw_type` | 可选标量 | 来源类型号或类型标识，用于适配审计 |
| `media_url` | 可选字符串 | 仅允许本机媒体缓存引用 |
| `file_name` / `file_size` | 可选字符串 / 整数 | 展示用附件名和已发布可读文件大小 |
| `media_status` | 可选字符串 | 可用、缩略图、首帧、未找到或不支持等状态 |
| `decode_status` | 可选字符串 | 导出器的正文解码状态，保留在来源归档与统计中 |

导入接口还接受毫秒时间戳和 ISO 8601 时间文本；没有时区的 ISO 文本按北京时间解释。新导出器应直接使用 Unix 秒，避免不明确的时区或单位。排序使用时间和消息 ID，同秒的 ID 必须能稳定排序。

微信适配器将数据库、表和原始记录身份纳入消息 ID，避免不同分片或会话重复行号冲突。应用将 ID 视为不透明字符串。不要用显示名、某段正文或单独的本地行号替代全局身份。

`sender_id` 等额外来源字段可以留在原归档，但当前浏览索引不保证保存所有扩展字段。图谱人物节点仍按会话和显示名划分，不能因为导出含内部发送者字段就宣称全局身份已消歧。

## 清单不是一个“全都完成”的开关

| 清单字段 | 正确含义 |
|---|---|
| `message_count` / `conversation_count` | 声明的导出数量；导入时实际重数 |
| `complete` | 导出器针对其声明来源范围完成行覆盖检查；不等于全设备或全历史覆盖 |
| `content_complete` | 导出器是否仍有正文解码限制；与行覆盖、媒体可打开分别判断 |
| `failed_databases` / `failed_tables` | 失败来源；非空或明确 `complete: false` 的导出不能替换完整索引 |
| `source_tables` | 微信适配器逐表的预期数量与导出数量 |
| `decode_status_counts` | 正文解码成功、仅元数据、缺失、修复或不支持等分类 |
| `media_enrichment` | 已有媒体增强的分类数量、缓存状态及范围 |
| `synthetic` / `label` | 人工示例的明确标记与说明，仅人工数据使用 |
| `notes` | 人类可读的来源与限制说明，不应包含密钥或下载凭据 |

通用 JSON 导入兼容没有清单的用户导出；此时只能验证实际输入与新索引的一致性，不能替来源声明“全量”。微信适配器输出的更强来源清单，需要使用其逐表审计路径核对。

`metadata_only` 可能表示保留了图片、语音、卡片等消息的可解释说明，不应改记成“正文完整解码成功”。媒体增强成功也不应擦除此前的正文限制。

## 媒体与安全

有效引用形如 `/api/media/<缓存文件名>`，不接受远程 URL、绝对本机路径、目录穿越或嵌套目录。缓存文件实际位于当前私人状态目录下的 `media/`。仅导入 JSON 不会自动把别处文件搬入缓存，也不会下载远程媒体。

归档不应包含 SQLCipher 密钥、图片 AES 密钥、CDN 下载令牌或原始带凭据 XML。格式适配器应提取用户可读字段，并把不能恢复的部分标注为未知或不支持。来源范围、未解析表和未知方向应保留，不能为了展示好看而删掉记录。

## 验证

```bash
python3 personal_world.py import "/path/to/export.jsonl" --no-open
python3 personal_world.py verify
```

新索引通过检查后发布。空消息、重复消息 ID、清单数量不一致和明确失败的导出会被拒绝；不把导入失败解释成“没有记录”。对可选微信来源，验证还比较逐表清单和分类状态。验证只能覆盖选定输入及其声明范围，不能证明某段消息在现实中真实发生。

实现依据：[server.py](../server.py)、[sync_wechat.py](../sync_wechat.py)、[verify_archive.py](../verify_archive.py)。

## 累积集合格式

v0.2.0 引入由程序生成的 `personal-world.collection.v1`。当前消息仍有原来的统一12字段；额外 origin 指向本机保留批次及原消息编号。来源批次包括 source_id、archive_sha256、输入数量和日期范围，不能相加当作去重总量。用户导出的集合若作为单个文件再次导入，只按该文件提供的统一内容核对，不把其中的原批次声明当作已核实的外部证据。详见 [历史导入](history-import.md)。
