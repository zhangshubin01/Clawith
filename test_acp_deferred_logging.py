#!/usr/bin/env python3
"""Test script to verify ACP deferred write mode logging improvements."""

import json
import os
from pathlib import Path

def check_logs():
    """Check if the new logging is working correctly."""
    log_file = Path(__file__).parent / ".cursor" / "debug-0afa65.log"
    
    if not log_file.exists():
        print(f"❌ Log file not found: {log_file}")
        return False
    
    print(f"✅ Found log file: {log_file}")
    print(f"\n📊 Analyzing recent entries...\n")
    
    with open(log_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Check for key log entries
    has_queued = False
    has_review_entry = False
    has_no_pending = False
    
    for line in lines[-20:]:  # Check last 20 lines
        try:
            entry = json.loads(line)
            msg = entry.get('message', '')
            
            if 'queued ide_write' in msg or 'queued ide_append' in msg:
                has_queued = True
                print(f"✅ Found queued write: {entry['data'].get('path', 'N/A')}")
                print(f"   Queue length: {entry['data'].get('queue_len', 'N/A')}")
                print(f"   Session ID: {entry['data'].get('session_id', 'N/A')[:16]}...")
            
            if 'batch_review_start' in msg:
                has_review_entry = True
                print(f"\n✅ Found review start:")
                print(f"   Pending count: {entry['data'].get('n_pending', 'N/A')}")
                print(f"   Session ID: {entry['data'].get('session_id', 'N/A')[:16]}...")
            
            if 'no_pending_exits' in msg:
                has_no_pending = True
                print(f"\n⚠️  Found no pending writes (this might be the issue)")
        
        except json.JSONDecodeError:
            continue
    
    print("\n" + "="*60)
    print("📋 Summary:")
    print("="*60)
    
    if has_queued:
        print("✅ Writes are being queued correctly")
    else:
        print("❌ No queued writes found - files may not be written or session_id mismatch")
    
    if has_review_entry:
        print("✅ Deferred review is being triggered")
    else:
        print("❌ Deferred review not triggered - check if 'done' message is received")
    
    if has_no_pending:
        print("⚠️  Review was called but queue was empty")
        print("   This suggests session_id mismatch or write failure")
    
    print("\n💡 Recommendations:")
    print("1. Check backend/clawith.log for 'ide_write_file write succeeded' messages")
    print("2. Verify session_id consistency between write and review")
    print("3. Ensure _session_cwds is properly initialized for each session")
    
    return has_queued and has_review_entry

if __name__ == "__main__":
    check_logs()
