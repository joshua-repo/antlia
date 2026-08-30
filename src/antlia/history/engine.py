"""The query engine and its data types, or a message naming the extra.

DuckDB reading Parquet directly, and the arrow types a read is built out of.
Every module in this layer that touches either takes them from here.

That indirection is not decoration. Every other layer in antlia answers a
missing dependency with `MissingExtra` and the exact `pip install`, and a plain
`import duckdb` at the top of a module is sorted *above* any antlia import --
so a guard placed in the module that needs it can never run first. Importing
these through a first-party module puts the guard first in every file, and
`pip install antlia[store]` is what a consumer reads instead of a
`ModuleNotFoundError` from three frames down.

`pyarrow.dataset` is deliberately absent: it pulls in pandas when pandas is
installed, and only the writer needs it, so it stays a deferred import inside
`store.write`.
"""

from __future__ import annotations

from antlia.auth.errors import MissingExtra

try:
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover - exercised by an install, not a test
    raise MissingExtra("history", "store", "duckdb and pyarrow") from exc

__all__ = ["duckdb", "pa", "pq"]
