"""Beta 专用证据读取：严格错误传播与不依赖功能开关的生命周期检查。"""

import time
from dataclasses import replace
from typing import Any

from ..retrieval.hybrid_retriever import HybridResult
from ..retrieval.vector_search import retrieve_documents
from ..utils.json_utils import safe_json_dict
from ..memory_scope import adapter_sql, current_adapter_access, current_read_policy, read_denied, read_filters
from ..temporal_coverage import summarize_records


class MemoryEngineEvidenceMixin:
    async def get_temporal_coverage(self, *, session_id, persona_id) -> dict:
        """Read scoped active record dates without semantic ranking or source text."""
        access = current_adapter_access.get()
        if read_denied(self.read_policy_config):
            return {'status': 'unavailable'}
        policy = current_read_policy()
        session_id, persona_id = read_filters(session_id, persona_id)
        if (access is None or not access.adapter_id or (not session_id and policy is None) or not persona_id
                or self.db_connection is None):
            return {'status': 'unavailable'}
        predicate, values = adapter_sql('metadata')
        # Legacy unspecified adapter sharing must not expand this new evidence.
        if access.adapter_ids is None and policy is None:
            predicate, values = "json_extract(metadata,'$.adapter_id') = ?", [access.adapter_id]
        if not values:
            return {'status': 'unavailable'}
        session_sql = " AND json_extract(metadata,'$.session_id') = ?" if session_id else ""
        sql = ("SELECT id, metadata FROM documents WHERE json_valid(metadata) AND "
               + predicate + session_sql + " AND json_extract(metadata,'$.persona_id') = ?"
               " AND COALESCE(json_extract(metadata,'$.status'),'active') = 'active' LIMIT 10001")
        async with self.db_connection.execute(sql, [*values, *([session_id] if session_id else []), persona_id]) as cursor:
            rows = await cursor.fetchall()
        if len(rows) > 10000:
            return {'status': 'unavailable'}
        atoms = await self._agentic_atom_rows(
            [int(row[0]) for row in rows], include_content=False
        )
        visible = []
        for memory_id, raw_metadata in rows:
            metadata = safe_json_dict(raw_metadata)
            atom_rows = atoms.get(int(memory_id))
            if atom_rows is not None or metadata.get('atom_types'):
                if not self._agentic_live_rows(atom_rows or []):
                    continue
            visible.append(metadata)
        return summarize_records(visible)

    async def _agentic_has_atom_table(self) -> bool:
        # 原子表在引擎初始化时创建，结果可按实例缓存；避免每次召回都查 sqlite_master。
        cached = getattr(self, "_atom_table_checked", None)
        if cached is not None:
            return cached
        if self.db_connection is None:
            raise RuntimeError("memory database is not ready")
        async with self.db_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_atoms'"
        ) as cursor:
            present = await cursor.fetchone() is not None
        self._atom_table_checked = present
        return present

    async def _agentic_atom_rows(
        self, memory_ids: list[int], *, include_content: bool = True
    ) -> dict[int, list]:
        """按候选批量取状态；只有确认不存在表时才按普通旧文档处理。"""
        if not memory_ids or not await self._agentic_has_atom_table():
            return {}
        grouped: dict[int, list] = {}
        ids = list(dict.fromkeys(memory_ids))
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            async with self.db_connection.execute(
                "SELECT id, parent_memory_id, "
                + ("content" if include_content else "NULL")
                + ", status, expires_at "
                "FROM memory_atoms WHERE parent_memory_id IN "
                f"({','.join('?' for _ in batch)}) ORDER BY id",
                batch,
            ) as cursor:
                for row in await cursor.fetchall():
                    grouped.setdefault(int(row[1]), []).append(row)
        return grouped

    @staticmethod
    def _agentic_live_rows(rows: list) -> list:
        now = time.time()
        return [row for row in rows if row[3] == "active" and row[4] > now]

    async def _apply_agentic_atom_policy(
        self, results: list[HybridResult]
    ) -> list[HybridResult]:
        grouped = await self._agentic_atom_rows([r.doc_id for r in results])
        filtered = []
        for result in results:
            rows = grouped.get(result.doc_id)
            if rows is None and not result.metadata.get("atom_types"):
                filtered.append(result)
                continue
            live = self._agentic_live_rows(rows or [])
            if live:
                filtered.append(
                    replace(
                        result,
                        content="\n".join(dict.fromkeys(str(row[2]) for row in live)),
                        metadata={
                            **result.metadata,
                            "retrieved_atom_ids": [int(row[0]) for row in live],
                        },
                    )
                )
        return filtered

    async def resolve_live_facts(
        self, memory_id: int, content: str, metadata: dict[str, Any]
    ) -> list[str] | None:
        grouped = await self._agentic_atom_rows([memory_id])
        rows = grouped.get(memory_id)
        if rows is None and not metadata.get("atom_types"):
            return [line for line in content.split("\n") if line.strip()]
        live = self._agentic_live_rows(rows or [])
        return list(dict.fromkeys(str(row[2]) for row in live)) if live else None

    async def count_memory_atoms(self, memory_id: int) -> int:
        if not await self._agentic_has_atom_table():
            return 0
        async with self.db_connection.execute(
            "SELECT COUNT(*) FROM memory_atoms WHERE parent_memory_id = ?",
            (int(memory_id),),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    async def get_memory_atom_lifecycle(self, memory_id: int) -> dict | None:
        rows = (await self._agentic_atom_rows([memory_id])).get(memory_id)
        if not rows:
            return None
        active = len(self._agentic_live_rows(rows))
        return {"total": len(rows), "active": active, "hidden": len(rows) - active}

    async def get_memory_for_recall(self, memory_id: int) -> dict | None:
        """Beta 严格读取，普通存储异常不转换为不存在；旧 get_memory 不变。"""
        if read_denied(self.read_policy_config):
            return None
        documents = await retrieve_documents(
            self.faiss_db, metadata_filters={}, ids=[int(memory_id)], limit=1
        )
        if not documents:
            return None
        document = documents[0]
        return {
            "id": int(document["id"]),
            "text": str(document.get("text") or ""),
            "metadata": safe_json_dict(document.get("metadata")),
        }
