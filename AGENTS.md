# Agent Inventory development

- Preserve read-only inspection of target projects: never execute/import discovered scripts, packages, or build hooks.
- Keep collected machine paths, command arguments, credentials, reports, SQLite files, and local settings out of Git. Stage source files explicitly.
- After scanner or configuration changes, run `python -m unittest discover -s tests -v` and `python -m reviews.verify_gaps`. Every gap result should be false.
- Run a one-time inventory refresh after a validated change when host access is available. Report partial collection honestly. OS scheduling is responsible for unattended execution; these instructions do not start a daemon.
- Preserve the login-only task principal and single-instance protection when editing Windows scheduling. Do not install a second persistent watcher alongside the scheduled task.
- Keep declared metadata, static evidence, runtime events, and KPI calculations distinct. Do not manufacture usage or business impact.
