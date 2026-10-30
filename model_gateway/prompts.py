"""提示词版本库。可替换变量走占位符，外部资料单独附上，两边不会互相替换。"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from model_gateway.db import connect
from model_gateway.errors import PromptError
from model_gateway.models import (
    ExternalMaterial,
    MaterialInput,
    MaterialSpec,
    PromptSlot,
    PromptSpec,
    PromptVersion,
    RenderedMaterial,
    RenderedPrompt,
    SlotKind,
)

# {{ 和 }} 先换成哨兵，替换完再变回字面量大括号。替换不是递归的。
_OPEN = "\uE000"
_CLOSE = "\uE001"
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def split_slots(slots: list[PromptSlot]) -> tuple[list[PromptSlot], list[PromptSlot]]:
    variables = [slot for slot in slots if slot.kind == SlotKind.VARIABLE]
    externals = [slot for slot in slots if slot.kind == SlotKind.EXTERNAL]
    return variables, externals


def find_placeholders(body: str) -> set[str]:
    protected = body.replace("{{", _OPEN).replace("}}", _CLOSE)
    return set(_PLACEHOLDER.findall(protected))


def stringify_variable(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    raise PromptError(f"变量类型不支持: {type(value).__name__}")


def coerce_variables(source: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    data = getattr(source, "__dict__", None)
    if isinstance(data, dict):
        return {key: value for key, value in data.items() if not str(key).startswith("_")}
    raise PromptError("variables 需要 dict，或带实例属性的对象")


def substitute_variables(body: str, variables: Mapping[str, str]) -> str:
    """只替换已声明的变量。外部资料不会进这里，正文里的 {name} 必须正好对上变量槽。"""
    for name, value in variables.items():
        if _OPEN in value or _CLOSE in value:
            raise PromptError(f"变量 {name} 含有保留字符")
    protected = body.replace("{{", _OPEN).replace("}}", _CLOSE)

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise PromptError(f"模板变量 {{{name}}} 没有提供值")
        return variables[name]

    rendered = _PLACEHOLDER.sub(repl, protected)
    return rendered.replace(_OPEN, "{").replace(_CLOSE, "}")


class PromptStore:
    def __init__(self, path: str | Path, connection: sqlite3.Connection | None = None) -> None:
        self.path = path
        self._conn = connection or connect(path)
        self._lock = threading.Lock()
        self._owns = connection is None

    def close(self) -> None:
        if self._owns:
            self._conn.close()

    def publish(self, spec: PromptSpec) -> PromptVersion:
        """发布一个新版本。active 会把同 key 上一条生效版本改成 archived，draft 不会。"""
        self._check_body(spec)
        slots_json = json.dumps(
            [slot.model_dump(mode="json") for slot in spec.slots],
            ensure_ascii=False,
        )
        created_at = _now()
        with self._lock:
            current = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM prompt_versions WHERE prompt_key = ?",
                (spec.key,),
            ).fetchone()
            version = int(current["version"]) + 1
            if spec.status == "active":
                self._conn.execute(
                    "UPDATE prompt_versions SET status = 'archived' WHERE prompt_key = ? AND status = 'active'",
                    (spec.key,),
                )
            self._conn.execute(
                """
                INSERT INTO prompt_versions
                  (prompt_key, version, role, body, slots_json, status, changelog, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.key,
                    version,
                    spec.role,
                    spec.body,
                    slots_json,
                    spec.status,
                    spec.changelog,
                    created_at.isoformat(),
                ),
            )
            self._conn.commit()
        return self.get(spec.key, version)

    def get(self, key: str, version: int | None = None) -> PromptVersion:
        """不传 version 时只取当前 active。指定版本时不论 draft 还是 archived 都能取到。"""
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    """
                    SELECT * FROM prompt_versions
                    WHERE prompt_key = ? AND status = 'active'
                    ORDER BY version DESC LIMIT 1
                    """,
                    (key,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM prompt_versions WHERE prompt_key = ? AND version = ?",
                    (key, version),
                ).fetchone()
        if row is None:
            label = f"{key}@{version}" if version is not None else f"{key} 的生效版本"
            raise PromptError(f"提示词不存在: {label}")
        return _prompt_from_row(row)

    def list_versions(self, key: str) -> list[PromptVersion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM prompt_versions WHERE prompt_key = ? ORDER BY version",
                (key,),
            ).fetchall()
        return [_prompt_from_row(row) for row in rows]

    def set_status(self, key: str, version: int, status: str) -> PromptVersion:
        if status not in {"draft", "active", "archived"}:
            raise PromptError("状态只能是 draft、active 或 archived")
        with self._lock:
            found = self._conn.execute(
                "SELECT version FROM prompt_versions WHERE prompt_key = ? AND version = ?",
                (key, version),
            ).fetchone()
            if found is None:
                raise PromptError(f"提示词不存在: {key}@{version}")
            if status == "active":
                self._conn.execute(
                    """
                    UPDATE prompt_versions SET status = 'archived'
                    WHERE prompt_key = ? AND status = 'active' AND version != ?
                    """,
                    (key, version),
                )
            self._conn.execute(
                "UPDATE prompt_versions SET status = ? WHERE prompt_key = ? AND version = ?",
                (status, key, version),
            )
            self._conn.commit()
        return self.get(key, version)

    def put_material(self, spec: MaterialSpec) -> ExternalMaterial:
        created_at = _now()
        with self._lock:
            current = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM external_materials WHERE material_id = ?",
                (spec.material_id,),
            ).fetchone()
            version = int(current["version"]) + 1
            self._conn.execute(
                """
                INSERT INTO external_materials
                  (material_id, version, title, content, content_type, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.material_id,
                    version,
                    spec.title,
                    spec.content,
                    spec.content_type,
                    spec.source,
                    created_at.isoformat(),
                ),
            )
            self._conn.commit()
        return self.get_material(spec.material_id, version)

    def get_material(self, material_id: str, version: int | None = None) -> ExternalMaterial:
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    """
                    SELECT * FROM external_materials
                    WHERE material_id = ?
                    ORDER BY version DESC LIMIT 1
                    """,
                    (material_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM external_materials WHERE material_id = ? AND version = ?",
                    (material_id, version),
                ).fetchone()
        if row is None:
            label = f"{material_id}@{version}" if version is not None else material_id
            raise PromptError(f"外部资料不存在: {label}")
        return _material_from_row(row)

    def list_material_versions(self, material_id: str) -> list[ExternalMaterial]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM external_materials WHERE material_id = ? ORDER BY version",
                (material_id,),
            ).fetchall()
        return [_material_from_row(row) for row in rows]

    def list_prompts(self) -> list[PromptVersion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM prompt_versions ORDER BY prompt_key, version"
            ).fetchall()
        return [_prompt_from_row(row) for row in rows]

    def list_materials(self) -> list[ExternalMaterial]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM external_materials ORDER BY material_id, version"
            ).fetchall()
        return [_material_from_row(row) for row in rows]

    def render(
        self,
        key: str,
        *,
        version: int | None = None,
        variables: Mapping[str, Any] | Any | None = None,
        materials: list[MaterialInput] | None = None,
    ) -> RenderedPrompt:
        prompt = self.get(key, version)
        provided = coerce_variables(variables or {})
        variable_slots, external_slots = split_slots(prompt.slots)
        variable_names = {slot.name for slot in variable_slots}
        external_names = {slot.name for slot in external_slots}
        unknown = set(provided) - variable_names
        if unknown & external_names:
            name = sorted(unknown & external_names)[0]
            raise PromptError(f"{name} 是外部资料，不能当作变量替换")
        if unknown:
            raise PromptError(f"未声明的变量: {', '.join(sorted(unknown))}")
        resolved: dict[str, str] = {}
        for slot in variable_slots:
            if slot.name in provided:
                if provided[slot.name] is None:
                    raise PromptError(f"变量 {slot.name} 不能是 null")
                resolved[slot.name] = stringify_variable(provided[slot.name])
            elif slot.default is not None:
                resolved[slot.name] = stringify_variable(slot.default)
            elif slot.required:
                raise PromptError(f"缺少可替换变量 {slot.name}")
            else:
                resolved[slot.name] = ""
        text = substitute_variables(prompt.body, resolved)
        rendered_materials = self._resolve_materials(external_slots, materials or [])
        return RenderedPrompt(
            prompt_key=prompt.prompt_key,
            version=prompt.version,
            role=prompt.role,
            text=text,
            materials=rendered_materials,
            variables_used=resolved,
        )

    def _resolve_materials(
        self,
        slots: list[PromptSlot],
        materials: list[MaterialInput],
    ) -> list[RenderedMaterial]:
        by_slot: dict[str, MaterialInput] = {}
        declared = {slot.name for slot in slots}
        for item in materials:
            if item.slot not in declared:
                raise PromptError(f"未声明的外部资料槽位: {item.slot}")
            if item.slot in by_slot:
                raise PromptError(f"外部资料槽位重复: {item.slot}")
            by_slot[item.slot] = item
        rendered: list[RenderedMaterial] = []
        for slot in slots:
            item = by_slot.get(slot.name)
            if item is None:
                if slot.required:
                    raise PromptError(f"缺少外部资料 {slot.name}")
                continue
            if item.material_id is not None:
                stored = self.get_material(item.material_id, item.version)
                rendered.append(
                    RenderedMaterial(
                        name=slot.name,
                        title=stored.title,
                        content=stored.content,
                        material_id=stored.material_id,
                        material_version=stored.version,
                        source=stored.source,
                    )
                )
                continue
            rendered.append(
                RenderedMaterial(
                    name=slot.name,
                    title=item.title or slot.name,
                    content=item.content or "",
                )
            )
        return rendered

    def _check_body(self, spec: PromptSpec) -> None:
        variables, externals = split_slots(spec.slots)
        variable_names = {slot.name for slot in variables}
        external_names = {slot.name for slot in externals}
        found = find_placeholders(spec.body)
        if found & external_names:
            raise PromptError("外部资料不能写进模板占位符，请放到 materials")
        missing = variable_names - found
        extra = found - variable_names
        if missing or extra:
            raise PromptError(
                "模板占位符必须且只能对应可替换变量；"
                f"缺少 {sorted(missing) or '-'}，多余 {sorted(extra) or '-'}"
            )


def _prompt_from_row(row: sqlite3.Row) -> PromptVersion:
    slots = [PromptSlot.model_validate(item) for item in json.loads(row["slots_json"])]
    return PromptVersion(
        prompt_key=row["prompt_key"],
        version=row["version"],
        role=row["role"],
        body=row["body"],
        slots=slots,
        status=row["status"],
        changelog=row["changelog"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _material_from_row(row: sqlite3.Row) -> ExternalMaterial:
    return ExternalMaterial(
        material_id=row["material_id"],
        version=row["version"],
        title=row["title"],
        content=row["content"],
        content_type=row["content_type"],
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
