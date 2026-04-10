import pytest
from pathlib import Path
from app.plugins.clawith_superpowers.market_client import SuperpowersMarketClient


def test_client_initialization(tmp_path):
    client = SuperpowersMarketClient(base_dir=tmp_path)
    assert client.base_dir == tmp_path
    assert not client.is_cloned()


def test_is_cloned_when_not_cloned(tmp_path):
    client = SuperpowersMarketClient(base_dir=tmp_path)
    assert not client.is_cloned()


def test_list_available_skills_when_not_cloned(tmp_path):
    client = SuperpowersMarketClient(base_dir=tmp_path)
    assert client.list_available_skills() == []
