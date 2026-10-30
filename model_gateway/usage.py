"""每次调用的 token、延迟、花费和错误率。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from model_gateway.db import connect
from model_gateway.errors import ErrorCode, GatewayError
from model_gateway.models import CallRecord, UsageSummary


def summarize(records: list[CallRecord]) -> UsageSummary:
    """error_rate 是失败尝试 / 全部尝试。request_error_rate 是最终没成功的请求 / 请求数。"""
    attempts = len(records)
    successes = sum(1 for record in records if record.status == "success")
    errors = attempts - successes
    by_request: dict[str, bool] = {}
    for record in records:
        ok = by_request.get(record.logical_request_id, False)
        by_request[record.logical_request_id] = ok or record.status == "success"
    requests = len(by_request)
    failed_requests = sum(1 for ok in by_request.values() if not ok)
    cost = sum((record.cost_usd for record in records), Decimal("0"))
    latencies = [record.latency_ms for record in records]
    return UsageSummary(
        attempts=attempts,
        requests=requests,
        successes=successes,
        errors=errors,
        error_rate=(errors / attempts) if attempts else 0.0,
        request_error_rate=(failed_requests / requests) if requests else 0.0,
        input_tokens=sum(record.input_tokens for record in records),
        output_tokens=sum(record.output_tokens for record in records),
        cache_read_tokens=sum(record.cache_read_tokens for record in records),
        cache_write_tokens=sum(record.cache_write_tokens for record in records),
        total_tokens=sum(record.total_tokens for record in records),
        cost_usd=cost,
        avg_latency_ms=(sum(latencies) / attempts) if attempts else 0.0,
        max_latency_ms=max(latencies) if latencies else 0.0,
    )


class UsageStore:
    def __init__(self, path: str | Path, connection: sqlite3.Connection | None = None) -> None:
        self.path = path
        self._conn = connection or connect(path)
        self._lock = threading.Lock()
        self._owns = connection is None

    def close(self) -> None:
        if self._owns:
            self._conn.close()

    def record(self, item: CallRecord) -> CallRecord:
        """追加一行。重试不会改旧行，每次尝试各记一条。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO call_records (
                  id, logical_request_id, attempt, provider, model, prompt_key, prompt_version,
                  materials_json, started_at, latency_ms, status, error_code, error_message,
                  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                  uncached_input_tokens, total_tokens, cost_usd, price_tier, provider_request_id,
                  stream, finish_reason
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    item.id,
                    item.logical_request_id,
                    item.attempt,
                    item.provider,
                    item.model,
                    item.prompt_key,
                    item.prompt_version,
                    item.materials_json,
                    item.started_at.isoformat(),
                    item.latency_ms,
                    item.status,
                    item.error_code.value if item.error_code else None,
                    item.error_message,
                    item.input_tokens,
                    item.output_tokens,
                    item.cache_read_tokens,
                    item.cache_write_tokens,
                    item.uncached_input_tokens,
                    item.total_tokens,
                    format(item.cost_usd, "f"),
                    item.price_tier,
                    item.provider_request_id,
                    1 if item.stream else 0,
                    item.finish_reason,
                ),
            )
            self._conn.commit()
        return self.get(item.id)

    def get(self, record_id: str) -> CallRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM call_records WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise GatewayError(ErrorCode.INVALID_REQUEST, f"调用记录不存在: {record_id}")
        return _from_row(row)

    def list_calls(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        logical_request_id: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[CallRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if model is not None:
            clauses.append("model = ?")
            params.append(model)
        if logical_request_id is not None:
            clauses.append("logical_request_id = ?")
            params.append(logical_request_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM call_records {where} ORDER BY started_at, attempt, id",
                params,
            ).fetchall()
        return [_from_row(row) for row in rows]

    def summary(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageSummary:
        return summarize(
            self.list_calls(provider=provider, model=model, since=since, until=until)
        )


def _from_row(row: sqlite3.Row) -> CallRecord:
    return CallRecord(
        id=row["id"],
        logical_request_id=row["logical_request_id"],
        attempt=row["attempt"],
        provider=row["provider"],
        model=row["model"],
        prompt_key=row["prompt_key"],
        prompt_version=row["prompt_version"],
        materials_json=row["materials_json"],
        started_at=datetime.fromisoformat(row["started_at"]),
        latency_ms=row["latency_ms"],
        status=row["status"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        uncached_input_tokens=row["uncached_input_tokens"],
        total_tokens=row["total_tokens"],
        cost_usd=Decimal(row["cost_usd"]),
        price_tier=row["price_tier"],
        provider_request_id=row["provider_request_id"],
        stream=bool(row["stream"]),
        finish_reason=row["finish_reason"],
    )
