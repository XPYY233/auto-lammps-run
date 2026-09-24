# 势函数资源与适配接口

Issue #21 为产品执行链路增加私人势函数目录和 `resolve_potential` 领域操作。
当前支持独立 SNAP / qSNAP 文件的静态资源绑定，尚未接入主 Agent 自动选择、网页资源浏览或真实计算。
源码测试库与这里的运行资源目录分别维护；导入只接受系数、参数及许可三个文件。

## 固定资源

管理员将已审查资源导入仓库之外的私人目录。记录名称、元素顺序、格式、单位、来源 URL、
固定 Git 提交或源归档 SHA-256、原始路径、许可、适用范围和调用依据。
文件逐一记录 SHA-256，整体内容地址固定元数据和文件；重复导入返回同一标识，修改产生新记录。
每次读取重新检查文件与记录摘要、符号链接、硬链接及额外文件，保留原始模型字节及许可证。
不下载模型、不运行模拟、不自动修订旧格式。管理员可明确批准下述窄范围绑定转换；目录原件不变。

管理员入口（不提供给模型）：

```sh
python3 -m auto_lammps.potentials --store "$HOME/.local/share/auto-lammps-private/potential-catalog" import --source MODEL_DIRECTORY --metadata IMPORT_JSON
python3 -m auto_lammps.potentials --store "$HOME/.local/share/auto-lammps-private/potential-catalog" list
```

导入 JSON 的 `metadata` 包含 `name, format, elements, units, source, license, applicability,
usage_evidence, interaction`。`source` 包含 `url, revision, locator`。
`files` 将 `coefficients, parameters, license` 分别映射到源目录内的安全文件名。
初版仅接收 `snap` 格式、`metal/real` 单位声明；单位必须通过模型来源核对，文件本身不证明其单位。
`interaction` 为 `standalone/hybrid/unresolved`，不允许静默遗漏额外相互作用。

## 产品侧调用

服务创建 `PotentialAdapter`，配置该任务允许使用的资源摘要、固定软件环境摘要和依赖包声明。
Agent 提交资源摘要、按原子类型顺序排列的元素与任务单位。接口检查允许列表、文件完整性、
元素映射、单位、声明的 ML-SNAP 包、参数格式以及常规 SNAP / qSNAP 的系数维度。
返回固定文件名下的模型、许可证、两条势函数设置语句和可追溯绑定记录。
重复元素映射保留原子类型顺序，不排序、不猜测，不接受 NULL 混合映射。

现有 `manifest.freeze` 可将这些字节、绑定记录与后续 Agent 生成的输入一并冻结；集成测试已覆盖。
输出没有完整模拟流程，也没有作者目标脚本。此处不提交作业，未消耗计算提交次数。
正式提交仍须经过任务确认、隔离部署、预算与提交账本。

静态实现依据 [LAMMPS SNAP 文档](https://docs.lammps.org/pair_snap.html) 与
[描述符维度定义](https://docs.lammps.org/compute_sna_atom.html)，核对日期 2026-09-24。
化学分辨 SNAP、内切换、多势叠加和未知参数暂时阻断；旧 `diagonalstyle` 原样保存并默认要求版本审查。
不能把此限制解释成原论文或模型无效。

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
