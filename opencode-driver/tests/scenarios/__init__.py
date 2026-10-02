"""Fake-``opencode`` scenario modules for ``test_e2e.py``.

Each sibling module exposes ``handle(ctx)`` (loaded by ``fake_opencode.py``
via ``FAKE_OC_SCENARIO``, see ``tests/fake_opencode.py``'s own docstring for
the ``Ctx`` API). ``common.py`` holds shared helpers every scenario uses:
role/call-purpose detection from the prompt text, PLAN.md rendering, git
helpers and the fenced-json reply helper.
"""
