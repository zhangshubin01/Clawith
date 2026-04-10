#!/usr/bin/env python
"""Test async methods of the skill manager."""

import sys
from pathlib import Path

backend_path = Path(__file__).parent / "backend"
sys.path.insert(0, str(backend_path))

import asyncio
from app.plugins.clawith_superpowers.skill_manager import SkillManager


async def test_manager():
    print("Testing SkillManager initialization...")
    manager = SkillManager()
    assert manager is not None
    print("✓ SkillManager initialized successfully")

    print("\nTesting client property...")
    assert hasattr(manager, "client")
    print("✓ Client property exists")

    print("\nTesting sync_skills() method exists...")
    assert hasattr(manager, "sync_skills")
    assert asyncio.iscoroutinefunction(manager.sync_skills)
    print("✓ sync_skills() is an async method")

    print("\nTesting install_skill() method exists...")
    assert hasattr(manager, "install_skill")
    assert asyncio.iscoroutinefunction(manager.install_skill)
    print("✓ install_skill() is an async method")

    print("\nTesting update_all() method exists...")
    assert hasattr(manager, "update_all")
    assert asyncio.iscoroutinefunction(manager.update_all)
    print("✓ update_all() is an async method")

    print("\nTesting get_installed_skills() method exists...")
    assert hasattr(manager, "get_installed_skills")
    assert asyncio.iscoroutinefunction(manager.get_installed_skills)
    print("✓ get_installed_skills() is an async method")

    print("\nTesting uninstall_skill() method exists...")
    assert hasattr(manager, "uninstall_skill")
    assert asyncio.iscoroutinefunction(manager.uninstall_skill)
    print("✓ uninstall_skill() is an async method")

    print("\n✅ All async methods verified")


if __name__ == "__main__":
    try:
        asyncio.run(test_manager())
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()