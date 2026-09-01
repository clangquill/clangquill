"""Configuration dataclass shared by the Sphinx extension and the CLI.

A single :class:`Config` describes one clangquill run: which inputs to parse,
how to invoke libclang, and how to render the resulting MyST. The Sphinx
extension reads the ``clangquill_*`` config values into a :class:`Config`; the
``clangquill build`` CLI constructs one directly. Keeping the schema in one
place means both front ends validate identically and stay in sync.

The Sphinx config name of a field is always ``clangquill_<field-name>`` (see
:data:`CONFIG_FIELDS`), so the extension can register and read every value by
iterating the dataclass rather than repeating each name.
"""

from __future__ import annotations

import math
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

# Sphinx config values are namespaced with this prefix.
CONFIG_PREFIX = "clangquill_"

# Permitted values for ``group_by`` (how generated pages are partitioned).
GROUP_BY_CHOICES = ("symbol", "file", "class", "namespace")


# ``bool`` is an ``int`` subclass, but an int config field is never a flag.
def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_positive(value: float) -> bool:
    """Whether ``value`` is a finite number > 0.

    ``math.isfinite`` converts its argument to a C double, which raises
    ``OverflowError`` -- not ``False`` -- for an ``int`` too large to
    represent as one (``cache_lock_timeout`` is user input, so nothing rules
    out an absurd value arriving as a plain ``int``); treat that the same as
    "not finite" rather than letting a raw ``OverflowError`` escape past the
    documented :class:`ConfigError`.
    """
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return False
    return finite and value > 0


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_str_dict(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


# Per-field type shape ``(field, predicate, expected-description)``, checked up
# front in :meth:`Config.validate` so wrong-typed Sphinx/CLI/Python input fails
# with an actionable :class:`ConfigError` naming the offending ``clangquill_*``
# value instead of a bare ``TypeError`` raised mid-validation (e.g. ``"4" < 0``
# for a string ``tu_batch``).
_TYPE_CHECKS: tuple[tuple[str, Callable[[object], bool], str], ...] = (
    ("jobs", _is_int, "an integer"),
    ("tu_batch", _is_int, "an integer"),
    ("toctree_maxdepth", _is_int, "an integer"),
    ("std", _is_str, "a string"),
    ("output_dir", _is_str, "a string"),
    ("root_document", _is_str, "a string"),
    ("group_by", _is_str, "a string"),
    ("compile_commands", _is_optional_str, "a string or None"),
    ("clang_resource_dir", _is_optional_str, "a string or None"),
    ("cache_dir", _is_optional_str, "a string or None"),
    ("cache_lock_timeout", _is_number, "a number"),
    ("comment_parser", _is_optional_str, "a string or None"),
    ("path_base", _is_optional_str, "a string or None"),
    ("diagnostics_log", _is_optional_str, "a string or None"),
    ("input", _is_str_list, "a list of strings"),
    ("compile_args", _is_str_list, "a list of strings"),
    ("include_dirs", _is_str_list, "a list of strings"),
    ("defines", _is_str_list, "a list of strings"),
    ("template_dirs", _is_str_list, "a list of strings"),
    ("include_undocumented", _is_bool, "a boolean"),
    ("extract_anonymous_namespaces", _is_bool, "a boolean"),
    ("warnings_as_errors", _is_bool, "a boolean"),
    ("templates", _is_str_dict, "a mapping of kind name to template name"),
)


class ConfigError(ValueError):
    """Raised when a :class:`Config` fails validation."""


@dataclass
class Config:
    """Everything one clangquill run needs, validated in :meth:`validate`.

    Field names map onto Sphinx config values by prefixing ``clangquill_``; for
    example :attr:`output_dir` is ``clangquill_output_dir``.
    """

    # -- inputs ---------------------------------------------------------------
    #: Header/source paths (or globs) to parse, relative to the base directory.
    input: list[str] = field(default_factory=list)
    #: Directory holding a ``compile_commands.json`` (overrides std/include/define).
    compile_commands: str | None = None
    #: Extra compiler arguments appended verbatim to every command, whether it
    #: came from the compile DB or from ``std``/``include_dirs``/``defines``.
    #: A ``-x`` here is dropped: the language stays a per-file decision.
    compile_args: list[str] = field(default_factory=list)
    #: ``-I`` include directories.
    include_dirs: list[str] = field(default_factory=list)
    #: C++ standard passed as ``-std=<std>``.
    std: str = "c++20"
    #: ``-D`` preprocessor definitions (``NAME`` or ``NAME=value``).
    defines: list[str] = field(default_factory=list)
    #: Clang resource directory (``-resource-dir``), appended to every command
    #: like :attr:`compile_args` and winning over any the compile DB carries.
    #: ``None`` lets clang decide -- which works only when the libclang doing
    #: the parse sits next to its own builtin headers, so a bundled libclang
    #: (the wheels ship one) generally needs this set.
    clang_resource_dir: str | None = None
    #: Translation units are parsed concurrently across this many threads. ``0``
    #: (the default) auto-detects the CPU count; ``1`` forces a serial parse.
    jobs: int = 0
    #: Number of input files grouped into one libclang translation unit. Grouping
    #: amortises the dominant parse cost — re-parsing the shared ``#include``
    #: closure — across the batch, which speeds up cold builds dramatically.
    #: ``0`` (the default) picks a sensible batch size; ``1`` parses every input
    #: as its own fully isolated translation unit. With ``compile_commands``
    #: configured this is an upper bound rather than the batch size: one unit
    #: can only be given one compiler command, so inputs are first grouped by
    #: the command the database answers with and an input whose flags are
    #: unique is parsed on its own.
    tu_batch: int = 0
    #: Extract what anonymous namespaces contain. ``False`` (the default)
    #: matches Doxygen's ``EXTRACT_ANON_NSPACES = NO``: an anonymous namespace
    #: has internal linkage, so its contents are one translation unit's
    #: implementation detail rather than API anyone can name, include or link
    #: against. When enabled they are documented with ``@anonymous`` in their
    #: qualified name -- the Sphinx C++ domain's spelling for an anonymous
    #: entity -- rather than under the enclosing namespace's name.
    extract_anonymous_namespaces: bool = False

    # -- output ---------------------------------------------------------------
    #: Directory (under the Sphinx srcdir / CWD) that generated pages go into.
    output_dir: str = "api"
    #: Directories searched before the bundled templates for overrides.
    template_dirs: list[str] = field(default_factory=list)
    #: Per-kind template overrides, e.g. ``{"class": "my_class"}``.
    templates: dict[str, str] = field(default_factory=dict)
    #: Directory holding the persistent cache that makes rebuilds incremental
    #: (reuse the parse and rewrite only changed pages). ``None`` disables
    #: caching: each build re-parses into a throwaway temp file and rewrites
    #: every page.
    cache_dir: str | None = None
    #: Seconds an incremental build waits for another build already holding
    #: the single-writer lock on ``cache_dir`` before giving up (see
    #: :mod:`clangquill._lock`). Only one build may run against a given
    #: ``cache_dir`` at a time; concurrent readers of its IR are unaffected.
    #: Ignored when ``cache_dir`` is ``None``, since a stateless build has
    #: nothing to lock.
    cache_lock_timeout: float = 300.0
    #: Emit pages/sections for symbols that carry no documentation comment.
    include_undocumented: bool = True
    #: Comment-parser override (a registered name or a dotted import path).
    comment_parser: str | None = None
    #: How to partition output pages: one of :data:`GROUP_BY_CHOICES`.
    group_by: str = "symbol"
    #: Directory that rendered file paths are shown relative to, resolved
    #: against the base directory (Sphinx srcdir / CWD). None keeps the absolute
    #: paths libclang reports, which leak the build-machine layout; set e.g. the
    #: project root to render stable, reproducible paths in the generated 'File'
    #: headings. Files outside the base keep their absolute path.
    path_base: str | None = None
    #: Path of a plain-text file receiving every libclang diagnostic of the run
    #: — all severities, plus the ``note:`` chain attached to each — resolved
    #: against the base directory (Sphinx srcdir / CWD). ``None`` disables it.
    #: Setting it switches the parse to full-diagnostic capture; the console and
    #: Sphinx warning stream keep showing only errors either way, so this is how
    #: to see the detail without drowning a build in it.
    diagnostics_log: str | None = None
    #: Treat any libclang diagnostic of warning severity or worse as a build
    #: failure: the CLI exits non-zero and the Sphinx extension raises, *after*
    #: the pages have been written. Off by default, because a header that
    #: warns still documents perfectly well; turn it on in CI to gate on a
    #: clean parse. Like :attr:`diagnostics_log` it switches the parse to
    #: full-diagnostic capture, since warnings are otherwise never collected.
    warnings_as_errors: bool = False

    # -- toctree / root -------------------------------------------------------
    #: ``:maxdepth:`` of the generated root toctree.
    toctree_maxdepth: int = 2
    #: Stem of the generated index/toctree page within ``output_dir``.
    root_document: str = "index"

    def validate(self) -> Config:
        """Validate the configuration in place, returning ``self``.

        Raises :class:`ConfigError` with an actionable message on the first
        problem found.
        """
        self._validate_types()
        if not self.input:
            msg = f"{CONFIG_PREFIX}input must list at least one C++ file to parse"
            raise ConfigError(msg)
        if not self.std:
            msg = f"{CONFIG_PREFIX}std must be a non-empty C++ standard, e.g. 'c++20'"
            raise ConfigError(msg)
        if self.group_by not in GROUP_BY_CHOICES:
            choices = ", ".join(GROUP_BY_CHOICES)
            msg = f"{CONFIG_PREFIX}group_by must be one of {{{choices}}}, got {self.group_by!r}"
            raise ConfigError(msg)
        self._validate_output_paths()
        if self.diagnostics_log is not None and not self.diagnostics_log:
            msg = f"{CONFIG_PREFIX}diagnostics_log must be a non-empty path, or None to disable"
            raise ConfigError(msg)
        if self.toctree_maxdepth < 1:
            msg = f"{CONFIG_PREFIX}toctree_maxdepth must be >= 1, got {self.toctree_maxdepth}"
            raise ConfigError(msg)
        if self.jobs < 0:
            msg = f"{CONFIG_PREFIX}jobs must be >= 0 (0 = auto-detect CPU count), got {self.jobs}"
            raise ConfigError(msg)
        if self.tu_batch < 0:
            msg = f"{CONFIG_PREFIX}tu_batch must be >= 0 (0 = auto, 1 = one TU per input), got {self.tu_batch}"
            raise ConfigError(msg)
        if not _is_finite_positive(self.cache_lock_timeout):
            # NaN and +inf both pass a bare `<= 0` check (NaN compares false to
            # everything; +inf is not <= 0 either) and would leave build_lock's
            # deadline unreachable, i.e. an indefinite wait -- exactly what a
            # timeout exists to rule out.
            msg = (
                f"{CONFIG_PREFIX}cache_lock_timeout must be a finite number > 0 seconds, got {self.cache_lock_timeout}"
            )
            raise ConfigError(msg)
        return self

    def _validate_output_paths(self) -> None:
        """Reject an :attr:`output_dir` / :attr:`root_document` that could write outside ``output_dir``.

        ``output_dir`` may legitimately be absolute (the CLI's ``-o`` accepts
        one directly), but is otherwise resolved against a base directory
        (the Sphinx srcdir / CWD, see ``pipeline.build``), so a ``..``
        segment could unintentionally escape that base. ``root_document``
        names a page written as ``output_dir / f"{root_document}.md"`` (see
        ``Generator.generate``), so a value containing a path separator could
        likewise escape ``output_dir`` (e.g. ``root_document="../foo"``).
        """
        if not self.output_dir:
            msg = f"{CONFIG_PREFIX}output_dir must be a non-empty directory name"
            raise ConfigError(msg)
        if ".." in Path(self.output_dir).parts:
            msg = (
                f"{CONFIG_PREFIX}output_dir must not contain '..' segments "
                f"(it is resolved against the Sphinx srcdir / CWD), got {self.output_dir!r}"
            )
            raise ConfigError(msg)
        if not self.root_document:
            msg = f"{CONFIG_PREFIX}root_document must be a non-empty document name"
            raise ConfigError(msg)
        if "/" in self.root_document or "\\" in self.root_document:
            msg = (
                f"{CONFIG_PREFIX}root_document must be a bare document name, not a path "
                f"(it names a page written under output_dir), got {self.root_document!r}"
            )
            raise ConfigError(msg)

    def _validate_types(self) -> None:
        """Reject wrong-typed field values with a field-named :class:`ConfigError`.

        Runs before the value/range checks in :meth:`validate` so that, for
        example, a string ``tu_batch`` reports a clear type error instead of
        blowing up on the ``self.tu_batch < 0`` comparison.
        """
        for name, is_valid, expected in _TYPE_CHECKS:
            value = getattr(self, name)
            if not is_valid(value):
                msg = f"{CONFIG_PREFIX}{name} must be {expected}, got {value!r}"
                raise ConfigError(msg)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> Config:
        """Build a :class:`Config` from a mapping keyed by ``clangquill_*`` names.

        Keys without the :data:`CONFIG_PREFIX` are ignored, so a Sphinx
        ``app.config`` (or any superset mapping) can be passed directly.
        ``input`` accepts a bare string for convenience and is normalised to a
        single-element list.
        """
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            key = CONFIG_PREFIX + f.name
            if key in values and values[key] is not None:
                kwargs[f.name] = values[key]
        if isinstance(kwargs.get("input"), str):
            kwargs["input"] = [kwargs["input"]]
        return cls(**kwargs)


def config_specs() -> Iterable[tuple[str, Any]]:
    """Yield ``(sphinx_name, default)`` pairs for every config field.

    Used by :func:`clangquill.sphinx_ext.setup` to register each value without
    duplicating the field list.
    """
    for f in fields(Config):
        if f.default is not MISSING:
            default = f.default
        elif f.default_factory is not MISSING:
            default = f.default_factory()
        else:  # pragma: no cover - every field has a default
            default = None
        yield CONFIG_PREFIX + f.name, default


#: ``(sphinx_name, default)`` for each registrable config value.
CONFIG_FIELDS = tuple(config_specs())

__all__ = [
    "CONFIG_FIELDS",
    "CONFIG_PREFIX",
    "GROUP_BY_CHOICES",
    "Config",
    "ConfigError",
    "config_specs",
]
