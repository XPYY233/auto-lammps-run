# 当前状态与恢复入口

更新：2026-09-24。当前为 PR 2 开发基础，不是已完成的科研复现产品。

本轮新增文献清单、不可覆盖的关联历史与提交账本只读视图，并调整为灰白科研工作台界面。本地 149 项检查中 148 项通过、1 项 Linux 专用检查跳过；浏览器已核验真实候选清单、分类及历史展开。关联 Issue #13；具体边界见 [文献历史](PAPER_HISTORY.md)，逐项第一周缺口见 [验收核对](WEEK1_AUDIT.md)。仍没有真实目标计算或科学复现结果。

## 当前交付

- [PR 0 / #2](https://github.com/XPYY233/auto-lammps-run/pull/2)：现状审计、三周规范、候选与只读 Zotero 发现。CI 已通过，等待授权维护者审查。
- [PR 1a / #4](https://github.com/XPYY233/auto-lammps-run/pull/4)：受信提交账本、防重复派发、并发核时/存储/次数控制、未知状态、取消及恢复。基于 PR 0 分支；父 PR 合并后应将 base 转向 main。关联 [Issue #3](https://github.com/XPYY233/auto-lammps-run/issues/3)，尚未完成其中的真实 HPC 验证。
- [PR 1b / #5](https://github.com/XPYY233/auto-lammps-run/pull/5)：输入冻结与受控 Slurm 只读查询，基于 PR 1a 分支，继续关联 Issue #3；59 项本地检查通过，远端 CI 以 PR 当前提交检查为准，等待授权维护者审查。
- [PR 1c / #6](https://github.com/XPYY233/auto-lammps-run/pull/6)：分支 feat/pr1c-ledger-reconciliation，基于 PR 1b，继续关联 Issue #3；补齐终态未核算恢复、查询顺序、原子回写和冲突批次阻断。尚无真实计算作业恢复验收。
- [PR 1d / #7](https://github.com/XPYY233/auto-lammps-run/pull/7)：分支 feat/pr1d-restricted-staging，基于 PR 1c，继续关联 Issue #3；受限流式上传、私人接收程序与资源脚本已完成离线验证，未部署真实上传或提交。
- [PR 1e / #8](https://github.com/XPYY233/auto-lammps-run/pull/8)：分支 feat/pr1e-runtime-guard，基于 PR 1d；增加受信提交适配器、签名许可校验、一次性远端派发与计算节点隔离启动器。仅合成验证，尚未部署，见 [RUNTIME.md](RUNTIME.md)。
- [PR 2 / #10](https://github.com/XPYY233/auto-lammps-run/pull/10)：分支 feat/pr2-task-confirmation，基于 PR 1e；持久条件草稿、来源/冲突核对、逐项确认、不可覆盖冻结及本机 FastAPI 网页，关联 [Issue #9](https://github.com/XPYY233/auto-lammps-run/issues/9)。浏览器已实测保存、冲突、刷新、服务重启、冻结和导出；不执行目标计算，见 [TASKS.md](TASKS.md)。
- [PR 2b / #12](https://github.com/XPYY233/auto-lammps-run/pull/12)：分支 feat/pr2b-literature-import，基于 PR 2；来源可定位的文献 CSV 导入与条件冲突保存，关联 [Issue #11](https://github.com/XPYY233/auto-lammps-run/issues/11)。实际边界见 [LITERATURE_IMPORT.md](LITERATURE_IMPORT.md)。
- 主分支保护已读回核实：offline-checks、一次审查、管理员同样受约束；没有自称独立审查或绕过保护。当前 PR 由同一账号发起，GitHub 不允许该账号自行批准；已请求用户提供有写权限的审阅者，尚未修改保护。

## 实际验证

PR 2b 文献条件导入已完成：独立实现旧工作台单条 CSV 格式适配、来源预览、明确输入映射与条目方法分类、事务导入和冻结来源摘要。145 项本地检查中 144 项通过、1 项 Linux 专用检查跳过；浏览器已用单独合成数据库实测预览、映射、冲突撤销、刷新、来源展开、格式错误及重复导入拒绝。没有真实文章导出端到端验收或科学结果，见 [LITERATURE_IMPORT.md](LITERATURE_IMPORT.md)。

PR 2 本机独立环境 135 项检查中 134 项通过、1 项 Linux 专用过滤器测试明确跳过；功能提交 ea20183 的 Linux CI 全部 135 项通过（[记录](https://github.com/XPYY233/auto-lammps-run/actions/runs/35948870109)）。此前 Linux CI 在功能提交 385a9a8 上全部 121 项通过，含真实内核过滤器检查（[运行记录](https://github.com/XPYY233/auto-lammps-run/actions/runs/35946789359)）。检查包括多进程争抢最后一次额度/核时/并发、同请求只派发一次、审计失败阻止派发、真正退出 worker 后不重提、接受回执对账、取消、超支及终态矛盾。提交调度器为合成替身，没有目标模拟。新增输入快照、只读查询解析及真实子进程边界测试。
功能提交 fed8f6a 的 [CI](https://github.com/XPYY233/auto-lammps-run/actions/runs/35886109041) 通过；后续进程退出测试及状态记录由独立提交保存，最终检查以 PR 当前版本为准。

真实 Zotero schema v2 导出通过。一个论文可有多个库条目，因此分别报告条目和 DOI 字符串数并保留重复组，不再使用 unique_papers。数据、条目身份及私人计数只保存在仓库之外，旧导出没有覆盖。

用户指定的登录入口及既有分区只读探测通过。PR 1b 对随机无作业标识完成真实 squeue/sacct 查询，均成功返回空记录，正确保留 unknown/not_visible；未查询其他科研作业详情。该检查仅证明连接/分区查询可用，不证明计算节点依赖或 LAMMPS 执行。没有 SSH 提交、付费 API 或 P-A-B 结果。

## 已批准与未完成

首轮总计 12 核时；单作业最多 8 核/30 分钟/8 GiB；并发 1；存储 1 GiB。模型调用额度为 0。原创代码 Apache-2.0 已批准。具体登录别名和配置只存私有部署记录，不再重复询问这些授权。

输入 manifest、只读查询私人回执、事务化恢复、受限上传及受信提交/启动器代码已实现；真实上传和执行部署、科学静态检查、系统权限隔离验收、模型、网页计算流程和科学评分仍未完成。首篇作者代码与论文条件存在冲突，任务范围及容差尚未冻结。[账本说明](LEDGER.md) 列明当前边界，不能将离线通过称为整个 PR 1 完成。

## 下一入口

下一步完成首篇作者工作流核验、真实文献条件适配验收与类型化科学条件检查；执行端仍需核实计算节点权限、物理存储强制限制、依赖和隔离验收。[受限上传说明](STAGING.md)列明当前已验证范围与不足。事务恢复与一次性管理员入口见 [RECOVERY.md](RECOVERY.md)。快照和只读查询的边界及来源见 [FROZEN_INPUTS.md](FROZEN_INPUTS.md)。继续核实首篇完整作者流程；满足条件和审查后才在批准预算内计算。

阶段汇报五项：已完成；实际验证；阻塞；需要确认；下一模块。较早审计见 AUDIT.md，第一周范围与候选见 CANDIDATES.md。
