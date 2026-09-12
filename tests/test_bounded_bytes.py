"""The Worker's body reader must treat JS null bodies (304, manual redirects) as empty."""

from __future__ import annotations

import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace


def load_bounded_bytes():
    source = ast.parse((Path(__file__).resolve().parents[1] / "apps/web/src/entry.py").read_text())
    node = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "bounded_bytes")
    namespace: dict = {"python_value": lambda value: value}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "entry.py", "exec"), namespace)
    return namespace["bounded_bytes"]


class JsNull:
    """Mimics Pyodide's JsNull proxy: not None, no stream API."""


class BoundedBytesTests(unittest.TestCase):
    def test_null_like_bodies_read_as_empty(self) -> None:
        bounded_bytes = load_bounded_bytes()
        for body in (None, JsNull()):
            message = SimpleNamespace(headers={}, body=body)
            self.assertEqual(asyncio.run(bounded_bytes(message, 1024)), b"")

    def test_streams_are_read_within_the_limit(self) -> None:
        bounded_bytes = load_bounded_bytes()

        class Reader:
            def __init__(self, chunks):
                self.chunks = list(chunks)

            async def read(self):
                if not self.chunks:
                    return SimpleNamespace(done=True, value=None)
                return SimpleNamespace(done=False, value=self.chunks.pop(0))

            async def cancel(self):
                self.chunks = []

            def releaseLock(self):
                pass

        stream = SimpleNamespace(getReader=lambda: Reader([b"ab", b"cd"]))
        self.assertEqual(asyncio.run(bounded_bytes(SimpleNamespace(headers={}, body=stream), 10)), b"abcd")
        big = SimpleNamespace(getReader=lambda: Reader([b"x" * 8, b"y" * 8]))
        with self.assertRaises(ValueError):
            asyncio.run(bounded_bytes(SimpleNamespace(headers={}, body=big), 10))
        with self.assertRaises(ValueError):
            asyncio.run(bounded_bytes(SimpleNamespace(headers={"content-length": "99"}, body=stream), 10))


if __name__ == "__main__":
    unittest.main()
