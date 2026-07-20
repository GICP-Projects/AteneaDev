# Contributing

Thank you for your interest in contributing to Atenea.

This repository is maintained as research software. Contributions should keep the
project reproducible, documented, and suitable for academic inspection and reuse.

## Maintainer

- Alfonso de Paz (GitHub: [@srfonso](https://github.com/srfonso))

## How to Contribute

Before opening a pull request, please:

1. Open an issue or contact the maintainer to discuss substantial changes.
2. Keep changes focused and avoid unrelated refactors.
3. Update the relevant documentation when behavior, configuration, deployment,
   or public APIs change.
4. Avoid committing credentials, private datasets, generated database volumes,
   model files, logs, or environment files with real values.
5. Run the relevant tests or validation steps for the affected component.

Each Python component is managed as an independent uv project. Run
`uv sync --locked` and project commands through `uv run` from the component
directory. Commit `pyproject.toml` and `uv.lock` changes together.

For API changes, start its isolated PostgreSQL service and run the suite as described
in [`src/atenea_api/docs/testing.md`](./src/atenea_api/docs/testing.md). External
services must be mocked in the default automated suite.

The independent NER and sentiment test environments and their optional model smoke
tests are documented in
[`src/atenea_services/docs/testing.md`](./src/atenea_services/docs/testing.md).

## Development Notes

- The API lives in `src/atenea_api`.
- Auxiliary FastAPI services live in `src/atenea_services`.

## Reporting Issues

When reporting a bug, include:

- A clear description of the problem.
- The component affected.
- Steps to reproduce the issue.
- The expected and observed behavior.
- Relevant logs or error messages with secrets removed.

## License

By contributing, you agree that your contribution will be distributed under the
same license as this repository.
