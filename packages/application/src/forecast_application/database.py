"""Shared prepared-SQL protocol and transactional SQLite verification adapter."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import sqlite3

Statement = tuple[str, tuple[Any, ...]]


class Database(Protocol):
    async def first(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None: ...

    async def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]: ...

    async def batch(self, statements: Sequence[Statement]) -> list[dict[str, Any]]: ...


class SQLiteDatabase:
    """Uses the exact migration and queries deployed to D1; batches are atomic."""

    def __init__(self, connection: sqlite3.Connection):
        import sqlite3

        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    async def first(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    async def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def _execute(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        cursor = self.connection.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        return {"results": rows, "meta": {"changes": max(0, cursor.rowcount)}}

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
        with self.connection:
            return self._execute(sql, params)

    async def batch(self, statements: Sequence[Statement]) -> list[dict[str, Any]]:
        # No await inside this transaction: concurrent coroutines cannot interleave.
        with self.connection:
            return [self._execute(sql, params) for sql, params in statements]
