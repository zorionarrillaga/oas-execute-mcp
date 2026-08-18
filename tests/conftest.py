"""Keep pytest from importing the script-style checkers.

Every `tests/test_*.py` in this repo is a self-contained checker: it runs its
assertions at module scope and exits non-zero on failure. That is deliberate —
the suite runs on a bare interpreter with no test framework installed. It also
means pytest must not *import* them, because importing runs the checks and hits
`sys.exit` during collection. `suite_test.py` drives the same scripts as
subprocesses instead, so `pytest` works without changing how they run.
"""

collect_ignore_glob = ["test_*.py", "drill_*.py", "validate_*.py"]
