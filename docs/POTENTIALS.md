# 势函数资源与适配接口

## 2026-10-08 现有资源优先与HPC原件

普通AI链优先读取已登记势函数的元素、格式、单位、适用范围和固定标识；只有确实缺失
才进入既有检索发现流程。目录读取失败、无可用资源或缺部署依赖时，在付费条件补全前
显示具体缺项，不让模型猜测兼容性。资源发现、静态绑定、环境可用与科学验证分别记载。

新 `RemotePotentialCatalog` 仅在控制端缓存受信目录元数据，完整文件留在用户配置HPC。
目录标识与条目标识分别固定内容；兼容已有本地登记目录，不迁移或改写旧冻结记录。
远端暂存从固定只读目录取得准确版本，核对全部格式角色、许可证据、字节数、摘要和
元素映射；不接受模型指定远端路径、遗漏文件或另一版本替换。提交前再次重构固定调用
语句并核验manifest绑定。合成检查覆盖MEAM、EAM/alloy和SNAP，不运行引擎。

当前新增远端接口通过离线检查；尚未在真实HPC注册新目录、激活新helper或验收新B。
原记录的许可与来源不能由此推定，新资源仍需实际证据。

Issue #21 为产品执行链路增加私人势函数目录和 `resolve_potential` 领域操作。
当前支持独立 SNAP / qSNAP、常规 MEAM 与 EAM/alloy setfl 文件的静态资源绑定，接入同一候选生成、资源筛选和准备历史。
真实自动选择能力、网页资源浏览及计算尚未验收。源码测试库与运行资源目录分别维护；每个资源只接受对应格式明确的文件角色。

## 固定资源

管理员将已审查资源导入仓库之外的私人目录。记录名称、元素顺序、格式、单位、来源 URL、
固定 Git 提交或源归档 SHA-256、原始路径、许可、适用范围和调用依据。
文件逐一记录 SHA-256，整体内容地址固定元数据和文件；重复导入返回同一标识，修改产生新记录。
每次读取重新检查文件与记录摘要、符号链接、硬链接及额外文件，保留原始模型字节及许可证。
目录自身不下载模型、不运行模拟、不自动修订旧格式。
参考端现有自动下载与文件核验入口，见 [自动准备 MEAM 资源](POTENTIAL_ACQUISITION.md)。管理员可明确批准下述窄范围绑定转换；目录原件不变。

管理员入口（不提供给模型）：

```sh
python3 -m auto_lammps.potentials --store "$HOME/.local/share/auto-lammps-private/potential-catalog" import --source MODEL_DIRECTORY --metadata IMPORT_JSON
python3 -m auto_lammps.potentials --store "$HOME/.local/share/auto-lammps-private/potential-catalog" list
```

导入 JSON 的 `metadata` 包含 `name, format, elements, units, source, license, applicability,
usage_evidence, interaction`。`source` 包含 `url, revision, locator`。
`files` 将 `coefficients, parameters, license` 分别映射到源目录内的安全文件名。
`snap` 接收 `metal/real` 单位声明；`meam` 使用 `library, parameters, license` 三个角色，目前只接受 `metal`。
`eam/alloy` 使用 `model, license` 两个角色，只接受已核对来源的 `metal` 单位声明。
单位必须通过模型来源核对，文件本身不证明其单位。
`interaction` 为 `standalone/hybrid/unresolved`，不允许静默遗漏额外相互作用。

## 产品侧调用

服务创建 `PotentialAdapter`，配置该任务允许使用的资源摘要、固定软件环境摘要和依赖包声明。
Agent 提交资源摘要、按原子类型顺序排列的元素与任务单位。接口检查允许列表、文件完整性、
元素映射、单位、相应包声明（ML-SNAP、MEAM 或 MANYBODY）、参数格式以及常规 SNAP / qSNAP 的系数维度。
返回固定文件名下的模型、许可证、两条势函数设置语句和可追溯绑定记录。
重复元素映射保留原子类型顺序，不排序、不猜测，不接受 NULL 混合映射。

现有 `manifest.freeze` 可将这些字节、绑定记录与后续 Agent 生成的输入一并冻结；集成测试已覆盖。
输出没有完整模拟流程，也没有作者目标脚本。此处不提交作业，未消耗计算提交次数。
正式提交仍须经过任务确认、隔离部署、预算与提交账本。

静态实现依据 [LAMMPS SNAP 文档](https://docs.lammps.org/pair_snap.html) 与
[描述符维度定义](https://docs.lammps.org/compute_sna_atom.html)，核对日期 2026-09-24。
化学分辨 SNAP、内切换、多势叠加和未知参数暂时阻断；旧 `diagonalstyle` 原样保存并默认要求版本审查。
不能把此限制解释成原论文或模型无效。

## MEAM 元素顺序与原件

Issue #49 支持 C++ `pair_style meam`，依据 [LAMMPS MEAM 文档](https://docs.lammps.org/pair_meam.html)
（核对日期 2026-09-24）。元数据 `elements` 明确固定参数索引顺序，与原子类型映射及库文件行顺序分开。
例如库索引为 `Cu, Ni`、原子类型为 `Ni, Cu, Ni` 时，生成：

```text
pair_style meam
pair_coeff * * potentials/<pin>/library.meam Cu Ni potentials/<pin>/model.meam Ni Cu Ni
```

不根据原子类型重新排序库索引，也不删除暂时未映射到原子类型的已声明元素。绑定记录同时保存两种顺序。
文件改为受控路径，库文件、参数和许可内容逐字节不变，不补写默认值。

静态检查覆盖每项 19 字段库记录、有限数值、所选元素是否存在、t0/ibar 限制、参数索引范围和维数、
重复赋值记录及基本标志值。库中重复的所选元素保留原件但阻止绑定，避免默认选取一项掩盖来源歧义。
未知参数可保留为有阻断项的资源，不提供给候选生成。最多选择 8 个元素；只接受有明确非空参数文件的
独立 MEAM，暂不支持 NULL、混合势、MS-MEAM、加速变体、Fortran D 指数及新参考晶格 `dia3`。
源文件可包含额外库元素，但当前仅支持 ASCII、常规数字和字符串语法。

v2 检查保留重复赋值的顺序与行号，零原子序号作为提示保留；原 v1 目录按旧规则核验。
包清单仍是部署声明，静态检查不证明特定引擎版本兼容、势函数适用或物理结果正确。这个适配层不改写
作者计算工作流，不将候选生成的输入用于 A；A 仍直接运行经核对的作者入口与原配置。
合成测试覆盖索引/映射差异、文件破坏、缺包拒绝、旧 SNAP 摘要稳定，以及同一候选服务的历史和重启复用。

## EAM/alloy 文件与映射

按 [LAMMPS EAM 文档](https://docs.lammps.org/pair_eam.html)（2026-10-06 核对）检查
setfl 的三行注释、元素数与文件顺序、密度/距离网格、逐元素原子头、嵌入能/密度表及
对称元素对表。数组必须齐全，数值有限；保留全部原字节和许可证据。
按 [LAMMPS 固定版本读取器](https://github.com/lammps/lammps/blob/stable_2Aug2023_update4/src/text_file_reader.cpp)
的行读取语义处理数组：到达声明个数后，末行多余项不移入下一数组；保留这些项并在
静态记录中列出数组、行号和忽略个数。所有项仍检查为有限数值；额外独立行、截断或
未声明表仍拒绝，不靠改写势函数文件通过检查。
原子头中的零晶格常数及 dummy 标签按引擎文档保留，不能据此判断模型失效。

文件元素顺序和模拟原子类型顺序分别记录。例如文件为 Ni、Co、Cr，而原子类型为
Co、Cr、Ni 时，生成 `pair_style eam/alloy` 与按 Co、Cr、Ni 排列的 `pair_coeff`。
无需使用文件中全部元素，重复类型映射可保留；不接受 NULL 或未声明元素。
没有 MANYBODY 部署声明时拒绝绑定，不从文件后缀猜测为 funcfl、eam/fs、eam/cd 或混合势。
新资源仍需来源、许可和环境审查；此静态绑定不是模型适用性或新论文复现通过。

## 旧参数的明确适配

Issue #27 增加管理员选项 `legacy_snap_pins`，它必须是 `allowed_pins` 的子集，默认空。
管理员需先审查指定软件环境是否采用当前 SNAP 语法；此声明不能代替计算节点核验。
策略与 `software_sha256` 一并参与准备请求及后台恢复的配置身份，改变后不会继续执行旧排队项。
它不能由浏览器请求或模型返回值开启。

规则 `remove-diagonalstyle-3-v1` 依据固定版本的
[LAMMPS 官方说明](https://github.com/lammps/lammps/blob/d71abe6102c44577442ba7f03b7378a83166b9fd/doc/src/pair_snap.rst)：
2019 年删除旧参数时保留原默认的索引方式。仅接受明确写入 `diagonalstyle 3` 的资源，且必须明确
给出 `rcutfac, twojmax, rfac0, rmin0, quadraticflag, bzeroflag`，另外只允许 `switchflag`。
缺项、其他取值和额外参数均拒绝，不能猜测历史默认值或顺便转换其他模型变体。

`switchflag` 未写入时保留原字节，并在记录中明确列出依赖默认值 1，要求环境审查。
已核对 [2018 年实现](https://github.com/lammps/lammps/blob/b47e49223377d4ff6779e712bae54bbddc3596cf/src/SNAP/pair_snap.cpp)
与当前实现均使用 1；该历史版本的文档却写为 0，不能单凭旧文档补成 0。
这项源码核对不证明作者当时使用的具体二进制版本，也不授权未经核对的运行环境。

绑定时仅移除该参数所在行，其余参数字节、系数和许可原样保留。绑定记录保存规则、依据、
原始与实际参数 SHA-256 和移除的行号，并随候选快照保存。目录资源摘要仍指原件，
运行文件摘要指转换后的字节，两者明确区分；不将转换后的文件冒充作者原件。
适配层直接产生当前 `pair_coeff` 语法，不读取或改写作者目标输入脚本。

网页可用性、主 Agent 资源筛选和最终绑定使用同一检查。转换不绕过元素、单位、相互作用、
包声明或其他阻断项，不提高模型状态或授权执行。数值等价性仍未实测，记录明确为未验证。

## 验证边界

所有记录当前固定为 `collected`（已收集），尚无 `connected/task_verified` 的晋级操作。
静态绑定成功不是实际软件环境核验，更不是科学适用性证明。环境包清单是管理员声明；
接入计算节点后还须按已批准规则验证。元数据说明与允许列表也不等于正式盲测隔离：
管理员必须检查允许模型内容，另行落实身份、挂载、工具和网络边界。

真实 MLEARN Cu SNAP 资源已在私人目录导入：固定来源提交、核对 Git blob、保留 BSD-3-Clause
许可与原始字节；随后核实作者生成器使用独立 SNAP，并另存元数据版本。已完成旧参数的静态
绑定转换核验，原件仍不变；拟议软件环境未部署，数值等价、论文准确调用及适用性尚未验证。
当前状态仅为已收集，没有宣称势函数已经接通或论文已经复现。公有仓库不包含该模型文件或私有导入记录。
