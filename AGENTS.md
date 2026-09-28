# Development rules

最新用户明确批准（2026-09-27）：B 路径与独立子 Agent 已批准；所有资源额度限制取消，B 仍最多两次。原开发时限、Codex 保留比例和 `32*b+b²` 核时上限不再作为阻塞；集群七天硬限制、32 核/8 GiB 单作业配置、并发 1、完整记账及历史保留继续。实际 API 仍须用户提供本项目密钥，不能借用其他项目凭据。

历史修订（其中核时上限已被 2026-09-27 全额解除取代）：第一周作者参考 A 可继续提交直至跑通，不限次数；每次失败和费用保留。B 及最终产品最多两次，进入 B 前仍须先汇报策略并获确认。A 使用 32 核、8 GiB 内存、并发 1，遵守集群七天时限；B 累计核时仍按完整 A 的实际小时数 b 计算为 32*b+b²。

- For this project, read the locally installed `auto-lammps-stage-guide` skill at
  `~/.codex/skills/auto-lammps-stage-guide/SKILL.md`. It routes current norms and
  recorded lessons; it adds no approval gate. Reopen both final-expectation images
  as required below, then resume the current user task instead of expanding setup.
- The product's runtime AI is DeepSeek (provider `deepseek-official`); its role,
  boundaries, API slot and integration requirements are defined in `DEEPSEEK.md` at
  the repository root. Read it before changing any model-facing interface, and finish
  its section-9 developer confirmation (project specs plus both approved images)
  before implementing it.
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
- Record each newly observed execution error in docs/AUDIT.md during the same work
  turn, before retrying: evidence, impact, cause or unverified hypothesis, corrective
  action and validation status. Add reusable lessons to the stage skill; update the
  private checkpoint and retain attempts/costs. Recording a lesson is not a verified fix.
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
- Latest first-week exception: author reference A may continue until successful
  without an attempt cap; retain all failures and costs. B and the final product
  stay at two submissions. Reference A uses 32 cores with no user-imposed
  time limit (respect cluster limits). The former A-derived core-hour ceiling is superseded by the latest user
  removal of resource quotas; retain its historical record, never reinstate it. Record
  policy amendments without changing identities, old charges or submission history.
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

Latest user update (2026-09-27): the reported B strategy is approved; do not ask for the same approval again. Development time and Codex reserve limits are lifted. Retain B's two-submission evaluation rule unless explicitly amended. The new six-page private UI reference set replaces the old global side navigation. Review the roadmap and all six current UI references each work turn; shared components must follow the home page.


Before every new work turn, after restoring compacted context, and before revising the plan, open and visually inspect both user-approved images:
- Technical roadmap: `~/.local/share/auto-lammps-private/requirements/approved-technical-roadmap.png`.
- Product interface authority: `~/.local/share/auto-lammps-private/requirements/approved-ui-home.png`.
- Related pages in the same private directory: `approved-ui-cases.png`, `approved-ui-help.png`, `approved-ui-tasks.png`, `approved-ui-resources.png`, `approved-ui-new-task.png`.
- Previous detail layout is retained as `approved-product-interface-20260923.png`; use its result-content requirements, but the new home governs shared navigation.

Do not rely only on remembered summaries. Both images stay private and must not be committed. If either is unavailable, report that rather than claiming to have reviewed it.

The interface reference defines a light blue/white research workspace: global navigation at the top only; the natural-language request, execution stages and results in the center; task information, submission count, downloads and resources at right. Results expose data, plots, atomic structures, trajectories, reports and logs. Preserve the reproduction register with full titles, DOI links and statuses. All displayed results and completion states require real evidence; illustrated values, resource counts and dates are not run authorizations. Keep the first real P-A-B validation ahead of the full interface redesign.

Then reconcile the roadmap with the latest user corrections, which take precedence:
- The product is natural-language research computing; paper reproduction is its validation method.
- P is the paper result. A directly executes the author's source and original workflow/configuration; do not replace A with independently composed inputs or invented invocation parameters. B independently generates its workflow from permitted inputs, without author solution code or target answers.
- Codex owns week-one P-A-B validation. Independent runtime API integration begins in week two, requested after the first validation succeeds.
- Report the B strategy and wait for user confirmation before starting B.
- Target physics runs only through accounted HPC; retain the approved budget, two-submission evaluation limit and full failure history.
- Latest user instruction: if the B strategy receives no reply within three minutes, work on the approved human-facing interface while waiting. Silence never approves B. Accept the full product flow from a blank page without developer backend assistance; UI checks alone do not establish end-to-end automation.
- Every reproduction paper must show its full published title and verified DOI/link in the web register, reproduction records and relevant progress reports. Distinguish unselected candidates from pending, in-progress and reproduced tasks; do not report a candidate as a completed reproduction.
- Preserve the domain adapter, verified source test library, potential library, progress feedback, analysis/OVITO, paper status list and history requirements. The picture's illustrative numbers and formulas are not frozen scoring criteria.

Use this check to choose the next action; do not repeat the entire roadmap to the user each turn.

Homepage natural-language input also provides a clickable route to the guided new-task page. Model settings require a specific model ID and secure API-key entry. Results offer multiple evidence-backed plots, downloads and a follow-up natural-language analysis window. Runtime AI selects analyses/plots from the research request and real data, not a hardcoded paper figure list. Background job monitoring must survive a closed browser and retain state; do not equate a Codex heartbeat with the final product service.

## Shared-directory collaboration

The user-designated deepseek-harness owns source/potential discovery; follow
`docs/HARNESS_COLLABORATION.md`. Coordinate file ownership before code changes.
Do not switch a shared checkout branch, stage another worker's files or revert
unrelated changes. Use separate worktrees for independent simultaneous code work.
Keep discovered author solutions and targets out of the active B context.

Before starting a new paper's P–A–B preparation, consult the existing resource
catalog and harness handoff. Require a matched LAMMPS workflow and its complete
potential files/parameters first; preserve missing-resource candidates without
starting A. Bundle completeness is not scientific reference validation. Do not
repeat already verified discovery at unchanged versions.
