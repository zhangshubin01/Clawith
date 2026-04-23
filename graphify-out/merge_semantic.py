#!/usr/bin/env python3
"""Merge all semantic extraction chunks into .graphify_semantic_new.json"""

import json
from pathlib import Path

# Base output directory
out_dir = Path('.')

all_nodes = []
all_edges = []
all_hyperedges = []
total_input = 0
total_output = 0

# Read all chunk files - they are saved as chunk_*.json in the temp directory
for i in range(1, 23):
    chunk_file = out_dir / f'.chunk_{i}.json'
    if not chunk_file.exists():
        print(f"Warning: {chunk_file} not found, skipping")
        continue

    try:
        data = json.loads(chunk_file.read_text())
        if 'nodes' in data:
            all_nodes.extend(data['nodes'])
        if 'edges' in data:
            all_edges.extend(data['edges'])
        if 'hyperedges' in data:
            all_hyperedges.extend(data['hyperedges'])
        total_input += data.get('input_tokens', 0)
        total_output += data.get('output_tokens', 0)
        print(f"Chunk {i}: {len(data.get('nodes', []))} nodes, {len(data.get('edges', []))} edges")
    except Exception as e:
        print(f"Error reading {chunk_file}: {e}")
        continue

# Deduplicate nodes by id
seen_ids = set()
deduped_nodes = []
for node in all_nodes:
    if node['id'] not in seen_ids:
        seen_ids.add(node['id'])
        deduped_nodes.append(node)

result = {
    'nodes': deduped_nodes,
    'edges': all_edges,
    'hyperedges': all_hyperedges,
    'input_tokens': total_input,
    'output_tokens': total_output,
}

out_path = out_dir / '.graphify_semantic_new.json'
out_path.write_text(json.dumps(result, indent=2))
print(f"\nMerged result: {len(deduped_nodes)} nodes, {len(all_edges)} edges")
print(f"Total tokens: input={total_input}, output={total_output}")
print(f"Saved to {out_path}")
