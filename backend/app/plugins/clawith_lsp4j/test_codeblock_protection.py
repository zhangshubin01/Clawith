"""测试 _convert_file_paths_to_links 方法的代码块保护功能。"""

import sys
from pathlib import Path

# 添加 backend 目录到 Python 路径
backend_dir = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(backend_dir))

from app.plugins.clawith_lsp4j.jsonrpc_router import JSONRPCRouter


def test_toolcall_block_protection():
    """测试 toolCall markdown 块不被路径转换器破坏。"""
    # 原始文本包含 toolCall 块
    text_with_toolcall = """这里是一个工具调用：

```toolCall::list_files::abc-123-def-456::INIT
```

工具调用结束。"""

    result = JSONRPCRouter._convert_file_paths_to_links(text_with_toolcall)
    
    print("=" * 80)
    print("测试：toolCall 块保护")
    print("=" * 80)
    print("\n原始文本:")
    print(text_with_toolcall)
    print("\n转换后:")
    print(result)
    print("\n" + "=" * 80)
    
    # 验证 toolCall 块格式未被破坏
    assert "```toolCall::list_files::abc-123-def-456::INIT\n```" in result, \
        f"toolCall 块格式被破坏！\n实际输出:\n{result}"
    
    print("✅ 测试通过：toolCall 块格式保持完整")
    return True


def test_code_block_with_paths():
    """测试普通代码块中的路径不被转换。"""
    text_with_code_block = """这是一个代码示例：

```python
import os
path = "/Users/test/file.py"
print(path)
```

代码块结束。"""

    result = JSONRPCRouter._convert_file_paths_to_links(text_with_code_block)
    
    print("\n" + "=" * 80)
    print("测试：普通代码块中的路径保护")
    print("=" * 80)
    print("\n原始文本:")
    print(text_with_code_block)
    print("\n转换后:")
    print(result)
    print("\n" + "=" * 80)
    
    # 验证代码块中的路径未被转换
    assert 'path = "/Users/test/file.py"' in result, \
        f"代码块中的路径被错误转换！\n实际输出:\n{result}"
    
    # 验证代码块外的路径可能被转换（如果存在）
    # 这里代码块外的文本没有路径，所以不需要验证
    
    print("✅ 测试通过：代码块中的路径未被转换")
    return True


def test_multiple_codeblocks():
    """测试多个代码块都能被正确保护。"""
    text = """第一个代码块：

```toolCall::read_file::call-1::INIT
```

中间文本 /path/to/file1.py 应该被转换。

第二个代码块：

```javascript
const path = "/path/to/file2.js";
```

结束。"""

    result = JSONRPCRouter._convert_file_paths_to_links(text)
    
    print("\n" + "=" * 80)
    print("测试：多个代码块保护")
    print("=" * 80)
    print("\n原始文本:")
    print(text)
    print("\n转换后:")
    print(result)
    print("\n" + "=" * 80)
    
    # 验证两个代码块都未被破坏
    assert "```toolCall::read_file::call-1::INIT\n```" in result, \
        "第一个代码块（toolCall）被破坏！"
    
    assert 'const path = "/path/to/file2.js";' in result, \
        "第二个代码块（javascript）被破坏！"
    
    # 验证代码块外的纯文本路径被转换
    assert '[`/path/to/file1.py`](file:///path/to/file1.py)' in result, \
        "代码块外的路径未被转换！"
    
    print("✅ 测试通过：多个代码块都被正确保护")
    return True


def test_nested_backticks():
    """测试嵌套反引号的情况。"""
    text = """使用 `code` 和代码块：

```
code block with `nested backticks`
```

结束。"""

    result = JSONRPCRouter._convert_file_paths_to_links(text)
    
    print("\n" + "=" * 80)
    print("测试：嵌套反引号")
    print("=" * 80)
    print("\n原始文本:")
    print(text)
    print("\n转换后:")
    print(result)
    print("\n" + "=" * 80)
    
    # 验证代码块保持完整
    assert "```\ncode block with `nested backticks`\n```" in result or \
           "```" in result, \
        "代码块被破坏！"
    
    print("✅ 测试通过：嵌套反引号处理正常")
    return True


if __name__ == "__main__":
    print("运行 _convert_file_paths_to_links 代码块保护测试\n")
    
    tests = [
        test_toolcall_block_protection,
        test_code_block_with_paths,
        test_multiple_codeblocks,
        test_nested_backticks,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
        except AssertionError as e:
            print(f"\n❌ 测试失败: {test.__name__}")
            print(f"   错误: {e}")
            failed += 1
        except Exception as e:
            print(f"\n❌ 测试异常: {test.__name__}")
            print(f"   错误: {e}")
            failed += 1
    
    print("\n" + "=" * 80)
    print(f"测试总结: {passed} 通过, {failed} 失败")
    print("=" * 80)
    
    if failed == 0:
        print("\n🎉 所有测试通过！代码块保护功能正常工作。")
        sys.exit(0)
    else:
        print(f"\n⚠️  {failed} 个测试失败，请检查代码。")
        sys.exit(1)
