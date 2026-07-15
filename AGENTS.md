# AGENTS.md

## Ask Mr. Smith to sync the DocDNA database after editing supported source files.

## DocDNA workflow

Before broad code exploration, use the DocDNA MCP tools for overview, search,
call graph, impact, and data-flow questions. Automatic refresh keeps the index
current while its watcher is enabled. For an immediate refresh, run
`python docdna_refresh.py <project>`; use `--full` after source renames,
deletions, or broad refactors.