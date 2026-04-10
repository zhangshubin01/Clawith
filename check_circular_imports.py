#!/usr/bin/env python3
import sys
import importlib
import os

count = 0
circular_errors = []

for path, subdirs, files in os.walk('backend'):
    # Skip virtual environment
    if '.venv' in path or '__pycache__' in path:
        continue
    for file in files:
        if file.endswith('.py'):
            # Get module path
            rel = os.path.relpath(os.path.join(path, file), 'backend')
            module = 'backend.' + rel.replace(os.sep, '.').replace('.py', '')
            if module.endswith('__init__'):
                module = module.rsplit('.', 1)[0]
            if not module:
                continue
            
            # Skip migrations (alembic versions) - they don't need to be imported
            if 'alembic/versions' in path:
                continue
                
            try:
                importlib.import_module(module)
                count += 1
            except Exception as e:
                err_str = str(e)
                # Look for circular import or import errors that are likely circular
                if 'circular' in err_str.lower() or 'cannot import' in err_str.lower():
                    circular_errors.append((module, err_str.splitlines()[0]))

print(f'Total modules imported: {count}')
print(f'Possible circular import issues: {len(circular_errors)}')
for module, err in sorted(circular_errors)[:15]:
    print(f'- {module}: {err}')
if len(circular_errors) > 15:
    print(f'- ... and {len(circular_errors) - 15} more')
