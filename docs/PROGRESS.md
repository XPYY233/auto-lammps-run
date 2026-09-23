# 当前状态与恢复入口

更新：2026-09-24。当前为 PR 1d 开发基础，不是已完成的科研复现产品。

## 当前交付

- [PR 0 / #2](https://github.com/XPYY233/auto-lammps-run/pull/2)：现状审计、三周规范、候选与只读 Zotero 发现。CI 已通过，等待授权维护者审查。
- [PR 1a / #4](https://github.com/XPYY233/auto-lammps-run/pull/4)：受信提交账本、防重复派发、并发核时/存储/次数控制、未知状态、取消及恢复。基于 PR 0 分支；父 PR 合并后应将 base 转向 main。关联 [Issue #3](https://github.com/XPYY233/auto-lammps-run/issues/3)，尚未完成其中的真实 HPC 验证。
- [PR 1b / #5](https://github.com/XPYY233/auto-lammps-run/pull/5)：输入冻结与受控 Slurm 只读查询，基于 PR 1a 分支，继续关联 Issue #3；59 项本地检查通过，远端 CI 以 PR 当前提交检查为准，等待授权维护者审查。
- [PR 1c / #6](https://github.com/XPYY233/auto-lammps-run/pull/6)：分支 feat/pr1c-ledger-reconciliation，基于 PR 1b，继续关联 Issue #3；补齐终态未核算恢复、查询顺序、原子回写和冲突批次阻断。尚无真实计算作业恢复验收。
- [PR 1d / #7](https://github.com/XPYY233/auto-lammps-run/pull/7)：分支 feat/pr1d-restricted-staging，基于 PR 1c，继续关联 Issue #3；受限流式上传、私人接收程序与资源脚本已完成离线验证，未部署真实上传或提交。
- 主分支保护已读回核实：offline-checks、一次审查、管理员同样受约束；没有自称独立审查或绕过保护。当前 PR 由同一账号发起，GitHub 不允许该账号自行批准；已请求用户提供有写权限的审阅者，尚未修改保护。

## 实际验证

95 项离线测试通过，包括多进程争抢最后一次额度/核时/并发、同请求只派发一次、审计失败阻止派发、真正退出 worker 后不重提、接受回执对账、取消、超支及终态矛盾。提交调度器为合成替身，没有目标模拟。新增输入快照、只读查询解析及真实子进程边界测试。
功能提交 fed8f6a 的 [CI](https://github.com/XPYY233/auto-lammps-run/actions/runs/35886109041) 通过；后续进程退出测试及状态记录由独立提交保存，最终检查以 PR 当前版本为准。

真实 Zotero schema v2 导出通过。一个论文可有多个库条目，因此分别报告条目和 DOI 字符串数并保留重复组，不再使用 unique_papers。数据、条目身份及私人计数只保存在仓库之外，旧导出没有覆盖。

用户指定的登录入口及既有分区只读探测通过。PR 1b 对随机无作业标识完成真实 squeue/sacct 查询，均成功返回空记录，正确保留 unknown/not_visible；未查询其他科研作业详情。该检查仅证明连接/分区查询可用，不证明计算节点依赖或 LAMMPS 执行。没有 SSH 提交、付费 API 或 P-A-B 结果。

## 已批准与未完成

首轮总计 12 核时；单作业最多 8 核/30 分钟/8 GiB；并发 1；存储 1 GiB。模型调用额度为 0。原创代码 Apache-2.0 已批准。具体登录别名和配置只存私有部署记录，不再重复询问这些授权。

输入 manifest、只读查询私人回执、事务化恢复和受限上传代码已实现；真实上传部署、生产提交、科学静态检查、系统权限隔离、模型、网页和科学评分仍未完成。首篇作者代码与论文条件存在冲突，任务范围及容差尚未冻结。[账本说明](LEDGER.md) 列明当前边界，不能将离线通过称为整个 PR 1 完成。

## 下一入口

下一步实现隔离启动器与实际提交处理程序，核实计算节点权限及存储强制限制。[受限上传说明](STAGING.md)列明当前已验证范围与不足。事务恢复与一次性管理员入口见 [RECOVERY.md](RECOVERY.md)。快照和只读查询的边界及来源见 [FROZEN_INPUTS.md](FROZEN_INPUTS.md)。继续核实首篇完整作者流程；满足条件和审查后才在批准预算内计算。

阶段汇报五项：已完成；实际验证；阻塞；需要确认；下一模块。较早审计见 AUDIT.md，第一周范围与候选见 CANDIDATES.md。
