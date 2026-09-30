"""r20: trioctl's git reads ignore `git replace` refs (real git, no mocks).

The acceptance pre-create check reads the target's committed metrics/ set;
a replace ref pointing the target tip at an API-7 commit must not turn an
API-6 target into an accepted one."""
from __future__ import annotations

import re
import subprocess

from r16_harness import SCRIPT, git, init_repo, load

t = load("trioctl_r20_replace", SCRIPT)


def test_target_metrics_api_reads_the_real_tip(tmp_path):
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n"})
    api7 = git(home, "rev-parse", "HEAD")
    for name in ("trio-metrics.py", "trio_loop.py"):
        path = home / "metrics" / name
        path.write_text(re.sub(r"^METRICS_API = 7$", "METRICS_API = 6", path.read_text(),
                               count=1, flags=re.M))
    git(home, "commit", "-qam", "vendor an API-6 core")
    api6 = git(home, "rev-parse", "HEAD")
    assert t._target_metrics_api(home, "main") == 6
    git(home, "replace", api6, api7)
    naive = subprocess.run(["git", "-C", str(home), "show", "refs/heads/main:metrics/trio-metrics.py"],
                           capture_output=True, text=True, check=True).stdout
    assert "METRICS_API = 7" in naive  # plain git honours the replacement ...
    assert t._target_metrics_api(home, "main") == 6  # ... trioctl does not
    real_tree = git(home, "--no-replace-objects", "rev-parse", f"{api6}^{{tree}}")
    assert git(home, "rev-parse", f"{api6}^{{tree}}") != real_tree  # plain git: the replacement's tree
    assert t._git_read(home, "rev-parse", "HEAD^{tree}") == real_tree
