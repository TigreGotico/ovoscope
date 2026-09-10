# Contributing

## Dev setup

```bash
git clone https://github.com/TigreGotico/ovoscope
cd ovoscope
pip install -e ".[pydantic]"
```

## Running tests

```bash
pytest test/unittests -q
```

The full suite (including live OVOS integration tests) is slower and more
sensitive to the local environment; CI is the source of truth for whether a
change is green.

## Submitting a change

- Branch off `dev`, not `master`.
- Open a **draft** pull request into `dev`. The maintainer merges when it is
  ready — do not merge your own PR.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for
  commit messages (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- Keep documentation in `docs/` and `README.md` in sync with any behaviour
  change in the same PR.
- Add or update tests for the behaviour you change.

## License

By contributing, you agree your contribution is licensed under the
[Apache 2.0 License](LICENSE) that covers this project.
