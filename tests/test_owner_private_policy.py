"""Synthetic event/SQLite authorization tests (no external services)."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from astrbot_plugin_livingmemory.core.memory_scope import (
    adapter_access, adapter_sql, adapter_visible, adapter_cache_key,
    resolve_memory_scope,
)


CONFIG = {"filtering_settings": {"owner_private_read_union": True,
                                  "owner_private_ids": "owner-synthetic"}}


class Event:
    def __init__(self, sender="owner-synthetic", group="", adapter="adapter-synthetic"):
        self.sender, self.group, self.adapter = sender, group, adapter
        self.unified_msg_origin = f"{adapter}:{'GroupMessage' if group else 'FriendMessage'}:{group or sender}"

    def get_platform_id(self): return self.adapter
    def get_platform_name(self): return "aiocqhttp"
    def get_sender_id(self): return self.sender
    def get_group_id(self): return self.group


def rows():
    scopes = [Event().unified_msg_origin,
              "livingmemory:user:aiocqhttp:owner-synthetic",
              "adapter-synthetic:GroupMessage:group-a",
              "adapter-synthetic:GroupMessage:group-b",
              Event(sender="other-synthetic").unified_msg_origin,
              "livingmemory:user:aiocqhttp:other-synthetic"]
    result = [{"session_id": s, "adapter_id": "adapter-synthetic", "persona_id": "persona-synthetic"} for s in scopes]
    result += [{**result[2], "persona_id": "other-persona"},
               {**result[2], "adapter_id": "other-adapter"},
               {"session_id": scopes[2]},
               {**result[2], "session_id": "adapter-synthetic:GroupMessage:"}]
    return result


@pytest.mark.parametrize("event,allowed", [(Event(), {0, 1, 2, 3}),
    (Event(group="group-a"), {2}), (Event(sender="other-synthetic"), {4, 5})])
def test_event_policy_filters_in_sql_before_top_k(event, allowed):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (id INTEGER, metadata TEXT, score INTEGER)")
    db.executemany("INSERT INTO documents VALUES (?, ?, ?)", [(i, json.dumps(m), 100-i) for i, m in enumerate(rows())])
    with adapter_access(CONFIG, event, persona_id="persona-synthetic"):
        sql, params = adapter_sql("metadata")
        actual = {r[0] for r in db.execute(f"SELECT id FROM documents WHERE {sql} ORDER BY score DESC LIMIT 20", params)}
        assert actual == allowed
        assert {i for i, m in enumerate(rows()) if adapter_visible(m)} == allowed
    db.close()


@pytest.mark.parametrize("event,persona", [(None, "persona-synthetic"), ("adapter-synthetic", "persona-synthetic"),
    (Event(), None), (Event(adapter=""), "persona-synthetic")])
def test_uncertain_event_or_persona_fails_closed(event, persona):
    with adapter_access(CONFIG, event, persona_id=persona):
        assert not any(adapter_visible(m) for m in rows())


def test_cache_separates_owner_group_and_persona_and_write_scope_is_original():
    keys = []
    for e, p in [(Event(), "persona-synthetic"), (Event(group="group-a"), "persona-synthetic"), (Event(), "other-persona")]:
        with adapter_access(CONFIG, e, persona_id=p):
            keys.append(adapter_cache_key())
            assert resolve_memory_scope(CONFIG, e) == e.unified_msg_origin
    assert len(set(keys)) == 3


def test_default_off_preserves_adapter_behavior():
    with adapter_access({}, Event()):
        assert adapter_visible({"session_id": "arbitrary"})


def test_shared_adapters_and_aliases_cannot_grant_owner_reads():
    config = {"filtering_settings": {**CONFIG["filtering_settings"],
              "shared_adapter_ids": ["adapter-synthetic", "other-adapter"]},
              "access_control": {"identity_aliases": "other-synthetic=owner-synthetic"}}
    with adapter_access(config, Event(sender="other-synthetic"), persona_id="persona-synthetic"):
        assert not adapter_visible(rows()[2])
        assert not adapter_visible(rows()[7])
    with adapter_access(config, Event(), persona_id="persona-synthetic"):
        assert not adapter_visible(rows()[7])


def test_config_schema_keeps_policy_off_and_accepts_synthetic_owners():
    from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
    assert ConfigManager({}).get("filtering_settings.owner_private_read_union") is False
    configured = ConfigManager(CONFIG)
    assert configured.get("filtering_settings.owner_private_read_union") is True
    assert configured.get("filtering_settings.owner_private_ids") == "owner-synthetic"


def test_no_event_engine_read_fails_closed():
    asyncio.run(_no_event_engine_read())


async def _no_event_engine_read():
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    from tests.test_memory_engine import _FakeFaissDB
    db = _FakeFaissDB()
    memory_id = await db.insert("unauthorized synthetic secret", rows()[4])
    engine = MemoryEngine(":memory:", db, config={"owner_private_read_union": True})
    # No retriever, source DB or external calls may be reached without real context.
    assert await engine.search_memories("synthetic query", session_id=Event().unified_msg_origin, persona_id="persona-synthetic") == []
    assert (await engine.search_memories_agentic(query="synthetic query")).results == []
    assert await engine.get_memory_for_recall(memory_id) is None
    assert await engine.get_memory_source(memory_id) == []
    assert await engine.get_memory(memory_id) is None


def test_runtime_enable_also_closes_no_event_engine_reads():
    asyncio.run(_runtime_enable())


async def _runtime_enable():
    from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    from tests.test_memory_engine import _FakeFaissDB
    class WritableConfig(dict):
        def save_config(self): pass
    config = ConfigManager(WritableConfig())
    db = _FakeFaissDB()
    memory_id = await db.insert("synthetic secret", rows()[4])
    engine = MemoryEngine(":memory:", db, read_policy_config=config)
    assert await engine.get_memory(memory_id) is not None
    config.apply_updates({"filtering_settings.owner_private_read_union": True})
    assert await engine.get_memory(memory_id) is None


class SqlStorage:
    """Real SQLite host SQL session; no vector service or embedding requests."""
    def __init__(self, path):
        self.path = path

    async def get_documents(self, metadata_filters, ids=None, limit=50, offset=0):
        # The native host uses this API when no SQL ownership predicate is
        # needed (authenticated unfiltered management / default-OFF behavior).
        clauses, params = ["1"], []
        if ids is not None:
            clauses.append(f"id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        for key, value in metadata_filters.items():
            clauses.append("json_extract(metadata, ?) = ?")
            params.extend([f"$.{key}", value])
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            data = connection.execute(
                "SELECT id, text, metadata FROM documents WHERE " + " AND ".join(clauses)
                + " ORDER BY id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in data]
        finally:
            connection.close()

    @asynccontextmanager
    async def get_session(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        class Session:
            async def execute(self, sql, values):
                data = connection.execute(str(sql), values).fetchall()
                return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: data))
        try:
            yield Session()
        finally:
            connection.close()


@pytest.mark.asyncio
async def test_real_engine_all_read_paths_and_stale_cache(tmp_path):
    from tests.test_agentic_engine_retrieval import _make_engine
    from astrbot_plugin_livingmemory.core.tools.memory_evidence_reader import read_memory_evidence_core
    engine = await _make_engine(tmp_path)
    engine.config["owner_private_read_union"] = True
    ids = []
    try:
        for index, metadata in enumerate(rows()):
            ids.append(await engine.add_memory(
                "synthetic coffee evidence " + str(index),
                session_id=metadata.get("session_id"), persona_id=metadata.get("persona_id"),
                metadata={**metadata, "source_time_start": "2025-01-02", "source_time_end": "2025-01-03",
                          "participant_identities": [{"identity_key": "aiocqhttp:owner-synthetic"}]},
                source_messages=[{"role": "user", "content": "synthetic original " + str(index)}]))
        engine.faiss_db.document_storage = SqlStorage(engine.db_path)
        for event, allowed in [(Event(), {0, 1, 2, 3}), (Event(group="group-a"), {2}), (Event(sender="other-synthetic"), {4, 5})]:
            with adapter_access(CONFIG, event, persona_id="persona-synthetic"):
                allowed_ids = {ids[i] for i in allowed}
                normal = await engine.search_memories("coffee", 20, "forged:GroupMessage:scope", "forged-persona")
                assert {r.doc_id for r in normal} == allowed_ids
                # Same query is deliberately reused across event scopes and personas.
                cached = await engine.search_memories("coffee", 20)
                assert {r.doc_id for r in cached} == allowed_ids
                beta = await engine.search_memories_agentic(query="coffee", k=20, session_id="forged", persona_id="forged")
                assert {r.doc_id for r in beta.results} == allowed_ids
                speaker = await engine.search_memories_agentic(target="current_speaker", k=20, identity_key="aiocqhttp:owner-synthetic", identity_candidates=[])
                assert {r.doc_id for r in speaker.results} == allowed_ids
                for i, memory_id in enumerate(ids):
                    evidence = await read_memory_evidence_core(memory_engine=engine, recall_session_id="forged", recall_persona_id="forged", memory_id=memory_id, include_source=True)
                    assert (evidence["status"] == "success") == (i in allowed)
                    source = await engine.get_memory_source(memory_id)
                    assert bool(source) == (i in allowed)
                coverage = await engine.get_temporal_coverage(session_id="forged", persona_id="forged")
                assert coverage["status"] == "ok"
                assert coverage["dated_record_count"] == len(allowed)
            # Exercise actual ordinary and Beta tool entry points with host-
            # resolved persona, even when upstream persona filtering is off.
            from tests.test_agentic_tools import _plugin_context, _context, _patched_host_sp
            from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
            from astrbot_plugin_livingmemory.core.tools.memory_search_tool import MemorySearchTool
            from astrbot_plugin_livingmemory.core.tools.memory_agentic_search_tool import AgenticMemorySearchTool
            from astrbot_plugin_livingmemory.core.tools.memory_read_tool import MemoryReadTool
            config = ConfigManager({"filtering_settings": {**CONFIG["filtering_settings"], "use_persona_filtering": False}, "recall_engine": {"max_k": 20}})
            arguments = dict(config_manager=config, memory_engine=engine, context=_plugin_context("persona-synthetic"))
            with _patched_host_sp():
                for cls in (MemorySearchTool, AgenticMemorySearchTool):
                    output = json.loads(await cls(**arguments).call(_context(event), query="coffee", k=20, include_source=True))
                    assert {r.get("id", r.get("memory_id")) for r in output["results"]} == allowed_ids
                speaker_tool = json.loads(await AgenticMemorySearchTool(**arguments).call(_context(event), query="", target="current_speaker", k=20))
                assert {r.get("id", r.get("memory_id")) for r in speaker_tool["results"]} == (allowed_ids if event.sender == "owner-synthetic" else set())
                read = MemoryReadTool(**arguments)
                assert json.loads(await read.call(_context(event), memory_id=ids[next(iter(allowed))], include_source=True))["status"] == "success"
                forbidden = next(i for i in range(len(ids)) if i not in allowed)
                assert json.loads(await read.call(_context(event), memory_id=ids[forbidden], include_source=True))["status"] == "not_found"
            # Automatic recall uses the same real engine, while only message-
            # capture dependencies are isolated with the upstream test helper.
            from tests.event_handler_support import _make_real_handler, _make_event, _make_req
            from unittest.mock import AsyncMock, patch
            directory = tmp_path / (event.group or event.sender)
            directory.mkdir(exist_ok=True)
            handler, manager, store = await _make_real_handler(directory, engine)
            handler.config_manager = config
            handler._memory_recall.config_manager = config
            auto_event = _make_event(group=bool(event.group))
            auto_event.unified_msg_origin = event.unified_msg_origin
            auto_event.get_platform_id.return_value = event.adapter
            auto_event.get_platform_name.return_value = "aiocqhttp"
            auto_event.get_sender_id.return_value = event.sender
            auto_event.get_group_id.return_value = event.group
            auto_event.get_message_str.return_value = "最早记忆 coffee"
            request = _make_req("coffee")
            try:
                with patch("astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id", new=AsyncMock(return_value="persona-synthetic")):
                    await handler.handle_memory_recall(auto_event, request)
                injected = "\n".join(p.text for p in request.extra_user_content_parts)
                for i in range(len(ids)):
                    assert ("synthetic coffee evidence " + str(i) in injected) == (i in allowed)
                assert "<LivingMemory-Temporal-Coverage>" in injected
            finally:
                await handler.shutdown()
                await store.close()
        with adapter_access(CONFIG, Event(), persona_id="other-persona"):
            results = await engine.search_memories("coffee", 20)
            assert {r.doc_id for r in results} == {ids[6]}
        # External/stale-cache scope changes must be re-authorized, not trusted.
        with adapter_access(CONFIG, Event(group="group-a"), persona_id="persona-synthetic"):
            await engine.search_memories("coffee", 20)
            await engine.db_connection.execute("UPDATE documents SET metadata = json_set(metadata, '$.session_id', ?) WHERE id = ?", (Event(sender="other-synthetic").unified_msg_origin, ids[2]))
            await engine.db_connection.commit()
            assert await engine.search_memories("coffee", 20) == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_native_vector_selection_excludes_unauthorized_before_top_k(tmp_path):
    import faiss
    import numpy as np
    from astrbot_plugin_livingmemory.core.retrieval.vector_search import retrieve_vectors, query_embeddings
    path = str(tmp_path / "vectors.db")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE documents(id INTEGER, doc_id TEXT, text TEXT, metadata TEXT)")
    data = rows()
    # Forbidden private rows are all closer than the authorized group-a row.
    data.extend([rows()[4] for _ in range(80)])
    connection.executemany("INSERT INTO documents VALUES (?, ?, ?, ?)", [(i, str(i), "synthetic vector", json.dumps(m)) for i, m in enumerate(data)])
    connection.commit()
    connection.close()
    index = faiss.IndexIDMap2(faiss.IndexFlatL2(2))
    vectors = np.asarray([[float(100 if i == 2 else i), 0.] for i in range(len(data))], dtype=np.float32)
    index.add_with_ids(vectors, np.arange(len(data), dtype=np.int64))
    class Provider:
        calls = 0
        async def get_embedding(self, query):
            self.calls += 1
            return [0., 0.]
    provider = Provider()
    db = SimpleNamespace(document_storage=SqlStorage(path), embedding_provider=provider,
                         embedding_storage=SimpleNamespace(index=index, dimension=2))
    cache = {}
    token = query_embeddings.set(cache)
    try:
        with adapter_access(CONFIG, Event(group="group-a"), persona_id="persona-synthetic"):
            hits = await retrieve_vectors(db, "coffee", 1, {})
            assert [r.data["id"] for r in hits] == [2]
            assert [r.data["id"] for r in await retrieve_vectors(db, "coffee", 1, {})] == [2]
        with adapter_access(CONFIG, Event(), persona_id="persona-synthetic"):
            hits = await retrieve_vectors(db, "coffee", 20, {})
            assert {r.data["id"] for r in hits} == {0, 1, 2, 3}
        assert provider.calls == 1
    finally:
        query_embeddings.reset(token)


def test_binding_persona_and_write_context_do_not_union_storage():
    from astrbot_plugin_livingmemory.core.memory_scope import bind_read_persona
    with adapter_access(CONFIG, Event()):
        assert not adapter_visible(rows()[2])
        bind_read_persona("persona-synthetic")
        assert adapter_visible(rows()[2])
    with adapter_access(CONFIG, Event(), write=True):
        assert adapter_cache_key() == ("adapter-synthetic",)
        assert resolve_memory_scope(CONFIG, Event()) == Event().unified_msg_origin


@pytest.mark.asyncio
@pytest.mark.parametrize("event,own_index", [(Event(), 0), (Event(group="group-a"), 2),
    (Event(sender="other-synthetic"), 4)])
@pytest.mark.parametrize("session_filtering", [True, False])
async def test_admin_forget_archives_only_original_scope_with_real_engine(tmp_path, event, own_index, session_filtering):
    from tests.test_agentic_engine_retrieval import _make_engine
    from tests.test_agentic_tools import _plugin_context, _context, _patched_host_sp
    from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
    from astrbot_plugin_livingmemory.core.tools.memory_forget_tool import MemoryForgetTool
    engine = await _make_engine(tmp_path)
    engine.config["owner_private_read_union"] = True
    config = ConfigManager({"filtering_settings": {**CONFIG["filtering_settings"],
                            "use_persona_filtering": False, "use_session_filtering": session_filtering},
                            "agent_tools": {"forget_tool_cooldown_seconds": 0}})
    event.is_admin = lambda: True
    ids = []
    try:
        for metadata in rows():
            ids.append(await engine.add_memory("synthetic forget evidence",
                session_id=metadata.get("session_id"), persona_id=metadata.get("persona_id"),
                metadata=metadata))
        engine.faiss_db.document_storage = SqlStorage(engine.db_path)
        tool = MemoryForgetTool(config_manager=config, memory_engine=engine,
                                context=_plugin_context("persona-synthetic"))
        with _patched_host_sp():
            unknown_persona = MemoryForgetTool(config_manager=config, memory_engine=engine,
                                               context=_plugin_context(None))
            denied = json.loads(await unknown_persona.call(_context(event), memory_ids=[ids[own_index]]))
            assert denied["forgotten"] is False
            # Even a mixed batch must leave the valid original-scope row active.
            for forbidden in range(len(ids)):
                if forbidden == own_index:
                    continue
                output = json.loads(await tool.call(_context(event),
                    memory_ids=[ids[own_index], ids[forbidden]]))
                assert output["forgotten"] is False
            output = json.loads(await tool.call(_context(event), memory_ids=[ids[own_index]]))
        assert output["forgotten"] is True
        assert output["archived_count"] == 1
        cursor = await engine.db_connection.execute("SELECT id, metadata FROM documents")
        archived = {row["id"] for row in await cursor.fetchall()
                    if json.loads(row["metadata"]).get("status") == "archived"}
        assert archived == {ids[own_index]}
        # The tool's mutation context must not survive its request.
        assert await engine.get_memory(ids[own_index]) is None
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_id", [None, "adapter-synthetic"])
async def test_registered_native_page_management_preserves_adapter_boundary(tmp_path, adapter_id):
    from tests.test_agentic_engine_retrieval import _make_engine
    from tests.test_page_api import FakePlugin, _qp
    from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
    from astrbot_plugin_livingmemory.core.page_api import PluginPageApi, PAGE_API_PREFIX
    engine = await _make_engine(tmp_path)
    engine.config["owner_private_read_union"] = True
    try:
        ids = []
        for metadata in (rows()[0], rows()[7]):
            ids.append(await engine.add_memory("synthetic page evidence",
                session_id=metadata["session_id"], persona_id=metadata["persona_id"],
                metadata=metadata, source_messages=[{"role": "user", "content": "synthetic source"}]))
        engine.faiss_db.document_storage = SqlStorage(engine.db_path)
        plugin = FakePlugin(memory_engine=engine)
        plugin.config_manager = ConfigManager(CONFIG)
        api = PluginPageApi(plugin)
        api.register_routes()
        routes = {route: handler for route, handler, _, _ in plugin._api_routes}
        query = {"adapter_id": adapter_id} if adapter_id else {}
        with _qp(args=query):
            page = await routes[f"{PAGE_API_PREFIX}/memories"]()
        assert page["status"] == "ok"
        assert {item["id"] for item in page["data"]["items"]} == ({ids[0]} if adapter_id else set(ids))
        for index, memory_id in enumerate(ids):
            allowed = not adapter_id or index == 0
            with _qp(args={**query, "memory_id": str(memory_id)}):
                detail = await routes[f"{PAGE_API_PREFIX}/memories/detail"]()
            assert (detail["status"] == "ok") == allowed
            if allowed:
                assert detail["data"]["source_messages"][0]["content"] == "synthetic source"
            with _qp(args=query, get_json={"memory_id": memory_id, "field": "status", "value": "archived"}):
                update = await routes[f"{PAGE_API_PREFIX}/memories/update"]()
            assert (update["status"] == "ok") == allowed
        cursor = await engine.db_connection.execute("SELECT id, metadata FROM documents")
        archived = {row["id"] for row in await cursor.fetchall()
                    if json.loads(row["metadata"]).get("status") == "archived"}
        assert archived == ({ids[0]} if adapter_id else set(ids))
        # A generic eventless caller cannot inherit authenticated page authority.
        assert await engine.get_memory(ids[0]) is None
        assert await engine.get_memory_source(ids[0]) == []
        with adapter_access(CONFIG, "adapter-synthetic"):
            assert (await engine.list_memories_page())["total"] == 0
            assert await engine.get_memory(ids[0]) is None
    finally:
        await engine.close()
