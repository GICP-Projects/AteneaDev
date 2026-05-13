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

## Development Notes

- The API lives in `atenea_api`.
- Auxiliary FastAPI services live in `atenea_services`.

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
