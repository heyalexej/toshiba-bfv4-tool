# Contributing

Contributions are welcome. Please keep protocol behavior covered by offline
tests and keep mutating operations behind explicit confirmation flags.

Before opening a pull request:

```bash
uv run ruff check .
uv run pytest
```

Do not add private network details, serial-number inventories, vendor firmware,
or proprietary application assets. Use documentation and test fixtures that
can be redistributed with the project.
