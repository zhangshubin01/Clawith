from __future__ import annotations

import re
from typing import Any, Dict, Optional

import yaml


def extract_skill_metadata(skill_name: str, content: str) -> Dict[str, Any]:
    """Extract metadata from Superpowers SKILL.md.

    Superpowers skills can have YAML frontmatter between --- markers.
    If no frontmatter, extracts name from first heading and uses first
    paragraph as description.
    """
    metadata: Dict[str, Any] = {
        "name": skill_name,
        "description": f"Superpowers skill: {skill_name}",
        "content": content,
    }

    # Check for YAML frontmatter (must start with --- on first line and end with --- on line by itself)
    # Uses a pattern that matches:
    # - First line is exactly --- (with optional whitespace)
    # - Then any content (the YAML frontmatter)
    # - Then a line that is exactly --- (with optional whitespace)
    # - Then the rest of the content
    frontmatter_match = re.match(r'^---\s*$(.*?)^---\s*$(.*)', content, re.DOTALL | re.MULTILINE)
    # Fallback pattern to handle cases where frontmatter markers aren't on their own line
    if not frontmatter_match:
        frontmatter_match = re.match(r'^---\s*(.*?)\s*---\s*(.*)$', content, re.DOTALL)
    if frontmatter_match:
        frontmatter_yaml = frontmatter_match.group(1)
        main_content = frontmatter_match.group(2)
        metadata["content"] = main_content.strip()
        try:
            frontmatter = yaml.safe_load(frontmatter_yaml)
            if isinstance(frontmatter, dict):
                for key, value in frontmatter.items():
                    metadata[key] = value
        except yaml.YAMLError:
            # If YAML parsing fails, just keep the defaults
            pass
    else:
        # Try to extract from first heading and paragraph
        lines = content.splitlines()
        # Find first heading
        for line in lines:
            if line.startswith('# '):
                metadata["name"] = line[2:].strip()
                break
        # Find first non-empty paragraph after heading
        found_heading = False
        for line in lines:
            if line.startswith('# '):
                found_heading = True
                continue
            if found_heading and line.strip():
                metadata["description"] = line.strip()
                break

    # Ensure name is set
    if not metadata.get("name"):
        metadata["name"] = skill_name

    return metadata


def to_clawith_skill(skill_name: str, content: str) -> Dict[str, Any]:
    """Convert Superpowers skill to Clawith Skill create/update dict."""
    meta = extract_skill_metadata(skill_name, content)

    return {
        "name": meta.get("name", skill_name),
        "description": meta.get("description", f"Superpowers: {skill_name}"),
        "content": meta["content"],
        "source": "superpowers",
        "skill_type": "workflow",
        "config_schema": extract_config_schema(meta),
        "enabled": True,
    }


def extract_config_schema(metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract JSON Schema for configuration from metadata."""
    # Some Superpowers skills have parameters defined
    if "parameters" in metadata:
        params = metadata["parameters"]
        if isinstance(params, dict):
            schema = {
                "type": "object",
                "properties": {},
                "required": [],
            }
            for param_name, param_def in params.items():
                prop = {}
                if "description" in param_def:
                    prop["description"] = param_def["description"]
                if "type" in param_def:
                    prop["type"] = param_def["type"]
                if "default" in param_def:
                    prop["default"] = param_def["default"]
                schema["properties"][param_name] = prop
                if param_def.get("required", False):
                    schema["required"].append(param_name)
            return schema
    return None
