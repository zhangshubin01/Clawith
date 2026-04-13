"""Diff preview handler for IDEA plugin integration."""

import re
from typing import List, Dict, Any


def extract_code_diffs(content: str) -> List[Dict[str, Any]]:
    """Extract code blocks and generate diff structures from LLM response.
    
    Looks for patterns like:
    ```python:path/to/file.py
    ...code...
    ```
    """
    diffs = []
    # Pattern to match code blocks with optional file path in the language identifier
    pattern = r'```(\w+)?(?::([\w\./\-]+))?\n(.*?)```'
    matches = re.finditer(pattern, content, re.DOTALL)
    
    for match in matches:
        lang = match.group(1) or "text"
        file_path = match.group(2)
        new_content = match.group(3)
        
        if file_path:
            diffs.append({
                "file_path": file_path,
                "language": lang,
                "new_content": new_content,
                "change_type": "modify"  # Default to modify, IDE can check existence
            })
            
    return diffs
