"""代码 Diff 提取器 — 从 LLM 响应中解析带文件路径的代码块。

用于 IDEA 插件集成场景，将 LLM 返回的代码块转换为结构化的 diff 数据。
支持两种格式：
1. 显式路径: ```kotlin:src/main/kotlin/Main.kt
2. 注释内路径: ```java\n// src/main/java/Main.java
"""

import re
from typing import List, Dict

from loguru import logger


def extract_code_diffs(content: str) -> List[Dict[str, str]]:
    """从 LLM 响应内容中提取带文件路径的代码块。

    按优先级依次尝试两种匹配策略：
    1. 显式路径格式: ```<lang>:<path> — 语言标识后紧跟冒号和路径
    2. 注释内路径格式: 代码块首行注释中包含文件路径

    Args:
        content: LLM 返回的完整文本内容

    Returns:
        提取到的代码 diff 列表，每项含:
        - file_path: 文件路径
        - language: 编程语言
        - new_content: 新文件内容
    """
    logger.debug("[IDE-Bridge] extract_code_diffs: content_len={}", len(content))
    diffs: list[dict[str, str]] = []
    
    # 策略1: 显式路径（语言标识后跟 `:路径`）
    # 匹配: ```kotlin:src/main/kotlin/Main.kt
    pattern_explicit = r'```(\w+)?\s*:\s*([\w\./\-]+)\n(.*?)```'
    matches = re.finditer(pattern_explicit, content, re.DOTALL)
    
    for match in matches:
        lang = match.group(1) or "text"
        file_path = match.group(2).strip()
        new_content = match.group(3).strip()
        diffs.append({
            "file_path": file_path,
            "language": lang,
            "new_content": new_content
        })
        logger.debug("[IDE-Bridge] 显式路径匹配: path={} lang={} len={}",
                     file_path, lang, len(new_content))

    # 策略2: 注释内路径（无显式路径时回退）
    # 匹配代码块首行的路径注释: ```java\n// src/main/java/Main.java
    if not diffs:
        logger.debug("[IDE-Bridge] 无显式路径匹配，尝试注释内路径回退")
        pattern_comment = r'```(\w+)?\n(.*?)```'
        matches = re.finditer(pattern_comment, content, re.DOTALL)
        for match in matches:
            lang = match.group(1) or "text"
            code_block = match.group(2)
            path_match = re.match(r'\s*//\s*([\w\./\-]+\.\w+)\s*\n', code_block)
            if path_match:
                file_path = path_match.group(1).strip()
                new_content = code_block[path_match.end():].strip()
                diffs.append({
                    "file_path": file_path,
                    "language": lang,
                    "new_content": new_content
                })
                logger.debug("[IDE-Bridge] 注释路径匹配: path={} lang={}", file_path, lang)
    
    logger.info("[IDE-Bridge] extract_code_diffs 完成: diffs_count={}", len(diffs))
    return diffs
