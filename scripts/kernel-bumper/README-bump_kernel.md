bump_kernel.py

This script updates `KERNEL_VERSION` in `kernel/*/build-args` to the latest
patch release for each series per kernel.org, and then runs
`make -C kernel update-kernel-yamls` to update references across the repo.

Usage:

```sh
python3 scripts/bump_kernel.py --dry-run
python3 scripts/bump_kernel.py --git-commit
```

Notes:
- The script is idempotent and will no-op when there are no changes.
- When integrating with Renovate, prefer running the script as a `postUpgradeTasks`
  command so Renovate manages branch creation and PRs.
