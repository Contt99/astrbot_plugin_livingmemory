"""Event-derived read boundary. Never used to resolve a storage scope."""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class OwnerReadPolicy:
    adapter_id: str = ""
    persona_id: str = ""
    private_scopes: tuple[str, ...] = ()
    group_prefix: str = ""
    valid_event: bool = False
    # 私聊时允许读的群会话，按发言者 QQ 号从历史消息里反查得出。
    # 与 group_prefix（主人全开）并存：prefix 非空时两者都能命中同一批群。
    group_scopes: tuple[str, ...] = ()

    def with_persona(self, persona_id):
        return replace(self, persona_id=str(persona_id or "").strip())

    @property
    def ready(self):
        return self.valid_event and bool(self.persona_id) and self.persona_id != "[%None]"

    def visible(self, metadata):
        scope = str(metadata.get("session_id") or "")
        group = (bool(self.group_prefix) and scope.startswith(self.group_prefix)
                 and bool(scope[len(self.group_prefix):])
                 and ":" not in scope[len(self.group_prefix):])
        return (self.ready and metadata.get("adapter_id") == self.adapter_id
                and metadata.get("persona_id") == self.persona_id
                and (scope in self.private_scopes or group
                     or scope in self.group_scopes))

    def sql(self, metadata):
        if not self.ready:
            # A bound parameter also forces host retrieval through the SQL path.
            return "? = 1", ["deny"]
        scope = f"json_extract({metadata}, '$.session_id')"
        alternatives, params = [], [self.adapter_id, self.persona_id]
        if self.private_scopes:
            alternatives.append(f"{scope} IN ({','.join('?' for _ in self.private_scopes)})")
            params.extend(self.private_scopes)
        if self.group_scopes:
            alternatives.append(f"{scope} IN ({','.join('?' for _ in self.group_scopes)})")
            params.extend(self.group_scopes)
        if self.group_prefix:
            # Equality on the literal prefix avoids wildcard expansion of adapter IDs.
            alternatives.append(f"(substr({scope}, 1, ?) = ? AND length({scope}) > ? AND instr(substr({scope}, ?), ':') = 0)")
            params.extend([len(self.group_prefix), self.group_prefix,
                           len(self.group_prefix), len(self.group_prefix) + 1])
        return (f"(json_extract({metadata}, '$.adapter_id') = ? AND "
                f"json_extract({metadata}, '$.persona_id') = ? AND ("
                + " OR ".join(alternatives or ["0"]) + "))"), params


def derive_read_policy(config, event, persona_id=None):
    # Imports are local to keep the existing memory_scope public API stable.
    from .memory_scope import (
        _event_value,
        _config_get,
        parse_value_list,
        resolve_group_scopes,
    )
    denied = OwnerReadPolicy()
    if event is None or isinstance(event, str):
        return denied
    adapter = _event_value(event, "get_platform_id")
    platform = _event_value(event, "get_platform_name").casefold()
    sender = _event_value(event, "get_sender_id")
    group = _event_value(event, "get_group_id")
    origin = _event_value(event, "unified_msg_origin")
    if not adapter or not platform or not sender or ":" in adapter or ":" in sender or ":" in group:
        return denied
    expected = f"{adapter}:{'GroupMessage' if group else 'FriendMessage'}:{group or sender}"
    if origin != expected:
        return denied
    owners = parse_value_list(_config_get(config, "filtering_settings.owner_private_ids", ""))
    scopes = (origin,) if group else (origin, f"livingmemory:user:{platform}:{sender.casefold()}")
    if group:
        return OwnerReadPolicy(adapter, str(persona_id or ""), scopes, "", True)
    # 私聊：主人保持“全群”并集（沿用 group_prefix），其余人按本人 QQ 号
    # 反查其在历史消息里出现过的群聊会话，读不到没待过的群。
    owner_prefix = f"{adapter}:GroupMessage:" if sender in owners else ""
    group_scopes = () if owner_prefix else resolve_group_scopes(adapter, platform, sender)
    return OwnerReadPolicy(adapter, str(persona_id or ""), scopes,
                           owner_prefix, True, tuple(group_scopes))
