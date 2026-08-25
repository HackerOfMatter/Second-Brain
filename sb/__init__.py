"""Second Brain — a local, privacy-first PARA capture and execution engine.

Layers:
    config      configuration loading
    frontmatter YAML-frontmatter markdown read/write (no third-party dep)
    models      the vault schema, as pydantic models
    vault       Obsidian vault I/O; the vault is the only source of truth
    llm         pluggable completion providers (ollama | cloud | heuristic)
    parser      raw capture -> Project metadata
    workflow    step sequencing, "what's next", step scheduling
    calsync     calendar sinks (local .ics today, Google Calendar behind OAuth)
    api         Starlette app + local web UI
"""

__version__ = "0.1.0"
