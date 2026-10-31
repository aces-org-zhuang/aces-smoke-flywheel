"""Declared local virtualisation of the native-dependent `weasyprint` package.

Environment: Windows, no GTK3 / libgobject-2.0-0 available.
Scope: only satisfies InvenTree's import-time requirement
(`report.models` does a hard `sys.exit(1)` when the OSError is raised).
Any actual PDF render call raises loudly, so it can never silently pass.
"""

__version__ = "0.0.0-virtualised"


class HTML:  # pragma: no cover
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "weasyprint is virtualised in this environment: PDF rendering is unavailable"
        )

    def write_pdf(self, *args, **kwargs):
        raise RuntimeError(
            "weasyprint is virtualised in this environment: PDF rendering is unavailable"
        )
