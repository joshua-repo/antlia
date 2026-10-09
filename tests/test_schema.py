"""The shared contract: every dataset of a class carries that class's identity."""

from __future__ import annotations

from antlia.history.types import TABLES
from antlia.schema import IDENTITY, Asset


def test_every_asset_class_declares_its_identity():
    assert set(IDENTITY) == set(Asset)


def test_history_tables_carry_their_class_identity_verbatim():
    # `expirations` is a listing, not an observation of a contract, and is the
    # one table allowed to carry only part of its class's identity.
    for spec in TABLES.values():
        if spec.name == "expirations":
            continue
        wanted = IDENTITY[spec.asset]
        held = {c.name: c for c in spec.columns}
        for column in wanted:
            assert held[column.name] == column, (spec.name, column.name)
