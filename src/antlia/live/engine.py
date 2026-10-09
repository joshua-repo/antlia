"""DuckDB and arrow for the live layer, or a message naming the extra.

The same guard as `history.engine`, kept separate because `live` may not
import `history`. Only reads that canonicalise a vendor frame need it --
`live.fx` runs on the standard library -- so it is imported inside those
reads, never at package import.
"""

from __future__ import annotations

from antlia.auth.errors import MissingExtra

try:
    import duckdb
    import pyarrow as pa
except ImportError as exc:  # pragma: no cover - exercised by an install, not a test
    raise MissingExtra("live", "store", "duckdb and pyarrow") from exc

__all__ = ["duckdb", "pa"]
