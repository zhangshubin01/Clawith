#!/usr/bin/env python3
"""Direct test script to verify adapter functionality without pytest"""

# Add backend directory to path
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.plugins.clawith_superpowers.adapter import extract_skill_metadata, to_clawith_skill

print("Running adapter tests...")

# Test 1: extract_basic_metadata
print("\n1. Testing extract_basic_metadata:")
content1 = """---
name: test-skill
description: "This is a test skill"
---

# Test Skill

This is a test skill.
"""
metadata1 = extract_skill_metadata("test-skill", content1)
try:
    assert metadata1["name"] == "test-skill"
    assert metadata1["description"] == "This is a test skill"
    assert "content" in metadata1
    print("✅ PASS")
except AssertionError as e:
    print(f"❌ FAIL: {e}")
    print(f"  Metadata: {metadata1}")

# Test 2: extract_without_frontmatter
print("\n2. Testing extract_without_frontmatter:")
content2 = """# My Skill
This is my skill description.

## Usage
Some usage here.
"""
metadata2 = extract_skill_metadata("my-skill", content2)
try:
    assert metadata2["name"] == "My Skill"
    assert metadata2["description"] == "This is my skill description."
    print("✅ PASS")
except AssertionError as e:
    print(f"❌ FAIL: {e}")
    print(f"  Metadata: {metadata2}")

# Test 3: to_clawith_skill_conversion
print("\n3. Testing to_clawith_skill_conversion:")
content3 = """---
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
result3 = to_clawith_skill("brainstorming", content3)
try:
    assert result3["name"] == "brainstorming"
    assert result3["source"] == "superpowers"
    assert result3["skill_type"] == "workflow"
    assert result3["config_schema"] is not None
    assert "topic" in result3["config_schema"]["properties"]
    print("✅ PASS")
except AssertionError as e:
    print(f"❌ FAIL: {e}")
    print(f"  Result: {result3}")

print("\n=== All tests completed ===")
