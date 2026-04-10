#!/usr/bin/env python3
import sys
import os

# Add the backend directory to the Python path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from app.plugins.clawith_superpowers.skill_manager import SkillManager

print("SkillManager imported successfully!")

manager = SkillManager()
print(f"Client initialized: {manager.client is not None}")

print("Test passed!")
