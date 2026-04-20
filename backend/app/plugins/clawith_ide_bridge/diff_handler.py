"""Diff handler for IDEA plugin integration."""

import re
from typing import List, Dict, Any


def extract_code_diffs(content: str) -> List[Dict[str, str]]:
    """Extract code blocks with file paths from LLM response.
    
    Matches patterns like:
    ```kotlin:src/main/kotlin/Main.kt
    fun main() { ... }
    ```
    or
    ```java
    // src/main/java/Main.java
    public class Main { ... }
    ```
    """
    diffs = []
    # Pattern 1: Explicit path in info string (```lang:path/to/file)
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

    # If no explicit paths found, try to find paths in comments
    if not diffs:
        pattern_comment = r'```(\w+)?\n(.*?)```'
        matches = re.finditer(pattern_comment, content, re.DOTALL)
        for match in matches:
            lang = match.group(1) or "text"
            code_block = match.group(2)
            # Look for a path comment at the start
            path_match = re.match(r'\s*//\s*([\w\./\-]+\.\w+)\s*\n', code_block)
            if path_match:
                file_path = path_match.group(1).strip()
                new_content = code_block[path_match.end():].strip()
                diffs.append({
                    "file_path": file_path,
                    "language": lang,
                    "new_content": new_content
                })
                
    return diffs
