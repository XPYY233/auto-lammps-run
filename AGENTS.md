# Development rules

- For this project, read the locally installed `auto-lammps-stage-guide` skill at
  `~/.codex/skills/auto-lammps-stage-guide/SKILL.md`. It routes current norms and
  recorded lessons; it adds no approval gate. Reopen both final-expectation images
  as required below, then resume the current user task instead of expanding setup.
- Prioritize end-to-end automation and the researcher's ability to complete a task.
  Audit and reuse available assets before building replacements or asking the user:
  literature workbench, Zotero PDFs, public supplements/source/potentials, installed
  HPC software, existing analysis and mature visualization tools. Retrieve obtainable
  resources within existing authorization; ask only about genuine unresolved needs.
- Read README.md and docs/PROGRESS.md, then only the relevant specification.
- Reconcile the current audit and private execution checkpoint with the latest user
  instructions. Historical blockers must not override later resolved decisions.
- Use docs/RELIABILITY.md when changing adapters, retrieval or failure recovery;
  distinguish proposed knowledge from verified experience and protect test answers.
- Reuse verified existing HPC software and prior audit evidence before proposing
  downloads or builds. A missing package in one module is not evidence that the
  cluster lacks a compatible engine. The user has explicitly accepted the existing
  compatible engine for the selected author reference run; do not repeat that
  compatibility decision or make the optional build PR a prerequisite for A.
- Prioritize the original author workflow for A. Repeat checks only for changed
  inputs, a real failure, or a specific unresolved condition. Preparation, help
  output and passing software tests are not a submitted or completed simulation.
- Reply briefly in Chinese; give pronunciation for key English terms when useful.
- All changes after initialization use an Issue, branch, PR, CI and authorized review.
- Preserve source history and failure records. Never force push or alter another project.
- Public code must exclude credentials, private paths, library keys, PDFs, private
  evidence, author reference source, hidden targets and real execution logs.
- Every target energy/force/minimization/dynamics evaluation is HPC-only through
  the accounted service, including reference runs, run 0 and engine prechecks.
- Download and retain LAMMPS engine archives, source trees, build directories and
  simulation working files on the user's configured HPC, not the local computer.
  Keep the local web/control service and audit metadata separate. Before removing
  a previously downloaded local artifact, verify its remote size and SHA-256.
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

## Mandatory roadmap and interface review

Before every new work turn, after restoring compacted context, and before revising the plan, open and visually inspect both user-approved images:
- Technical roadmap: `~/.local/share/auto-lammps-private/requirements/approved-technical-roadmap.png`.
- Product interface: `~/.local/share/auto-lammps-private/requirements/approved-product-interface.png`.

Do not rely only on remembered summaries. Both images stay private and must not be committed. If either is unavailable, report that rather than claiming to have reviewed it.

The interface reference defines a light blue/white research workspace: task navigation and recent history at left; the natural-language request, execution stages and results in the center; task information, submission count, downloads and resources at right. Results expose data, plots, atomic structures, trajectories, reports and logs. Preserve the reproduction register with full titles, DOI links and statuses. All displayed results and completion states require real evidence; illustrated values, resource counts and dates are not run authorizations. Keep the first real P-A-B validation ahead of the full interface redesign.

Then reconcile the roadmap with the latest user corrections, which take precedence:
- The product is natural-language research computing; paper reproduction is its validation method.
- P is the paper result. A directly executes the author's source and original workflow/configuration; do not replace A with independently composed inputs or invented invocation parameters. B independently generates its workflow from permitted inputs, without author solution code or target answers.
- Codex owns week-one P-A-B validation. Independent runtime API integration begins in week two, requested after the first validation succeeds.
- Report the B strategy and wait for user confirmation before starting B.
- Target physics runs only through accounted HPC; retain the approved budget, two-submission evaluation limit and full failure history.
- After the first P-A-B validation, redesign and accept the human-facing flow from a blank page, with no developer backend assistance.
- Every reproduction paper must show its full published title and verified DOI/link in the web register, reproduction records and relevant progress reports. Distinguish unselected candidates from pending, in-progress and reproduced tasks; do not report a candidate as a completed reproduction.
- Preserve the domain adapter, verified source test library, potential library, progress feedback, analysis/OVITO, paper status list and history requirements. The picture's illustrative numbers and formulas are not frozen scoring criteria.

Use this check to choose the next action; do not repeat the entire roadmap to the user each turn.
