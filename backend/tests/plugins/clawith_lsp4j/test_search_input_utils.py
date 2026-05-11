"""search_input_utils 回归：不加载 jsonrpc_router，避免 DB/异步副作用。"""

from pathlib import Path

from app.plugins.clawith_lsp4j.search_input_utils import (
    android_module_tier,
    collect_android_values_xml_hits,
    extract_android_resource_name,
    filename_keyword_for_search_file,
    infer_implicit_file_pattern_from_description,
    is_android_resource_query,
    is_unusable_natural_language_file_query,
    longest_latin_identifier,
    sanitize_search_input,
)


def test_sanitize_search_input_strips_quotes():
    assert sanitize_search_input('  "login"  ') == "login"
    assert sanitize_search_input("`Foo`") == "Foo"


def test_android_resource_query_and_name():
    assert is_android_resource_query("R.layout.activity_main")
    assert extract_android_resource_name("R.layout.activity_main") == "activity_main"
    assert extract_android_resource_name("string app_name") == "app_name"


def test_android_module_tier_ordering():
    assert android_module_tier("/proj/app/src/main/java/A.kt") == 0
    assert android_module_tier("/proj/feature/auth/src/main/java/A.kt") == 1
    assert android_module_tier("/proj/library/src/main/java/A.kt") == 2
    assert android_module_tier("/proj/other/A.kt") == 3


def test_longest_latin_and_filename_keyword_mixed_cjk():
    assert longest_latin_identifier("SignIn相关文件") == "SignIn"
    assert longest_latin_identifier("所有 Activity") == "Activity"
    assert filename_keyword_for_search_file("SignIn相关文件", "*") == "SignIn"
    assert filename_keyword_for_search_file("所有kotlin文件", "**/*.kt") == ""
    assert infer_implicit_file_pattern_from_description("所有kotlin文件", "*") == "**/*.kt"
    assert infer_implicit_file_pattern_from_description("foo", "*.kt") == "*.kt"


def test_unusable_nl_file_query():
    assert is_unusable_natural_language_file_query("登录相关文件", "*", False) is True
    assert is_unusable_natural_language_file_query("SignIn相关", "*", False) is False
    assert is_unusable_natural_language_file_query("登录相关文件", "**/*.kt", False) is False


def test_collect_android_values_xml_hits(tmp_path: Path):
    values_dir = tmp_path / "app" / "src" / "main" / "res" / "values"
    values_dir.mkdir(parents=True)
    (values_dir / "strings.xml").write_text(
        '<resources><string name="app_name">Hi</string></resources>',
        encoding="utf-8",
    )
    hits = collect_android_values_xml_hits(tmp_path, "app_name")
    assert len(hits) == 1
    assert hits[0]["fileName"] == "strings.xml"
