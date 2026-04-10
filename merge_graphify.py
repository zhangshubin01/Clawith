
import json
from pathlib import Path

# Read chunk 1
chunk1_text = """{{ chunk1_json }}""".replace("{{ chunk1_json }}", '''{{response_json}}'''.replace("{{response_json}}", """{{ chunk1_response }}""".replace("{{ chunk1_response }}", """{{ chunk1_actual }}""")))

# Parse and collect
data1 = json.loads(chunk1_text)

# Read chunk 2
chunk2_text = """{{ chunk2_json }}"""
data2 = json.loads(chunk2_text)

# Read chunk 3
chunk3_text = """{{ chunk3_json }}"""
data3 = json.loads(chunk3_text)

# Merge
all_nodes = data1.get('nodes', []) + data2.get('nodes', []) + data3.get('nodes', [])
all_edges = data1.get('edges', []) + data2.get('edges', []) + data3.get('edges', [])
all_hyperedges = data1.get('hyperedges', []) + data2.get('hyperedges', []) + data3.get('hyperedges', [])
input_tokens = data1.get('input_tokens', 0) + data2.get('input_tokens', 0) + data3.get('input_tokens', 0)
output_tokens = data1.get('output_tokens', 0) + data2.get('output_tokens', 0) + data3.get('output_tokens', 0)

merged = {
    'nodes': all_nodes,
    'edges': all_edges,
    'hyperedges': all_hyperedges,
    'input_tokens': input_tokens,
    'output_tokens': output_tokens,
}

Path('graphify-out/.graphify_semantic_new.json').write_text(json.dumps(merged, indent=2))
print(f'Merged: {len(all_nodes)} nodes, {len(all_edges)} edges')
