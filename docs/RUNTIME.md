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
- 可选 `runtime_options` 接受 `library_directories`、`mpi_transport` 和 `mpi_launcher`。库目录最多 8 个、不得重复，每个目录必须有清单中的直接文件；禁止路径穿越、绝对路径及被 work/output/proc/dev/tmp 挂载覆盖的目录。传输仅支持 `none`（默认）或 `intel-shm`。MPI 声明见下文。整个配置受许可的 profile 摘要约束，修改后旧许可失效。
- 控制目录中 `grant.key` 是独立的 32 字节私钥；请求许可包含 payload 与 HMAC-SHA256。payload 绑定 request_id、manifest/profile 摘要、到期时间、资源、输出名、批准/任务/评分/静态检查摘要、已审查提交及 batch_sha256。
- 许可签名端尚未提供。签名仅证明受信控制端签发了这些字段，不自动证明 GitHub 审查、科学条件、评分或静态检查已经完成。不得根据摘要格式正确就自动签发许可。
- 共享库及启动器不是 Agent 上传的文件。管理员应保证部署目录及所有祖先不可被非受信身份替换，并核实软件的依赖和许可。

## 执行范围

启动器只接受 Linux x86-64、单节点。默认仍为单进程；Issue #57 增加显式声明的 Intel Hydra 单节点 MPI，最多 8 个进程，每进程一个线程。必须存在匹配请求、用户、节点、CPU、时限且重启数为零的真实 RUNNING 分配；内核 cpuset、进程 affinity 必须与批准核数完全一致，cgroup 硬内存上限不超过批准资源。只设置 affinity 不能代替内核限制。环境变量本身不构成分配证明。

运行树使用逐文件摘要清单，输入只读；只允许预先声明的少量输出文件可写。Bubblewrap 建立隔离 namespace、断开外部网络、移除 capabilities、创建新会话；不挂载控制目录、宿主 home、凭据或全系统软件目录。子进程不继承控制端环境；沙箱设置固定 PATH、LC_ALL、HOME，并将 OpenMP/MKL 线程数固定为 1。

Issue #31 为已安装引擎增加受控环境声明。例如合成配置
`"runtime_options": {"library_directories": ["lib"], "mpi_transport": "intel-shm"}`
只生成沙箱内 `/lib` 的库搜索路径和 `I_MPI_FABRICS=shm`，不导入模块环境或
`LAMMPS_POTENTIALS`。单独的传输设置只支持 MPI 链接程序的单进程初始化，不启用多进程或
可写临时目录。目录中的全部文件仍需运行树摘要检查；库搜索路径存在不证明
动态加载插件齐全，帮助查询通过也不证明目标计算所需依赖齐全。

### 单节点 MPI 声明

管理员可在 `runtime_options` 中加入以下结构；示例不构成运行批准：

```json
{"mpi_transport":"intel-shm","mpi_launcher":{
  "kind":"intel-hydra-fork","max_ranks":8,
  "launcher_relative":"bin/mpiexec.hydra",
  "pmi_proxy_relative":"bin/hydra_pmi_proxy",
  "bootstrap_proxy_relative":"bin/hydra_bstrap_proxy"
}}
```

三个二进制须在同一目录且全部列入运行树摘要清单，依赖也必须固定。实际核数来自冻结申请，
不能通过模型提供额外命令行。启动参数固定为本机 fork、127.0.0.1 和回环接口；全部进程位于
原有网络 namespace 内，不引入 SSH、其他主机或宿主网络。使用经内核核对的 CPU ID 明确绑定。

MPI 模式另外挂载 `/tmp` 和 `/dev/shm` 两个独立 tmpfs；运行树应预建这两个空目录。
临时数据计入已核验的作业内存 cgroup，与全部 MPI 进程共同受内存上限约束，不绑定宿主临时目录。
它们不是持久结果目录，退出后不回收；必需结果仍写声明输出并受既有文件上限约束。
这没有解决输出平均分配、物理存储配额及所有副本的总成本问题。

CPU 核验读取 v2 的 `cpuset.cpus.effective` 或 v1 的 `cpuset.effective_cpus`；旧 v1 缺少该文件时
交集核对当前与祖先的 `cpuset.cpus`。缺失/异常控制器、核数不符、affinity 不一致均拒绝。
MPI 程序、分配、输入和许可全部检查后，只写一次执行意图，记录 rank 数与 CPU ID；超时及重入规则不变。

已在指定登录端只读查询到 Intel MPI 2021.4 的本地 fork 选项，固定启动器和两个代理的摘要与依赖线索；
没有启动 MPI 任务或 LAMMPS 计算。完整沙箱、多进程通信、临时内存及计算节点分配仍待真实验收。
依据：[Intel Hydra 启动选项](https://www.intel.com/content/www/us/en/docs/mpi-library/developer-reference-linux/2021-9/global-hydra-options.html)、
[Linux cpuset](https://www.kernel.org/doc/Documentation/cgroup-v1/cpusets.txt) 与
[cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)。

Issue #29 增加 cgroup v1 内存控制器支持。统一层级继续读取当前组及祖先的 `memory.max`；
v1 从进程成员关系定位 `memory` 控制器，读取 `memory.limit_in_bytes` 和 `memory.stat` 中的
`hierarchical_memory_limit`，取实际有效的较小上限。依据 [Linux 内核说明](https://www.kernel.org/doc/Documentation/cgroup-v1/memory.txt)，
后者已经考虑层级开关，不能简单把每个祖先的局部限额当成对子组生效。
混合层级优先使用实际承载 memory 控制器的 v1，缺失、重复、异常成员路径和无限上限均拒绝。
只支持统一根目录或根下 `memory` 控制器的宿主布局；其他挂载布局需要单独适配。
不使用 usage、soft limit 或环境变量证明限制，也不写入或调整任何 cgroup 配置。

2026-09-24 的登录端只读核验确认存在旧层级及 Slurm cgroup 约束配置；这不能代替计算节点
真实分配中的内存、CPU、namespace 和存储约束验收。最初四个常用引擎模块不含 SNAP；后续
扩展核验全部 15 个已列模块，13 个帮助查询成功，其中 3 个包含 SNAP。另两个未完成能力
核验，不能标记为“不支持”。已固定一个 2023 年引擎候选及其解析到的依赖摘要；它们合计
约 227 MiB，不包含尚未证明齐全的动态插件，也不代表项目所有副本的存储成本。

该候选在登录端完成一次隔离的 `-help -log none` 查询：仅绑定已核验的引擎/库，根目录只读，
外部网络隔离，使用当前 namespace 过滤器，没有目标输入或可写临时目录。不是生产启动器
端到端验收，也不是计算节点分配或科学计算验证。原始 MPI 初始化失败、一次依赖摘要检查
失败和解释器缺接口失败均保留；依赖摘要检查后续通过，但首次失败没有定位到具体文件。

默认 Python 虽满足版本要求，却未提供 `os.memfd_create`；显式选择的系统 Python 经核验
支持该接口，隔离帮助查询成功。部署必须固定解释器绝对路径并检查实际功能，不能仅检查
版本号或依赖模块 PATH。软件源码版本还需与安装构建来源核对；对应
[2023 年 SNAP 文档](https://github.com/lammps/lammps/blob/ff96eb2e84a5638f8d18afb87f997256894ab34c/doc/src/pair_snap.rst)
支持先前的静态参数转换依据，但不证明安装二进制源码一致或数值等价。

已发现指定入口的 bwrap 帮助不含 `--disable-userns` 与 `--clearenv`。实现使用其已列出的 `--seccomp FD`，通过 libseccomp 编译过滤器，禁止 unshare/setns 与携带 CLONE_NEWUSER 的 clone，并对 clone3 返回 ENOSYS。无需新版两个选项；过滤器不可用则拒绝。其他 ABI 尚不支持。此过滤器专门约束嵌套 namespace，不是所有系统调用的白名单。

Issue #61 增加可选的 [固定容量输出卷](OUTPUT_VOLUME.md)，使声明输出共享容量，保留原有
逐文件可写挂载；回收时只读重开持久镜像。未配置时仍使用下面的平均单文件限制。

独占 execution-intent 防止同请求重启执行。墙钟超时终止进程组；RLIMIT_AS、RLIMIT_FSIZE 和已核实的内存 cgroup 限制资源。输出名有限，单文件大小分摊声明的逻辑存储空间，预留调度流和收据空间。进程退出始终保持 scientific_status=not_evaluated，必须另做输出与科学验收。

Issue #33 将批准输出名保存到执行意图，供只读 `--collect` 入口使用。该入口不调用引擎或
调度器，只回收现有文件；受信控制端必须先取得终态和最终核算。协议、缺失回执和下载副本
预算见 [结果回收](OUTPUTS.md)。旧意图缺少输出清单时拒绝猜测文件范围。

Slurm 在启动脚本前打开输出文件，因此调度流写入已有请求根目录，而不是由启动器稍后建立的 output 目录；采用 append 保留异常重启证据。

## 验证与限制

Issue #57：372 项本地检查中 371 项通过、1 项 Linux 专用检查跳过。新增四进程冻结资源的同一
候选—上传—提交—回收合成链路，覆盖重入、容量声明缺失/篡改、许可、CPU 内核约束与固定启动参数。
实际外部调度器和 MPI 进程均由替身代替；不把这些检查或登录端帮助输出当作真实并行计算成功。

本地合成测试覆盖签名/过期/身份、分配不匹配、内存越限、输入篡改、审计写失败、超时杀进程、重复派发及意图后崩溃。真实 OS 文件大小限制测试只写合成字节；调度捕获测试只调用输出字节的 Python 子进程。没有 LAMMPS、run 0、skiprun 或其他 calculator。

Linux CI 另在一次性子进程内由 libseccomp 编译并加载过滤器，尝试被限制的系统调用，同时验证普通子进程仍能启动。macOS 明确跳过这一项；完整 bwrap、计算节点 cgroup/CPU、集群存储和引擎依赖仍未实测。不能把 CI 内核检查称为 HPC 隔离验收。

仍需真实部署身份隔离、不可篡改配置、物理存储硬配额及所有副本/运行缓存的全局成本核算，科学静态检查与许可签发，输出取回与有效性检查。单文件逻辑上限不等于文件系统物理块配额；运行树本身也不能免计存储。没有完成上述条件、论文任务冻结和授权代码审查前，不开放真实执行。

实现依据：[Bubblewrap 官方选项](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)、[libseccomp 公共 API](https://github.com/seccomp/libseccomp/blob/main/include/seccomp.h.in)、[Slurm sbatch](https://slurm.schedmd.com/sbatch.html)。部署依赖不随项目打包，未声明整个运行环境遵循本项目原创代码许可。
