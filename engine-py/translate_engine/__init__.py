"""PaperKo translation engine.

A Python package that parses academic PDFs, analyses their layout, translates the
text into Korean via a vLLM (OpenAI-compatible) server and re-renders a
layout-preserving PDF.  See the design docs (00–06) in the repository root.

The package is usable three ways:

* As a library     ``from translate_engine.layout import LayoutEngine``
* As a dev CLI      ``python -m translate_engine translate in.pdf out.pdf``
* As an app sidecar ``python -m translate_engine`` (stdio JSON-RPC 2.0 server)
"""

__version__ = "1.8.7"
PROTOCOL_VERSION = "1.0"
IR_VERSION = "1.0"

__all__ = ["__version__", "PROTOCOL_VERSION", "IR_VERSION"]
