# 受信提交与计算节点启动器

PR 1e 是可审查的实现，尚未部署或完成真实计算验收。没有签发真实执行许可，没有运行目标引擎，也没有 P-A-B 结果。与 [Issue #3](https://github.com/XPYY233/auto-lammps-run/issues/3) 共用验收目标。

## 调用与证据

受信控制端将 `SlurmSubmitter(ledger, endpoint, audit_directory)` 注入已有 `SubmissionService`。endpoint 复用受限上传的配置结构，但必须指向管理员安装的 `remote_submit.py` 及其固定摘要。Agent 不能提供 endpoint、命令、许可或账本身份。当前没有开放这项操作的网页或 MCP 接口。

本地预检要求资源与 manifest 匹配且 inputs_staged 已持久保存，再由账本事务抢占派发权。SSH 采用固定参数、严格主机校验、关闭转发与本机 hook，只调用摘要固定的辅助程序。网络操作前保存私人意图，响应原文先落盘再解析，证据摘要关联到账本。

远端辅助程序检查私有部署配置、启动器摘要、签名许可、上传回执、全部输入和批准的资源脚本。资源脚本来自私有控制目录，不能由上传文件替代。独占 scheduler-intent 写入并同步后才调用一次 sbatch。超时、损坏响应、非零退出及中断均保留 unknown；重复请求只读取已存结果，意图后崩溃也不会重新提交。恢复沿用按请求身份的只读 Slurm 对账。未知响应不被误记为明确拒绝。

## 私有部署契约

所有配置、密钥、许可、脚本与真实回执都在 Git 外，实际路径不出现在公开文档。部署尚未完成；以下字段说明不构成执行批准。

- `submission.json` 固定 `runtime_path`、`runtime_sha256`、`sbatch_path`、`sbatch_sha256`。
- 启动器相邻的 `runtime.json` 固定互不包含的 `requests_root`、`control_root`、`runtime_tree`；固定 `runtime_files` 清单、`engine_relative`、bwrap/scontrol 的路径及摘要、libseccomp 共享库的真实路径及摘要。
- 控制目录中 `grant.key` 是独立的 32 字节私钥；请求许可包含 payload 与 HMAC-SHA256。payload 绑定 request_id、manifest/profile 摘要、到期时间、资源、输出名、批准/任务/评分/静态检查摘要、已审查提交及 batch_sha256。
- 许可签名端尚未提供。签名仅证明受信控制端签发了这些字段，不自动证明 GitHub 审查、科学条件、评分或静态检查已经完成。不得根据摘要格式正确就自动签发许可。
- 共享库及启动器不是 Agent 上传的文件。管理员应保证部署目录及所有祖先不可被非受信身份替换，并核实软件的依赖和许可。

## 执行范围

启动器只接受 Linux x86-64、单节点单核、单进程入口。批准的最多 8 核并不意味着此版本已实现 MPI；请求多核会拒绝，不自动改科学任务。必须存在匹配请求、用户、节点、CPU、时限且重启数为零的真实 RUNNING 分配；CPU affinity 和 cgroup 硬内存上限必须不超过批准资源。环境变量本身不构成分配证明。

运行树使用逐文件摘要清单，输入只读；只允许预先声明的少量输出文件可写。Bubblewrap 建立隔离 namespace、断开外部网络、移除 capabilities、创建新会话；不挂载控制目录、宿主 home、凭据或全系统软件目录。子进程环境只有固定 PATH/LC_ALL，随后设置固定 HOME；不继承控制端环境。

Issue #29 增加 cgroup v1 内存控制器支持。统一层级继续读取当前组及祖先的 `memory.max`；
v1 从进程成员关系定位 `memory` 控制器，读取 `memory.limit_in_bytes` 和 `memory.stat` 中的
`hierarchical_memory_limit`，取实际有效的较小上限。依据 [Linux 内核说明](https://www.kernel.org/doc/Documentation/cgroup-v1/memory.txt)，
后者已经考虑层级开关，不能简单把每个祖先的局部限额当成对子组生效。
混合层级优先使用实际承载 memory 控制器的 v1，缺失、重复、异常成员路径和无限上限均拒绝。
只支持统一根目录或根下 `memory` 控制器的宿主布局；其他挂载布局需要单独适配。
不使用 usage、soft limit 或环境变量证明限制，也不写入或调整任何 cgroup 配置。

2026-09-24 的登录端只读核验确认存在旧层级及 Slurm cgroup 约束配置；这不能代替计算节点
真实分配中的内存、CPU、namespace 和存储约束验收。已检查的四个常用引擎模块均不含 SNAP，
其中一个 MPI 版本的帮助查询先因通信初始化失败；仅对帮助查询使用文档允许的共享内存通信后
成功，原始失败保留。未把该环境选项加入生产启动器或声称完整运行树可用。

已发现指定入口的 bwrap 帮助不含 `--disable-userns` 与 `--clearenv`。实现使用其已列出的 `--seccomp FD`，通过 libseccomp 编译过滤器，禁止 unshare/setns 与携带 CLONE_NEWUSER 的 clone，并对 clone3 返回 ENOSYS。无需新版两个选项；过滤器不可用则拒绝。其他 ABI 尚不支持。此过滤器专门约束嵌套 namespace，不是所有系统调用的白名单。

独占 execution-intent 防止同请求重启执行。墙钟超时终止进程组；RLIMIT_AS、RLIMIT_FSIZE 和已核实的内存 cgroup 限制资源。输出名有限，单文件大小分摊声明的逻辑存储空间，预留调度流和收据空间。进程退出始终保持 scientific_status=not_evaluated，必须另做输出与科学验收。

Slurm 在启动脚本前打开输出文件，因此调度流写入已有请求根目录，而不是由启动器稍后建立的 output 目录；采用 append 保留异常重启证据。

## 验证与限制

本地合成测试覆盖签名/过期/身份、分配不匹配、内存越限、输入篡改、审计写失败、超时杀进程、重复派发及意图后崩溃。真实 OS 文件大小限制测试只写合成字节；调度捕获测试只调用输出字节的 Python 子进程。没有 LAMMPS、run 0、skiprun 或其他 calculator。

Linux CI 另在一次性子进程内由 libseccomp 编译并加载过滤器，尝试被限制的系统调用，同时验证普通子进程仍能启动。macOS 明确跳过这一项；完整 bwrap、计算节点 cgroup/CPU、集群存储和引擎依赖仍未实测。不能把 CI 内核检查称为 HPC 隔离验收。

仍需真实部署身份隔离、不可篡改配置、物理存储硬配额及所有副本/运行缓存的全局成本核算，科学静态检查与许可签发，输出取回与有效性检查。单文件逻辑上限不等于文件系统物理块配额；运行树本身也不能免计存储。没有完成上述条件、论文任务冻结和授权代码审查前，不开放真实执行。

实现依据：[Bubblewrap 官方选项](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)、[libseccomp 公共 API](https://github.com/seccomp/libseccomp/blob/main/include/seccomp.h.in)、[Slurm sbatch](https://slurm.schedmd.com/sbatch.html)。部署依赖不随项目打包，未声明整个运行环境遵循本项目原创代码许可。
