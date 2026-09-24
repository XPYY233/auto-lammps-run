# Development rules

- Read README.md and docs/PROGRESS.md, then only the relevant specification.
- Reply briefly in Chinese; give pronunciation for key English terms when useful.
- All changes after initialization use an Issue, branch, PR, CI and authorized review.
- Preserve source history and failure records. Never force push or alter another project.
- Public code must exclude credentials, private paths, library keys, PDFs, private
  evidence, author reference source, hidden targets and real execution logs.
- Every target energy/force/minimization/dynamics evaluation is HPC-only through
  the accounted service, including reference runs, run 0 and engine prechecks.
- Do not submit until scope, resource/API limits and scoring rules are approved.
- Maximum two submissions per formal evaluation; uncertain submission means
  reconcile first. The second attempt cannot see target answers or score differences.
- Separate scheduler completion, output validity and scientific success.
- Week one: Codex is responsible for the real P-A-B workflow validation. Before B, report the strategy and wait for user confirmation. After the first validation succeeds, request API configuration for week two. From week two onward, model runtime uses the separately configured API. Record Codex-assisted validation separately from API automation acceptance.
- No copying existing code until provenance and license obligations are checked.
- No claim of blind isolation from directories or prompts alone: enforce identities,
  mounts, tools and network allowlists before any formal evaluation.
- Install optional web/test/geometry dependencies before the full suite: `python3 -m pip install -e '.[web,test,geometry]'`.
- Offline checks: `python3 -m unittest discover -s tests -v` and
  `python3 scripts/check_public_tree.py`. No simulation or paid API in CI.
- Specifications: docs/GOALS.md, ARCHITECTURE.md, EVALUATION.md, SECURITY.md,
  DEPLOYMENT.md, AUDIT.md, CANDIDATES.md, LICENSE_INVENTORY.md and PROGRESS.md.
