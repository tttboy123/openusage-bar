"""Private, independently authenticated Plugin API.

The package deliberately avoids eager imports so the stdio bridge can import
the pure ``contracts`` module without opening a database or importing Gateway.
"""

__all__: list[str] = []
