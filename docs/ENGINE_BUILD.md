# 记账的引擎编译

源码准备之后，`EngineBuildExecution` 把引擎编译作为同一预算批次的 `development`
任务，复用 `Ledger`、`StagingService`、`SubmissionService`、`SlurmSubmitter` 和
`ReconciliationService`。编译占用核时、并发和存储，不进入产品 Agent 的模拟提交次数。
不新建另一份预算来绕开现有批次限制。

`build.json` 只含固定官方源码准备目录、来源/清单摘要、源码 commit、MEAM/MPI 要求
以及 CMake、C++、MPI 包装器和 Make 的路径/摘要。快照类型是 `engine_build`，没有
LAMMPS 输入或虚构的科学分析配置。系统编译器可以使用发行版的普通硬链接别名；
仍核对只读权限、内容摘要并保留驱动器原路径，避免复制 GCC 后破坏其工具定位。

受信控制端按以下步骤调用：

1. 从批准的同一 campaign 登记 development 任务，任务身份为 build.json 的摘要。
2. `prepare` 冻结元数据、预留资源并产生可审查的 Slurm 脚本，不传输或提交。
3. 已有授权文件绑定明确的已审查代码提交、输入/运行配置/脚本摘要、资源、期限及
   `purpose=engine_build` 后，`advance` 才会传输元数据并请求一次调度提交。
4. 连接不确定或重复调用返回已记账状态；既有恢复服务按请求身份查询调度和最终核算，
   不以连接超时为理由重提。编译输出保留在超算，失败后不自动清除或重新编译。

这里复用的授权格式中，`scoring_sha256` 绑定引擎功能要求，`static_check_sha256` 绑定
源码清单；两者均不是论文分数。模块不签发授权、不完成代码审查，也不是 Agent 可用的
任意命令接口。部署身份及控制目录必须由管理员维护。

## 超算部署

安装独立的 `engine_build_worker.py`、`runtime_launcher.py` 和 `engine_source_worker.py`，
在其目录配置私有 `runtime.json`。包含相互独立的 `requests_root`、`control_root`、
`runtime_tree`，以及 `runtime_path/runtime_sha256`、
`source_helper_path/source_helper_sha256`、`scontrol_path/scontrol_sha256` 和已验证的
`output_volume` 配置。原有 `remote_submit.py` 使用同一配置与请求根目录；提交端点和
编译 worker 端点分别固定文件摘要。

同一个编译 worker：携带 `--root` 时只接收固定元数据协议；由已审查批处理脚本在
计算节点调用时才进入执行。入口核对授权、调度记录、CPU cgroup/亲和性和内存硬限制，
逐文件重验源码及工具，然后在容量固定的 ext2/FUSE 输出卷里配置与编译。
固定使用 Unix Makefiles，启用 MEAM/MPI、禁用 OpenMP 和 BUILD_TESTING；临时文件、
用户目录和编译日志都放在卷中。不会执行新生成的 LAMMPS 程序或任何目标输入。

CMake 与 Make 成功且产生 ELF 文件后，结果记录 `built=true` 和文件摘要；
`environment_verified`、`scientific_validation` 仍为 false。编译失败保留日志与状态。
后续还需只读回收、安装该二进制、核对包/版本/MPI 和运行环境；不能直接用于正式模拟。

该 worker 的主体是受信管理工具，不宣称具备主 Agent 的完整文件/网络隔离。
输出卷不等同于全项目物理配额。正式部署仍需把已有源码、工具、所有副本、卷外
回执与后续输出计入同一存储上限，核对工具在计算节点的实际可用性。

## 验证边界

合成测试覆盖共享预算、禁止使用 agent 身份编译、授权目的/摘要、真正的元数据
传输、防重复调度、未知状态保留、源码/工具变动，以及受控编译结果状态与二次执行
拒绝。测试不调用真实编译器、LAMMPS 或 HPC。

真实超算仅完成新版源码/工具校验代码的只读运行；没有调度提交、编译、环境验收
或科学结果。首次真实编译仍须针对已审查的明确提交，在批准资源内执行。
