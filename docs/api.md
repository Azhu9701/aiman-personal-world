# 本地 API · v0.1.0

服务由 `personal_world.py` 启动，默认地址为 `http://127.0.0.1:18792`。以下接口面向同一台电脑上的个人工作空间；没有公网、多租户、云模型或第三方消息发送接口。

## 连接与认证

先通过 CLI 的 `demo` 或 `open` 进入界面。启动凭据保存在私人状态目录的 `personal-world-access.json` 中；服务为每个状态目录使用独立的 `personal_world_session_<state_id>` HttpOnly、SameSite=Strict Cookie。

在已连接页面中执行同源 `fetch` 即可携带 Cookie。不要把凭据放进公开代码、终端历史、问题附件或共享链接。除启动入口外，数据 API 需要有效的本机会话。

所有写请求还必须具有与本地服务完全相同的 `Origin`，并使用 `Content-Type: application/json`。跨源请求、分块传输或超过 32 KiB 的写请求不被接受。主机检查与 Cookie 是本机访问边界，不是面向互联网的用户权限体系。

## 查询接口

日期参数 `from`、`to` 使用 `YYYY-MM-DD`，按北京时间解释，包含所选结束日期。时间戳响应为 Unix 秒。消息 ID、候选 ID、图节点 ID 和分页游标应作为不透明字符串使用，不从显示名重新生成。

| 方法与路径 | 参数 | 主要响应 |
|---|---|---|
| `GET /api/world/overview` | 无 | `ready`、`demo`、`counts`、`analysis`、人工状态汇总 `reviews`、`notes` |
| `GET /api/world/opportunities` | `q`、`from`、`to`、`limit`（默认 50，最多 500）、`status` | 候选 `items`、命中与实际分析数量、截断标记、方法与范围说明 |
| `GET /api/world/timeline` | `q`、`from`、`to`、`limit`（默认 100，最多 300）、`cursor` | 消息 `items`、`has_more`、`next_cursor`、范围说明 |
| `GET /api/world/graph` | `q`、`limit`（节点数，默认 80，8—160） | `nodes`、`edges`、命中与分析数量、`truncated`、范围说明 |
| `GET /api/world/evidence` | `id`（消息 ID）、`radius`（默认 4，1—25） | 原消息 `message`、`conversation`、同会话 `context`、`fingerprint` |
| `GET /api/world/coverage` | 无 | `total_messages`、`by_type`、`media_status`、`date_range`、`notes` |
| `GET /api/world/drafts` | 无 | 已保存本地草稿 `items` 与范围说明；最多返回 500 项 |

候选和图谱默认使用公开写在源码中的机器人词表。其他查询按空格、逗号或分号分词，最多使用 12 个词，组合为包含匹配。时间线可清空查询以浏览全部导入记录。这不是向量或语义搜索。

### 概览

`analysis.method` 为 `local_rules`；`semantic_search`、`cloud_upload`、`automatic_actions` 和 `at_rest_encryption` 为 `false`。`demo` 来自归档清单中的 `synthetic: true`，不依据消息内容猜测。

`counts.media` 统计归档中带有本地媒体引用的消息。它不是媒体文件的完整恢复证明；使用覆盖页及 `verify` 检查实际缓存和状态。

### 候选

每个候选是**同一会话、同一规则主题、固定 30 天窗口中的讨论片段**，不等于一个已识别的真实项目。关键字段为：

| 字段 | 含义 |
|---|---|
| `id` | 当前规则分组对应的稳定候选标识 |
| `conversation_id`、`conversation_name` | 证据所属会话 |
| `anchor_id`、`evidence_ids` | 锚点消息及可回看的支持或反例消息 ID |
| `signals` | 命中的需求、供给、合作、可能推进等规则；每项包含 `message_id` |
| `topics` | 词语命中的主题标签 |
| `score`、`score_label` | 固定规则的排序值及说明；不是成功率、可信度或个人评分 |
| `follow_up` | 后续线索状态、说明及最近相关时间；项目归属仍待核对 |
| `possible_progress` | 是否出现完成、取消或推进措辞；仍需检查消息上下文 |
| `status`、`review_note` | 本人的人工工作状态及备注 |
| `review_stale` | 已保存复核对应的证据版本是否与当前候选不一致；有历史复核时出现 |
| `missing` | 当前仍需人工核实的现实条件 |
| `source_fingerprint` | 用于比较候选证据版本的摘要，不能证明原消息真实 |
| `related_leads` | 最多 3 个跨会话需求/供给线索，附各自锚点证据；兼容性与当前状态未核实 |

`follow_up.state` 可能为 `later_related_evidence`、`no_later_match` 或 `recent`。同会话中的交付、取消、电话或线下沟通措辞会作为需要核对的后续语境，即使没有重复机器人关键词。`no_later_match` 只表示当前导入范围和规则未匹配到后续内容；不能解释成“没有推进”。观察间隔相对于归档中最后消息时间计算，不代表实时世界状态。

跨会话线索必须共享至少一个具体规则主题，只共享泛泛的“机器人”主题不会关联。每个其他会话最多列出一个讨论片段；它不是产能、价格或项目兼容性匹配证明。

候选引擎最多读取最近 50,000 条主题命中和 50,000 条后续语境命中，并保留至多 5,000 个分组候选。`truncated` 明示达到上限的情况。日期与状态过滤作用于返回候选；同一讨论片段的反例证据可能在选定日期以外，因此不要把过滤后的候选列表当成该日期区间的完整统计。

### 时间线、图谱与证据

时间线按消息时间和稳定 ID 倒序分页。继续使用返回的 `next_cursor`，不要用显示时间自行拼接游标。单条消息含原归档字段、`conversation_name`、规则 `tags` 和等于消息 ID 的 `evidence_id`。

图节点类型为 `conversation`、`person`、`topic`。人物节点仅表示某会话中的发送者显示名范围，尚未完成真实身份消歧；同会话内的同名仍可能有歧义。连线只表示在会话发言或在消息中提及主题，不证明实际合作、雇佣或能力。图谱最多处理最近 2,000 条命中，每条边列举至多 8 个消息 ID，边的 `count` 可以大于列举的样本数。节点或消息截断时返回 `truncated: true`。

证据接口返回锚点及其前后同会话消息。`fingerprint` 是规范消息内容的摘要，只用于一致性比较；它不是发送者签名、时间戳公证或来源真实性认证。

## 人工工作状态与草稿

| 方法与路径 | JSON 请求 | 作用 |
|---|---|---|
| `POST /api/world/review` | `id`、`source_fingerprint`、`status`、`note` | 保存本人对刚查看的候选版本的状态与备注 |
| `POST /api/world/draft` | `id`、`source_fingerprint` | 根据刚查看的候选证据版本生成并保存固定模板草稿 |

允许的复核状态为 `needs_review`、`follow_up`、`dismissed`、`completed`，备注最多 4,000 字符。人工选择 `completed` 只是一条带证据版本的注释，不会改写源消息或自动确立现实项目已经完成。

两类 POST 都必须带刚读取的候选 `source_fingerprint`（返回的 64 位十六进制摘要）。缺失或格式错误为 `400`；来源已变化为 `409`，响应含 `code: "source_changed"`。收到冲突时重新获取候选、查看新增证据，再由用户决定保存；不要自动用新指纹重试旧决定。

草稿返回 `id`、`opportunity_id`、`title`、`body`、`evidence_ids`、`created_at`、`source_fingerprint`、`status: "draft"`。相同候选证据版本可以复用已保存草稿，新版本生成不同草稿，旧稿保留。草稿列表还返回 `source_stale`：`true` 表示当前证据已变化，`false` 表示当前可核对且相同，`null` 表示候选当前不在可核对范围；`source_note` 解释生成时证据的限制。

草稿包括跨会话线索时会保留各自原消息 ID。当前接口生成固定模板，未提供模型润色、发送或草稿正文编辑保存接口。

复核与草稿写入独立的 `workspace.sqlite`；原归档与导入索引保持不变。候选已不在当前检索范围时，先重新检索并检查证据，再尝试保存。

### 在人工演示页面中试用

以下代码适合在已经连接的**人工演示页面**开发者控制台中执行。它使用同源 Cookie，不需要复制令牌：

```javascript
const result = await fetch('/api/world/opportunities?q=机器人&limit=5').then(r => r.json());
const candidate = result.items[0];
const evidence = await fetch('/api/world/evidence?id=' + encodeURIComponent(candidate.anchor_id)).then(r => r.json());
```

如需在演示中试验保存本人标记：

```javascript
await fetch('/api/world/review', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({id: candidate.id, source_fingerprint: candidate.source_fingerprint, status: 'needs_review', note: '人工示例：核对时间、规格和后续消息'})
}).then(r => r.json());
```

## 错误与既有聊天功能

常见响应包括：参数无效 `400`、缺少本机会话 `401`、主机或来源不匹配 `403`、证据或候选不存在 `404`、不支持的写操作 `405`、来源已变化或索引尚未就绪 `409`、请求过大 `413`、非 JSON 写请求 `415`、本地状态暂不可用 `503`。调用方应保留原界面并显示状态，不把错误解释成“没有聊天”或“没有机会”。

`/api/conversations`、`/api/messages`、`/api/search`、`/api/export` 与 `/api/media/` 继续提供本地历史浏览与附件服务；`/history?conversation=<id>&message=<id>` 可以打开证据对应的历史位置。导出是在本机产生新的私人文件，不是公开发布。
