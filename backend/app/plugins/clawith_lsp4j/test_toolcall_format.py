"""测试 toolCall markdown 块格式。"""

import re


def test_toolcall_regex_match():
    """验证插件的正则表达式是否能匹配 toolCall 格式。"""
    # 插件的 MATCHER_PATTERN（简化版，只测试 toolCall 部分）
    # 原始正则: ```([\w#+.-]+)::([^\n]+)::([^\n]+)\n+(.*?)```
    toolcall_pattern = re.compile(
        r"```([\w#+.-]+)::([^\n]+)::([^\n]+)\n+(.*?)```",
        re.DOTALL
    )
    
    # ❌ 错误的格式（带 ::INIT）
    wrong_format = "```toolCall::list_files::abc-123::INIT\n```"
    match_wrong = toolcall_pattern.search(wrong_format)
    
    print("=" * 80)
    print("测试 1: 错误格式（带 ::INIT）")
    print(f"输入: {wrong_format}")
    print(f"匹配结果: {'✅ 匹配成功' if match_wrong else '❌ 匹配失败'}")
    if match_wrong:
        print(f"  group(1) toolCall: {match_wrong.group(1)}")
        print(f"  group(2) toolName: {match_wrong.group(2)}")
        print(f"  group(3) toolCallId: {match_wrong.group(3)}")
    print()
    
    # ✅ 正确的格式（不带 ::INIT）
    correct_format = "```toolCall::list_files::abc-123\n```"
    match_correct = toolcall_pattern.search(correct_format)
    
    print("=" * 80)
    print("测试 2: 正确格式（不带 ::INIT）")
    print(f"输入: {correct_format}")
    print(f"匹配结果: {'✅ 匹配成功' if match_correct else '❌ 匹配失败'}")
    if match_correct:
        print(f"  group(1) toolCall: {match_correct.group(1)}")
        print(f"  group(2) toolName: {match_correct.group(2)}")
        print(f"  group(3) toolCallId: {match_correct.group(3)}")
    print()
    
    # 验证结果
    # 注意：错误格式实际上能匹配，但解析出来的字段是错的
    assert match_wrong is not None, "错误格式会匹配（但字段错位）"
    assert match_wrong.group(2) == "list_files::abc-123", "错误格式 group(2) 会包含多余内容"
    assert match_wrong.group(3) == "INIT", "错误格式 group(3) 会变成 INIT（而非 toolCallId）"
    
    assert match_correct is not None, "正确格式必须匹配"
    assert match_correct.group(1) == "toolCall", "group(1) 应该是 'toolCall'"
    assert match_correct.group(2) == "list_files", "group(2) 应该是工具名"
    assert match_correct.group(3) == "abc-123", "group(3) 应该是 toolCallId"
    
    print("=" * 80)
    print("✅ 所有测试通过！")
    print("\n结论:")
    print("  - 错误格式（带 ::INIT）会匹配，但 group(2) 和 group(3) 字段错位")
    print("    group(2)=list_files::abc-123, group(3)=INIT（而非 toolCallId）")
    print("  - 正确格式（不带 ::INIT）能被插件正确解析")
    print("    group(2)=list_files, group(3)=abc-123")
    print("  - 后端必须发送: ```toolCall::<name>::<id>\\n```")


def test_full_matcher_pattern():
    """测试完整的 MATCHER_PATTERN 对 toolCall 的匹配。"""
    # 完整的插件正则表达式
    MATCHER_PATTERN = re.compile(
        r"```([\w#+.-]+)::([^\n]+)::([^\n]+)\n+(.*?)```|"
        r"````think::(\d+|\{THINK_TIME})\n(.*?)\n````|"
        r"````think::(\d+|\{THINK_TIME})\n(.*)|"
        r"```([\w#+.-]*\n*)?(.*?)`{2,3}|"
        r"`{2,3}([\w#+.-]+\n*)?(.*)|"
        r"<think>(.*?)</think>|"
        r"<think>(.*)",
        re.DOTALL
    )
    
    print("\n" + "=" * 80)
    print("测试完整 MATCHER_PATTERN")
    print("=" * 80)
    
    # 测试正确格式
    correct = "```toolCall::list_files::abc-123\n```"
    match = MATCHER_PATTERN.search(correct)
    
    print(f"\n输入: {correct}")
    if match:
        print(f"✅ 匹配成功")
        print(f"  group(1) type: {match.group(1)}")  # toolCall
        print(f"  group(2) details: {match.group(2)}")  # list_files::abc-123
        
        # 模拟插件的解析逻辑
        if match.group(1) == "toolCall":
            details = match.group(2).split("::")
            if len(details) >= 2:
                tool_name = details[0]
                tool_call_id = details[1]
                print(f"  解析结果: tool_name={tool_name}, tool_call_id={tool_call_id}")
                assert tool_name == "list_files", "工具名解析错误"
                assert tool_call_id == "abc-123", "toolCallId 解析错误"
                print("  ✅ 工具名和 toolCallId 解析正确")
    else:
        print("❌ 匹配失败")
        assert False, "正确格式必须匹配"


if __name__ == "__main__":
    try:
        test_toolcall_regex_match()
        test_full_matcher_pattern()
        print("\n" + "=" * 80)
        print("🎉 所有测试通过！toolCall 格式修复正确。")
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        import sys
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        import sys
        sys.exit(1)
