"""精确验证 toolCall 正则匹配行为。"""

import re

# 插件的完整正则（toolCall 部分）
TOOLCALL_PATTERN = re.compile(
    r"```([\w#+.-]+)::([^\n]+)::([^\n]+)\n+(.*?)```",
    re.DOTALL
)

test_cases = [
    ("```toolCall::list_files::abc-123::INIT\n```", "带 ::INIT 的完整格式"),
    ("```toolCall::list_files::abc-123\n```", "不带 ::INIT 的格式"),
]

for text, desc in test_cases:
    print("=" * 80)
    print(f"测试: {desc}")
    print(f"输入: {repr(text)}")
    
    match = TOOLCALL_PATTERN.search(text)
    if match:
        print(f"✅ 匹配成功")
        print(f"  group(1) = {repr(match.group(1))}")
        print(f"  group(2) = {repr(match.group(2))}")
        print(f"  group(3) = {repr(match.group(3))}")
        print(f"  group(4) = {repr(match.group(4))}")
        
        # 模拟插件解析逻辑
        s1 = match.group(1)  # toolCall
        s2 = match.group(2)  # list_files::abc-123 或 list_files::abc-123::INIT
        s3 = match.group(3)  # INIT 或 abc-123
        
        if s1 == "toolCall":
            parts = s2.split("::")
            print(f"\n  插件解析逻辑:")
            print(f"    s2.split('::') = {parts}")
            if len(parts) >= 2:
                tool_name = parts[0]
                tool_call_id = parts[1]
                tool_call_status = s3
                print(f"    toolName = {repr(tool_name)}")
                print(f"    toolCallId = {repr(tool_call_id)}")
                print(f"    toolCallStatus = {repr(tool_call_status)}")
                
                # 验证
                if tool_name == "list_files" and tool_call_id == "abc-123":
                    print(f"  ✅ 解析正确！")
                else:
                    print(f"  ❌ 解析错误！")
    else:
        print(f"❌ 匹配失败")
    
    print()
