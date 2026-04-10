import pytest
from app.plugins.clawith_superpowers.adapter import extract_skill_metadata, to_clawith_skill


def test_extract_basic_metadata():
    content = """---
name: test-skill
description: "This is a test skill"
---

# Test Skill

This is a test skill.
"""
    metadata = extract_skill_metadata("test-skill", content)
    assert metadata["name"] == "test-skill"
    assert metadata["description"] == "This is a test skill"
    assert "content" in metadata


def test_extract_without_frontmatter():
    content = """# My Skill
This is my skill description.

## Usage
Some usage here.
"""
    metadata = extract_skill_metadata("my-skill", content)
    assert metadata["name"] == "My Skill"
    assert metadata["description"] == "This is my skill description."


def test_to_clawith_skill_conversion():
    content = """---
name: brainstorming
description: "Brainstorming for creative work"
parameters:
  topic:
    type: string
    description: "Topic to brainstorm"
    required: true
---

# Brainstorming
"""
    result = to_clawith_skill("brainstorming", content)
    assert result["name"] == "brainstorming"
    assert result["source"] == "superpowers"
    assert result["skill_type"] == "workflow"
    assert result["config_schema"] is not None
    assert "topic" in result["config_schema"]["properties"]


def test_invalid_yaml_frontmatter():
    content = """---
invalid: yaml: structure
---

# My Skill
Description here
"""
    metadata = extract_skill_metadata("my-skill", content)
    assert metadata["name"] == "my-skill"  # Should use default
    assert "invalid" not in metadata  # Should not include invalid fields


def test_no_frontmatter_no_headings():
    content = """This is a skill with no frontmatter and no headings.
It's just plain text content.
"""
    metadata = extract_skill_metadata("no-heading-skill", content)
    assert metadata["name"] == "no-heading-skill"  # Should use skill_name
    assert metadata["description"] == "Superpowers skill: no-heading-skill"  # Should use default description


def test_parameters_with_different_types():
    content = """---
name: parameter-test
description: "Test parameters with different types"
parameters:
  string_param:
    type: string
    description: "A string parameter"
    default: "default value"
  number_param:
    type: number
    description: "A number parameter"
    default: 42
  boolean_param:
    type: boolean
    description: "A boolean parameter"
    default: true
  array_param:
    type: array
    description: "An array parameter"
    default: ["item1", "item2"]
  object_param:
    type: object
    description: "An object parameter"
    default: {"key": "value"}
---
Content here
"""
    result = to_clawith_skill("parameter-test", content)
    assert result["config_schema"] is not None
    properties = result["config_schema"]["properties"]

    assert "string_param" in properties
    assert properties["string_param"]["type"] == "string"
    assert properties["string_param"]["default"] == "default value"

    assert "number_param" in properties
    assert properties["number_param"]["type"] == "number"
    assert properties["number_param"]["default"] == 42

    assert "boolean_param" in properties
    assert properties["boolean_param"]["type"] == "boolean"
    assert properties["boolean_param"]["default"] == True

    assert "array_param" in properties
    assert properties["array_param"]["type"] == "array"
    assert properties["array_param"]["default"] == ["item1", "item2"]

    assert "object_param" in properties
    assert properties["object_param"]["type"] == "object"
    assert properties["object_param"]["default"] == {"key": "value"}


def test_frontmatter_with_proper_newlines():
    content = """---
name: proper-frontmatter
description: "Frontmatter with --- on own lines"
---
This is content after frontmatter.
It should work correctly.
"""
    metadata = extract_skill_metadata("test-skill", content)
    assert metadata["name"] == "proper-frontmatter"
    assert metadata["description"] == "Frontmatter with --- on own lines"
    assert metadata["content"] == "This is content after frontmatter.\nIt should work correctly."
