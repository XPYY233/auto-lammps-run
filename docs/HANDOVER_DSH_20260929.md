# 交班文档：deepseek-harness → Codex（2026-09-29）

本文件写给 Codex（GPT）审计用。范围：第二周"应用端自然语言 → 完整流程"这一条主线，
以及本轮所有改动、证据位置、需要裁定的事项。**不重复**已读到的审计内容
（`docs/AUDIT.md` 的 #120 / #131 / 任务管理三条、`docs/ISOLATION_ACCEPTANCE.md`、
`docs/HARNESS_COLLABORATION.md` 的第二周职责调整、`docs/PROGRESS.md`、`docs/TARGET_PLANNING.md`）。

## 0. 最要紧的一条：合并前必须 rebase（否则会回退你的工作）

| 项 | 值 |
|---|---|
| 我的分支 | `harness/integration-candidate`（本地 `local/integration-2`），HEAD `4a5b433` |
| 分支基点 | `9bf7984`（**早于**你合并的 #136、#148） |
| 直接合并的后果 | **删除** `docs/ISOLATION_ACCEPTANCE.md`；并在 `auto_lammps/tasks.py`、`web.py`、`web_assets/app.js`、`app.css` 上用我的旧版本**覆盖你最近的任务生命周期/目标预览/渲染改动** |

建议流程（等你确认，我不自行操作）：

1. 我把 `origin/main` 合并进我的分支，**冲突一律保留你的版本**，再把我的改动重新应用到其上；
2. 逐项回归：`python3 -m unittest discover -s tests`、`node --test tests/js/*.cjs`；
3. 重新生成并核对 `docs/ISOLATION_ACCEPTANCE.md` 等你的文件**不被删除**；
4. 之后你再审 PR。

## 1. 本轮我做了什么（按主题，均已推送）

| 主题 | 提交 | 一句话 |
|---|---|---|
| 资源库/条件 | `178155b` | 补全条件时把**实际已装势函数**喂给模型，并拒绝它提出未提供的格式（例如只有 MEAM 时不得写 EAM） |
| 条件/候选报错 | `cb6e566`、`f76c7a0` | 报错具体化；对契约违反给一次有界修复；非法 JSON 容错解析 |
| 澄清闭环 | `6d9f983` | 用户答复作为**追加事件**形成新一轮可记账准备，冻结条件与旧记录不改写 |
| 用户可见性 | `e86dfc6` | 任务页常显用户原始提示词；具体失败原因与待处理问题上屏 |
| 任务状态 | `40e1524` | 列表状态纳入"方案准备"进展（此前准备失败也显示"待准备"） |
| 契约漂移审计 | `412a0ce` | **审计**：改前提示词漏掉 7/12 条校验规则（`git log -S "two to sixteen"` 为空为证） |
| 真实运行缺陷 | `1f6a6ac` | 真实跑一遍暴露并修掉 12 个缺陷（见 AUDIT.md） |
| 批准关卡 | `99b3f75` | `GET /plan`、`POST /plan/approve`（**绑定方案摘要**）、`POST /plan/revise`；未批准拒绝派发 |
| 提交链路 | `f795cc0`、`a8e3932`、`4db9c29` | 工作线程不再被单任务异常杀死；作废请求结算存储；清理"从未派发"残留预留；候选软件摘要与部署批准对齐；批准按钮永远可执行 |
| 活动流 | `3f10a2b`、`3addf4b`、`4a5b433` | AI 活动改为**人话进度播报**（时间 + 一句中文 + 补充说明，最新标"现在"），默认折叠、展开即加载、每 6 秒刷新；部署后页面**自动刷新** |
| 桌面入口 | `e20d0b9` | 双击打不开的**真因**是架构：解释器通用二进制但 `pydantic_core` 只有 arm64，入口在 Rosetta 下启动即 `dlopen` 失败；入口与生成器改为 `arch -arm64` 强制原生 |

审计与证据：`docs/AUDIT.md`（我的条目）、`docs/CANDIDATE_CONTRACT_AUDIT.md`、Issue **#153**。

## 2. 当前真实运行状态（可复现）

- 运行时：`~/.local/share/auto-lammps-private/portable-runtime-113`；端口 **8787**；
- 私有配置：`~/Auto-LAMMPS-研究资料/产品部署/本地启动器/current.json`；
- 桌面入口：`~/Desktop/Auto-LAMMPS.app`（由 `scripts/build_desktop_app.sh` 生成，现强制原生 arm64）；
- 候选配置：`…/candidate-config-draft-xai-mpea.json`（`output_layout: working_directory`；
  `software_sha256` 已对齐部署批准值 `1d8ab3d7…`）；
- 私有势函数库 pin：W（Lee–Bakes 2001 MEAM）`2c00ee39…`、XAI MPEA `60b65c8c…`；
- **活动政策修订（需你审计）**：`week2-endtoend-20260928` 的
  `total_core_seconds` 460800 → 38707200、`total_storage_bytes` 4 GiB → 16 GiB，
  各自附**私有审批记录**（`产品部署/政策修订-额度解除-20260928.md`、`政策修订-存储额度-20260928.md`），
  依据是你我共同的记录：用户 2026-09-27 明确"资源额度限制取消"，保留 7 天/32 核/8 GiB/并发 1。

## 3. 端到端到底走到哪（诚实口径）

**已打通**：自然语言 → 应用自身整理条件 → 补全 → 用户确认 → 冻结 → 方案准备 → **方案审核（看脚本/结构/分析）→ 用户批准**。

**已清掉的提交门槛**（每条都是真实缺陷，证据在 AUDIT.md）：模型调用无响应卡死、配置变化卡死、
存储预留泄漏、同一评估单活动请求、候选软件摘要不一致、执行工作线程被杀。

**剩余唯一门槛**：`authorization.ensure` 要求候选必须携带**完整的数值分析计划**
（`plan_adapter(proposal['analysis']['plan'])`）。我此前为绕开契约曾让模型省略 `analysis.plan`，
现在必须让它带上；而模型在"写路径 / analysis.files 一致性"上仍会间歇性失败（有 3 轮自动修复）。

**因此：尚未产生 HPC 作业号。** 不把准备完成或界面演示当作端到端完成。

## 4. 需要你审计或裁定的六件事

1. **批准关卡的接口与语义**：`GET /plan` 暴露脚本/结构/分析给用户审阅是否越界？
   批准"绑定方案摘要（`plan:<snapshot_sha256>`）"、方案一变即失效，是否符合产品规范？
2. **账本改动**：新增 `cancel_undispatched(evaluation)`；`cancel_intent` 保持只清核时、
   存储改用既有 `settle_cancelled_preparation_storage`；幂等键加入方案摘要（内容寻址）；
   "同一评估只允许一条活动请求"下由应用主动清理残留预留——这套语义是否可接受？
3. **政策修订**：两份审批记录的措辞与额度是否恰当；是否需要在 `docs/` 留公开摘要（不含私密路径）。
4. **桌面入口强制 arm64**：是否接受在生成器里写死 `arch -arm64`（Intel 机器自动退回）。
5. **活动流措辞**：现在的"人话播报"是否符合界面规范（信息量/术语/是否暴露内部字段）。
6. **备选路径**：是否允许评估"模型直接写 LAMMPS 脚本 + 既有 `bounded_command_and_output_screen`"
   作为与第一周 B 对齐的路径，以降低结构化契约的摩擦（代价：失去类型化分析与图表基础）。

## 5. 文件归属与我不做的事

- 我这轮改动的文件：`auto_lammps/{web,tasks,ledger,execution,execution_jobs,candidate_jobs,research_workflow,agent_candidates,analysis,analysis_v2,condition_generation,deepseek,hpc_connections,model_connections,papers}.py`、
  `auto_lammps/web_assets/*`、`scripts/{build_desktop_app.sh,desktop_supervisor.py,launch_local.py,stop_local.py}`、
  `tests/*`、`docs/{AUDIT.md,CANDIDATE_CONTRACT_AUDIT.md,本文件}`。
  若要改这些文件，请先在 Issue 说明，避免互相覆盖。
- 我**不做**：自行合并 PR、切换共享检出（`/Users/fanjunran/Auto-LAMMPS`）的分支、
  改动运行中的 HPC 作业、把私密证据或凭据写入公共树。

## 6. 我打算做的下一步（等你点头）

1. 把 `origin/main` 合并进我的分支并按第 0 节处理冲突，恢复被误删的你的文档；
2. 让候选可靠地带**合法 `analysis.plan`**（生成即校验 + 具体错误回喂 + 最小模板校验）；
3. 拿到作业号后：自动跟进状态 → **计算完成提醒** → 第二道关卡（AI 识别可可视化数据并可视化）。
