# 主人私聊读取策略（2.9.0+owner）

默认关闭，在插件高级设置「记忆隔离设置」启用：

- `filtering_settings.owner_private_read_union`：`false` 为上游原行为。
- `filtering_settings.owner_private_ids`：主人原始发送者 ID，逗号或换行分隔，默认空。只使用真实事件 ID，不使用模型参数或身份别名。

开启后，主人私聊读取同一适配器实例、同一人格的全部群记忆，以及本人的 `FriendMessage` 与 `livingmemory:user:<平台>:<发送者>` 旧私聊记忆。群聊只读本群；其他私聊只读自己的两种私聊作用域。此策略覆盖自动回忆、普通工具、Beta 对话/当前发言者回忆、按 ID 深读、原文和日期覆盖证据。读取限制优先于旧的 global/user 模式和适配器共享设置；写入仍使用原 `resolve_memory_scope`，不会改成联合范围。

真实事件的适配器、标准 `FriendMessage`/`GroupMessage` 来源、发送者或宿主人格解析不完整时拒绝读取。记录缺少匹配的 `adapter_id`、`persona_id` 或作用域时也不会读取。即使旧「人格过滤」关闭，开启本策略仍强制同人格。不会猜测旧数据归属，也不会迁移、删除或重新嵌入数据。

SQL 权限条件在关键词、图谱、原子和时间查询的候选限制之前执行；原生 FAISS 使用相同 SQL 选出的允许 ID 集合。旧宿主若没有原生 FAISS 选择器能力，该向量路线返回空结果，其他可用路线继续。检索缓存按完整策略隔离，并在复用时重新校验当前数据库归属。日期扫描保留 10000 条上限和原来的日期/生命周期规则。

时间覆盖功能来源：`_cloud/native-review-20261106/astrbot_plugin_livingmemory` 快照，仅移植 `core/temporal_coverage.py`、`core/event_handler_modules/memory_recall.py`、`core/managers/memory_engine_evidence.py` 中已有的时间覆盖改动，再将其读取条件接入本策略。上游基线为 2.9.0（`9baad49689a4cdc4ef289d0d3ebcf9178140a6b2`），未升级到 2.9.1。
