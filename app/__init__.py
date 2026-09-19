"""Why this file exists: its presence makes `app/` a Python PACKAGE, so the other modules can
import each other as `from app.domain import Link`. It is also a good place for a short crib
sheet, since it is the first file a reader opens.

JS/TS vs Python: packages. In Node any folder of files can be imported by path. Python needs an
`__init__.py` (it can be empty) to treat a directory as an importable package, and modules are
imported by DOTTED NAME, not by file path. The name is resolved from where the process was
started (the project root), which is why the tests import `app.x` and `tests.x` the same way.

Ten things that are different from JS/TS, all of which show up in this codebase:

1. INDENTATION IS THE SYNTAX. A block is introduced by a colon and defined by its indentation:
   there are no braces and no semicolons. Mixing tabs and spaces is an error, and the formatter
   (ruff) keeps everything at four spaces.
2. NAMING CONVENTIONS are enforced by habit, not by the compiler: `snake_case` for functions and
   variables, `PascalCase` for classes, `UPPER_CASE` for constants, and a leading underscore
   (`_helper`, `self._store`) means "private, by convention". Nothing stops outside code using
   it; there is no `private` keyword.
3. THERE IS NO `undefined`, only `None`, one singleton compared with `is` (`x is None`). Empty
   strings, `0`, `[]`, `{}` and `None` are all FALSY, so `if items:` means "non-empty", unlike JS.
4. TYPE HINTS ARE NOT ENFORCED AT RUNTIME. `def f(x: int)` accepts a string happily; a separate
   checker (mypy/pyright) reads the hints. FastAPI and pydantic are the exception: they READ the
   hints while the program runs and use them to validate and convert data.
5. EXCEPTIONS are the normal error mechanism (`raise`/`try`/`except`), not return values.
6. `self` is EXPLICIT. Methods take it as their first parameter; there is no implicit `this`.
7. ARGUMENTS are positional or by KEYWORD (`f(1, name="x")`), and `*args`/`**kwargs` collect
   the extras: rest parameters and object spread in one.
8. EVERYTHING IS AN OBJECT, and modules run once, on first import, and are cached. Importing a
   module runs its top-level code, which is why this app builds its objects in a factory
   function instead of at import time.
9. `async`/`await` exist and look like JS, but there is no built-in event loop: uvicorn starts
   one (an "asyncio" loop), and a single blocking call freezes every request on it.
10. DEPENDENCIES live in a virtual environment (an isolated folder of packages, like a local
    node_modules) and are listed in requirements.txt. There is no lockfile step; `pip freeze`
    is the closest thing.
"""
