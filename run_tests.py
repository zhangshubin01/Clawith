#!/usr/bin/env python3
import subprocess
import sys

def run_tests(test_dir):
    """Run pytest tests in the specified directory"""
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', test_dir, '-v'],
            capture_output=True,
            text=True,
            cwd='/Users/shubinzhang/Documents/UGit/Clawith/backend'
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return 1, "", str(e)

if __name__ == "__main__":
    print("Running tests for clawith_superpowers...")
    code, stdout, stderr = run_tests('tests/plugins/clawith_superpowers/')
    print("STDOUT:", stdout)
    print("STDERR:", stderr)
    print(f"Exit code: {code}")

    print("\n" + "="*50 + "\n")

    print("Running tests for clawith_acp...")
    code, stdout, stderr = run_tests('tests/plugins/clawith_acp/')
    print("STDOUT:", stdout)
    print("STDERR:", stderr)
    print(f"Exit code: {code}")