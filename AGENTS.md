# Clawith Project Instructions

This file is the project-level entry point for agent instructions.

## Primary Source of Project Rules

For this repository, the canonical project instructions live under:

- `.agents/rules/`
- `.agents/workflows/`

When working in this project, read and follow those files first. If this file and a file under `.agents/` ever conflict, prefer the more specific file under `.agents/`.

## Required Read Order

At the start of work on Clawith, use this order:

1. `.agents/workflows/read_architecture.md`
2. Relevant files under `.agents/rules/`

In practice:

- For general design, implementation, or feature questions, read `.agents/rules/design_and_dev.md`
- For deployment and environment updates, read `.agents/rules/deploy.md`
- For GitHub-related work, read `.agents/rules/github.md`
- For versioning and release work, read `.agents/rules/release.md`

## Notes

- The architecture document currently present in this repository is `ARCHITECTURE_SPEC_EN.md`
- Do not invent alternative instruction filenames when the real rules already exist under `.agents/`

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `python3 -c "from graphify.watch import _rebuild_code; from pathlib import Path; _rebuild_code(Path('.'))"` to keep the graph current
