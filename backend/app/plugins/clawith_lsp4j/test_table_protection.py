"""测试 _convert_file_paths_to_links 方法的表格保护功能。"""

import sys
from pathlib import Path

# 添加 backend 目录到 Python 路径
backend_dir = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(backend_dir))

from app.plugins.clawith_lsp4j.jsonrpc_router import JSONRPCRouter


def test_table_with_paths():
    """测试表格中包含文件路径时的转换。"""
    # 原始 Markdown 表格
    markdown_table = """这里是一个表格：

| 文件路径 | 描述 |
|----------|------|
| /Users/test/file.py | Python 文件 |
| /etc/config.yaml | 配置文件 |

表格结束。
"""

    # 期望结果：表格格式保持不变，路径不被转换
    result = JSONRPCRouter._convert_file_paths_to_links(markdown_table)
    
    print("原始文本:")
    print(markdown_table)
    print("\n转换后:")
    print(result)
    print("\n" + "="*80)
    
    # 验证表格格式未被破坏
    assert "| 文件路径 | 描述 |" in result, "表格头部被破坏"
    assert "|----------|------|" in result, "表格分隔线被破坏"
    assert "| /Users/test/file.py | Python 文件 |" in result, "表格单元格中的路径被错误转换"
    assert "| /etc/config.yaml | 配置文件 |" in result, "表格单元格中的路径被错误转换"
    
    print("✅ 测试通过：表格格式保持完整，路径未被转换")


def test_normal_paths_converted():
    """测试普通文本中的路径正常转换。"""
    text_with_paths = """这里是普通文本。

文件路径：/Users/test/file.py
另一个路径：/etc/config.yaml

结束。
"""

    result = JSONRPCRouter._convert_file_paths_to_links(text_with_paths)
    
    print("原始文本:")
    print(text_with_paths)
    print("\n转换后:")
    print(result)
    print("\n" + "="*80)
    
    # 验证路径被正确转换
    assert "[`/Users/test/file.py`](file:///Users/test/file.py)" in result, "普通路径未被转换"
    assert "[`/etc/config.yaml`](file:///etc/config.yaml)" in result, "普通路径未被转换"
    
    print("✅ 测试通过：普通路径正确转换为链接")


def test_mixed_content():
    """测试混合内容（表格 + 普通路径）。"""
    mixed_content = """这是一个测试。

普通路径：/Users/test/outside.py

| 列1 | 列2 |
|-----|-----|
| /Users/test/inside.py | 表格内 |

另一个普通路径：/Users/test/outside2.py
"""

    result = JSONRPCRouter._convert_file_paths_to_links(mixed_content)
    
    print("原始文本:")
    print(mixed_content)
    print("\n转换后:")
    print(result)
    print("\n" + "="*80)
    
    # 验证表格内的路径未被转换
    assert "| /Users/test/inside.py | 表格内 |" in result, "表格内路径被错误转换"
    
    # 验证表格外的路径被转换
    assert "[`/Users/test/outside.py`](file:///Users/test/outside.py)" in result, "表格外路径未被转换"
    assert "[`/Users/test/outside2.py`](file:///Users/test/outside2.py)" in result, "表格外路径未被转换"
    
    print("✅ 测试通过：混合内容正确处理")


if __name__ == "__main__":
    print("开始测试 _convert_file_paths_to_links 的表格保护功能...\n")
    
    try:
        test_table_with_paths()
        print()
        test_normal_paths_converted()
        print()
        test_mixed_content()
        print("\n" + "="*80)
        print("🎉 所有测试通过！")
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
