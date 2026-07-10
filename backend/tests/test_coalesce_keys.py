"""coalesce key 单元测试。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.coalesce_keys import normalize_coalesce_key


def test_search_text_same_query_same_key():
    a = normalize_coalesce_key("s1", "fs/search_text", "", {"query": "foo", "filePattern": "*.kt"})
    b = normalize_coalesce_key("s1", "fs/search_text", "", {"query": "foo", "filePattern": "*.kt"})
    assert a == b
    assert a != ""


def test_search_text_diff_query_diff_key():
    a = normalize_coalesce_key("s1", "fs/search_text", "", {"query": "foo"})
    b = normalize_coalesce_key("s1", "fs/search_text", "", {"query": "bar"})
    assert a != b


def test_search_text_cursor_skips_coalesce():
    assert normalize_coalesce_key("s1", "fs/search_text", "", {"query": "foo", "cursor": "x"}) == ""


def test_find_file_same_query_same_key():
    a = normalize_coalesce_key("s1", "fs/find_file", "", {"query": "MainActivity"})
    b = normalize_coalesce_key("s1", "fs/find_file", "", {"query": "MainActivity"})
    assert a == b
