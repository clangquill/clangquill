```{highlight} shell

```

# Contributing

Contributions are welcome, and they are greatly appreciated! Every little bit
helps, and credit will always be given.

You can contribute in many ways:

## Types of Contributions

### Report Bugs

Report bugs at <https://github.com/renefritze/clangquill/issues>.

If you are reporting a bug, please include:

* Your operating system name and version.
* Any details about your local setup that might be helpful in troubleshooting.
* Detailed steps to reproduce the bug.

### Fix Bugs

Look through the GitHub issues for bugs. Anything tagged with "bug" and "help
wanted" is open to whoever wants to implement it.

### Implement Features

Look through the GitHub issues for features. Anything tagged with "enhancement"
and "help wanted" is open to whoever wants to implement it.

### Write Documentation

clangquill could always use more documentation, whether as part of the
official clangquill docs, in docstrings, or even on the web in blog posts,
articles, and such.

### Submit Feedback

The best way to send feedback is to file an issue at <https://github.com/renefritze/clangquill/issues>.

If you are proposing a feature:

* Explain in detail how it would work.
* Keep the scope as narrow as possible, to make it easier to implement.
* Remember that this is a volunteer-driven project, and that contributions
  are welcome :)

## Get Started!

Ready to contribute? Here's how to set up {}`clangquill` for local development.

1. Fork the {}`clangquill` repo on GitHub.
2. Clone your fork locally:

   ```bash
   git clone https://github.com/renefritze/clangquill
   ```

3. Install your local copy with [uv](https://docs.astral.sh/uv/) (it creates and manages the virtualenv for you):

   ```bash
   cd clangquill/
   uv sync --extra dev
   ```

4. Create a branch for local development:

   ```bash
   git checkout -b name-of-your-bugfix-or-feature
   ```

   Now you can make your changes locally.
5. Make sure you have pre-commit installed and activated:

   ```bash
   uvx pre-commit install
   uvx pre-commit run --all-files
   ```

6. Commit your changes and push your branch to GitHub:

   ```bash
   git add .
   git commit -m "Your detailed description of your changes."
   git push origin name-of-your-bugfix-or-feature
   ```

7. Submit a pull request through the GitHub website.

## Pull Request Guidelines

Before you submit a pull request, check that it meets these guidelines:

1. The pull request should include tests.
2. If the pull request adds functionality, the docs should be updated. Put
   your new functionality into a function with a docstring, and add the
   feature to the list in README.rst.
3. The pull request should work for multiple Python versions.

## Tips

To run a subset of tests:

```bash
uv run pytest tests.test_clangquill
```

### Golden pages

`tests/golden/` holds byte-compared generator output: one directory per
`(fixture, group_by)` pair, plus the two flat files the single-symbol render
uses. Between them they render every bundled `templates/*.md.jinja`, so a
formatting, ordering or whitespace change shows up as a diff rather than
slipping past the substring assertions elsewhere in `tests/test_generator.py`.

When a change is *meant* to alter the output, regenerate instead of hand-editing:

```bash
CLANGQUILL_REGEN_GOLDENS=1 uv run pytest tests/test_generator.py
```

Then read the diff — that is the review. A new bundled template fails
`test_every_bundled_template_is_behind_a_golden_tree` until a golden tree
renders it.

### The comment corpus

`tests/comment_corpus/*.json` is asserted by both comment parsers — the Python
`doxygen_parse` and the C++ `DoxygenCommentParser::parse_raw_text` — so a case
added there covers both, and a grammar change landing on only one side fails on
the other. Prefer it to a parser-specific test whenever the behaviour is
expressible as raw comment in, `CommentModel` out.

## Deploying

TBD
