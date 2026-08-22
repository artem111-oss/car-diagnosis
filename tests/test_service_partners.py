"""Партнёрские СТО: заявки не публичны, пока не одобрены вручную."""
from __future__ import annotations

import pytest

from service.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db")


def test_new_partner_is_not_public_until_approved(store):
    pid = store.add_partner(name="СТО Восток", city="Казань",
                            contact="@sto_vostok", note="")
    assert store.list_partners(approved_only=True) == []
    store.approve_partner(pid)
    approved = store.list_partners(approved_only=True)
    assert len(approved) == 1 and approved[0]["name"] == "СТО Восток"


def test_unapproved_visible_only_with_flag_off(store):
    store.add_partner(name="СТО Юг", city="Сочи", contact="123", note="")
    assert len(store.list_partners(approved_only=False)) == 1


def test_stats_count_pending_and_approved(store):
    a = store.add_partner(name="A", city="X", contact="1", note="")
    store.add_partner(name="B", city="Y", contact="2", note="")
    store.approve_partner(a)
    s = store.stats()
    assert s["partners_approved"] == 1
    assert s["partners_pending"] == 1
