#!/usr/bin/env python
"""Quick verification script for superpowers plugin modules."""

import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent / "backend"
sys.path.insert(0, str(backend_path))

print("=== Verifying imports for clawith_superpowers plugin ===")

try:
    from app.plugins.clawith_superpowers.market_client import SuperpowersMarketClient
    print("✓ market_client.py imports successfully")
except Exception as e:
    print(f"✗ market_client.py failed: {e}")

try:
    from app.plugins.clawith_superpowers.adapter import extract_skill_metadata, to_clawith_skill
    print("✓ adapter.py imports successfully")
except Exception as e:
    print(f"✗ adapter.py failed: {e}")

try:
    from app.plugins.clawith_superpowers.skill_manager import SkillManager
    print("✓ skill_manager.py imports successfully")
except Exception as e:
    print(f"✗ skill_manager.py failed: {e}")

try:
    from app.plugins.clawith_superpowers.workflow_runner import WorkflowRunner
    print("✓ workflow_runner.py imports successfully")
except Exception as e:
    print(f"✗ workflow_runner.py failed: {e}")

try:
    from app.plugins.clawith_superpowers.routes import router
    print("✓ routes.py imports successfully")
except Exception as e:
    print(f"✗ routes.py failed: {e}")

try:
    from app.plugins.clawith_superpowers import plugin
    print("✓ __init__.py imports successfully")
except Exception as e:
    print(f"✗ __init__.py failed: {e}")

print("\n=== Testing adapter functions ===")

try:
    from app.plugins.clawith_superpowers.adapter import extract_skill_metadata

    test_content = """---
name: test-skill
description: "This is a test skill"
---

# Test Skill

This is a test skill.
"""
    metadata = extract_skill_metadata("test-skill", test_content)
    assert metadata["name"] == "test-skill"
    assert metadata["description"] == "This is a test skill"
    print("✓ extract_skill_metadata works with frontmatter")
except Exception as e:
    print(f"✗ extract_skill_metadata failed: {e}")

print("\n=== All verification checks complete ===")
