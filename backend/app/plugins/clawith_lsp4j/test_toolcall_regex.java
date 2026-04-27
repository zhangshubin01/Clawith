import java.util.regex.Pattern;
import java.util.regex.Matcher;

public class TestToolCallRegex {
    public static void main(String[] args) {
        // 插件的正则表达式
        Pattern pattern = Pattern.compile(
            "```([\\w#+.-]+)::([^\\n]+)::([^\\n]+)\\n+(.*?)```",
            Pattern.DOTALL
        );
        
        // 测试用例 1：正确的格式（4 个字段）
        String test1 = "```toolCall::list_files::abc-123::INIT\n```";
        System.out.println("测试 1: " + test1);
        Matcher m1 = pattern.matcher(test1);
        if (m1.find()) {
            System.out.println("  group(1): " + m1.group(1));
            System.out.println("  group(2): " + m1.group(2));
            System.out.println("  group(3): " + m1.group(3));
            System.out.println("  group(4): [" + m1.group(4) + "]");
            
            String[] parts = m1.group(2).split("::");
            System.out.println("  解析结果: toolName=" + parts[0] + ", toolCallId=" + parts[1]);
        } else {
            System.out.println("  ❌ 不匹配");
        }
        
        System.out.println();
        
        // 测试用例 2：错误的格式（只有 3 个字段，缺少 status）
        String test2 = "```toolCall::list_files::abc-123\n```";
        System.out.println("测试 2: " + test2);
        Matcher m2 = pattern.matcher(test2);
        if (m2.find()) {
            System.out.println("  group(1): " + m2.group(1));
            System.out.println("  group(2): " + m2.group(2));
            System.out.println("  group(3): " + m2.group(3));
            System.out.println("  group(4): [" + m2.group(4) + "]");
            
            String[] parts = m2.group(2).split("::");
            System.out.println("  解析结果: toolName=" + parts[0] + ", toolCallId=" + (parts.length > 1 ? parts[1] : "null"));
        } else {
            System.out.println("  ❌ 不匹配");
        }
    }
}
