"""Reading the warning stream of an in-process ``sphinx-build``.

The e2e suite builds several Sphinx applications in one interpreter, and Sphinx
re-registers its own nodes, directives and roles for every one of them. From the
second application onward that produces a run of ``is already registered``
warnings that belong to the test harness, not to the project under test -- and
they are enough to set ``app.statuscode`` to 1 under ``warningiserror``, which
would make a warnings-as-errors assertion fail for every build but the first.

So the tests assert on the warnings this module keeps rather than on
``statuscode`` alone.
"""

from __future__ import annotations

#: Substring of the harness noise described above. Sphinx phrases it three ways
#: (node, directive, role), all of them containing this.
_ALREADY_REGISTERED = "is already registered"


def project_warnings(text: str) -> list[str]:
    """Return the warnings in ``text`` that the project is answerable for."""
    return [line for line in text.splitlines() if line.strip() and _ALREADY_REGISTERED not in line]
