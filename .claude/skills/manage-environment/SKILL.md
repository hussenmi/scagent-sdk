---
name: manage-environment
description: >-
  Add a Python package the user needs but the base environment does not ship, into their
  persistent per-runtime overlay. Use when a capability or custom analysis code fails with a
  missing module, or the user explicitly asks to install a package. The install never touches the
  shared locked environment and always requires the user's approval.
---

# Adding a package on request

The analysis runtime is a shared, locked, reproducible base. You cannot `pip install` into it, and
you must never try to run `pip`, `uv`, or `conda` yourself in custom code — those installs would
either fail or corrupt every user's environment. Instead this skill adds a package into the user's
own **overlay**: a persistent, per-user directory layered on top of the base for the analysis
runtime. A package added once stays available across future sessions; it is layered on the path, so
the base is never modified.

Only **additive** packages — ones whose dependencies do not change any package the locked base
already pins — can be added, because a package that pulled a different `numpy`/`pandas`/CUDA build
would break the scientific stack. The tools enforce this for you.

## When to use it

- A capability or `run_analysis_code` failed with `ModuleNotFoundError` / `ImportError` for a
  package that is genuinely useful for the task (e.g. `gseapy`, `mygene`, `biomart`).
- The user explicitly asks to install a package.

Do **not** use it to work around a package that is already present, or to install something the
user did not ask for and the analysis does not need.

## The flow (always two steps, then approval)

1. **Propose.** Call `propose_package_install(package, reason)`. It resolves the package against the
   runtime without installing anything and tells you whether it is:
   - `additive` — safe to add;
   - `conflict` — it would change a base distribution; report the conflict to the user and stop,
     it cannot be added to the overlay;
   - `already_satisfied` — nothing to do;
   - `unavailable` — not on PyPI (likely conda-only); tell the user it needs an operator to add it
     to the base environment.
   Note the returned `install_request_id`.

2. **Ask the user.** Only for an `additive` result. Tell them the package, the reason, and that it
   will be added to their overlay, and ask them to approve. Wait for their reply.

3. **Install.** After they approve, call
   `install_proposed_package(install_request_id, question_asked)` where `question_asked` is the
   approval question you actually asked. The tool records the user's real answer from the session
   log; if no approving turn followed your proposal, it refuses — so never call this before the
   user has answered.

Use `list_installed_packages` to see what the overlay already holds, and `remove_installed_package`
to clean one up (for example after it has been added to the base environment).

After a successful install, the package is importable by `run_analysis_code` and the scientific
capabilities on the same runtime; just retry the step that needed it.
