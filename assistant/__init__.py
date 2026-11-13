"""Phase 2: the owner's personal assistant (the Shorts pipeline is one division).

Layers (see ``docs/ASSISTANT.md``)::

    runtime (main agent -> sub-agents)  ->  tools (allowlist)  ->  vault (markdown) + store (SQLite)
                 ^                                ^
             persona / registry (agents/*.md, skills/*/SKILL.md)

Importing this package (or any module in it) has no side effects.
"""
