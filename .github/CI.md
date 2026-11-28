# Development CI

The **Windows builds** workflow runs on every branch push, on manual requests, and on pull requests from forks. Pull requests within this repository use the checks from the pushed branch; their separate PR event skips the expensive jobs. Fork PRs run against GitHub's proposed merge commit. Superseded runs for the same branch or PR are cancelled. A same-repository PR placeholder does not cancel its branch build.

The Windows workflow installs dependencies, runs required tests, builds both executables, and verifies the installer before uploading artifacts. Required GUI test imports are checked explicitly so missing Qt libraries cannot silently turn a GUI test job green.

The Windows workflow also builds the [Microsoft Store MSIX](../packaging/msix/README.md) in a separate job with its own dependencies. Both standalone executables are checked for Store modules before packaging.

Development artifacts are uploaded to the workflow's Actions page. Public release publishing remains a separate workflow; development builds never publish a release.

For same-repository PRs, review the latest successful **push** run for the exact head commit before merging. Skipped PR-event jobs are not evidence of passing tests. Re-run or update the branch if it needs validation against newer changes on the target branch.
