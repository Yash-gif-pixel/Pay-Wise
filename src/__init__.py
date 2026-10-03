"""Buy or Wait? — AI-powered financial decision agent.

Pipeline order:
    load -> state -> extract -> forecast -> capacity -> candidates
         -> changes -> rank -> render -> validate

`run` wires these together as the CLI entrypoint.
"""

__all__ = [
    "load",
    "state",
    "extract",
    "forecast",
    "capacity",
    "candidates",
    "changes",
    "rank",
    "render",
    "validate",
]
