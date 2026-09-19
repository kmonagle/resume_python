# Why this file exists: its presence makes `app/` a Python PACKAGE, so the other
# modules can import each other as `from app.domain import Link`.
#
# JS/TS vs Python: in Node any folder of files can be imported by path. Python
# needs an `__init__.py` (it can be empty, as here) to treat a directory as an
# importable package. Modules are imported by dotted name, not by file path, and
# the name comes from where the process was started (the project root).
