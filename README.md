# Auto-LAMMPS

面向课题组自行部署的自然语言科研计算服务。研究者描述需求，Agent 设计方案，通过领域 Adapter 准备结构、调用势函数、生成 LAMMPS 输入、提交 HPC，并自动分析、可视化和报告。正常研究不要求提供论文。

文献复现是验证同一产品流程的测试体系：文献工作台生成测试需求，参考端运行作者代码建立 P-A 依据，再由产品主流程独立得到 B。论文源码测试集库与供计算使用的势函数库分别维护；目标作者源码不提供给被测 Agent。第一周用一篇真实论文的完整任务验收最小产品链路，随后扩大覆盖。

**当前处于第一周开发验证，尚未完成自动科研计算。** 已有自然语言任务工作台、文献清单与历史、固定输入与提交账本、受控 Slurm 执行、候选准备和分析模块。首个真实作者参考 A 已在 HPC 完成完整原流程，并形成 P–A 诊断数据与曲线；科学口径仍需核验，独立 B 尚未开始。现有 DeepSeek 条件整理接口仅用合成响应验收，真实 API 额度为零；其余模型商目前仅可保存选择偏好。

## 现在可以运行

需要 Python 3.11+、运行中的 Zotero 及其本地 API。本地文献与控制工具仅使用 Python 标准库；未部署的 Linux 启动器另外依赖 Bubblewrap/libseccomp。

```sh
python3 -m pip install -e '.[web,test,geometry]'
python3 -m unittest discover -s tests -v
python3 -m auto_lammps --query LAMMPS --output "$HOME/.local/share/auto-lammps-private/discovery.json"
```

输出必须位于 Git 仓库之外，文件权限为仅本人读写；已有文件不会覆盖。程序只读取本机 API，不下载 PDF、不读取数据库、不调用模型、不执行 LAMMPS。搜索命中只是线索，不能证明论文中任意图表由 LAMMPS 生成。schema v2 分别报告参考条目数和不同 DOI 字符串数，保留重复 DOI 组；不得把条目数当作独立论文数。

## 本机研究工作台

```sh
python3 -m auto_lammps.web --data-directory "$HOME/.local/share/auto-lammps-private/workbench" --port 8785
```

浏览器访问 `http://127.0.0.1:8785/`。从自然语言创建任务，查看任务、文献、资源和历史。配置已有私有参考报告及账本后，可查看真实 P–A 数据、曲线和下载文件；详见 [研究者工作台](docs/RESEARCH_WORKSPACE.md)。条件与证据编辑按需展开，仍保留 [任务条件](docs/TASKS.md) 与 [文献导入](docs/LITERATURE_IMPORT.md) 功能。网页不会因保存需求或模型偏好而提交计算。

科研计算不要求论文。管理员配置独立模型与调用额度后，可自动整理自然语言中的条件并显示原文依据和缺项；模型配置与实际验证边界见 [DeepSeek 条件整理](docs/MODEL_RUNTIME.md)。

私人势函数目录已支持固定 SNAP 与 MEAM 资源及静态调用检查，见 [势函数资源](docs/POTENTIALS.md)。已接入候选生成的资源筛选；MEAM 库索引与原子类型顺序分开保留，旧 SNAP 参数转换需管理员逐项指定并保留来源。尚未完成适用性自动选择或实际计算，已收集不代表已验证。

候选方案生成已连接模型接口、ASE 几何准备、势函数绑定和输入快照，见 [Agent 候选方案](docs/AGENT_CANDIDATES.md)。网页与阶段历史已通过独立合成验收，见 [准备记录](docs/CANDIDATE_HISTORY.md)；真实模型能力与 HPC 执行尚未验证。ASE 仅用于建模和文件读写，不在本机做物理计算。

结构准备支持常规立方晶体及有明确晶胞、元素和基元坐标的非立方结构，保留原子顺序、边界和计算方向。
该能力已接入同一候选流程；缺失结构信息的自动检索与科学适用性核验仍未完成。

受信控制端可回收已结束且完成核算的作业输出，校验文件并把下载副本纳入存储预算，见
[结果回收](docs/OUTPUTS.md)。通用回收器通过合成验证；本次真实 A 使用独立的参考端核验和报告发布，不能据此宣称通用自动回收已验收。

已有冻结计划驱动的数值表分析，可计算区间统计和线性拟合并留存来源与报告，见
[分析范围](docs/ANALYSIS.md)。相同数值计算函数已复用于真实 A 的远端诊断；通用自动回收与分析闭环、结构分析、OVITO 和独立科学评分仍待完成。

配置已有账本与结果目录后，任务页可查看提交次数、数值与单位、数据来源、报告下载和运行
历史，见 [结果页面](docs/RESULTS_VIEW.md)。读取仅核验保存报告及来源回执，不发起计算。

提交后的受信后台可自动跟进调度、回收输出并完成冻结计划分析，查询额度跨重启保留，见
[自动跟进](docs/FOLLOWING.md)。已通过合成串联及进程退出恢复检查，尚未在真实 HPC 启用。

候选方案已连接已有许可下的上传、一次派发和自动跟进，见
[受控执行连接](docs/AUTHORIZED_EXECUTION.md)。后台核对许可及精确输入绑定，不自动签发
批准；尚未部署真实提交、科学检查或网页执行入口。

“复现文献与历史”提供候选与已选清单、条件版本及已有账本次数；见 [历史记录边界](docs/PAPER_HISTORY.md)。当前独立评分尚未接入，不产生已复现记录。[第一周逐项核对](docs/WEEK1_AUDIT.md)列出实际完成与未完成要求。

冻结后可自动导出计算任务草稿和独立参考准备资料，见 [资料导出](docs/TASK_PACKAGES.md)。仅完成字段分离；条件内容与允许资源尚需核验，不能将草稿直接用于正式评测。

参考端已提供基于题目与 DOI 的 [源码检索](docs/SOURCE_DISCOVERY.md)，核查固定版本 README 并将结果显示在文献历史中。当前由管理员入口调用，尚未自动串联论文导入。

参考端已有保留来源的作者输入路径适配，见 [参考输入适配](docs/REFERENCE_PATHS.md)。
首个真实 A 已按原作者完整流程执行；调度结束、输出有效与科学通过分别记录。进入 B 前报告策略并取得确认。

参考端另有独立 API 驱动的条件/论文结果草稿服务，见 [自动证据整理](docs/REFERENCE_EVIDENCE.md)。
已接入网页的论文证据、引文和整理历史，支持恢复已返回草稿；仅完成合成响应验收，第二周才启用真实 API。研究者界面已按批准图片改造；用户允许在等待 B 策略确认期间先行改界面。自然语言到真实计算的完整人类使用验收仍未完成。

## 开发与部署

- [目标与三周路线](docs/GOALS.md) · [进度入口](docs/PROGRESS.md)
- [现状审计](docs/AUDIT.md) · [开发候选](docs/CANDIDATES.md)
- [提交账本](docs/LEDGER.md) · [输入冻结与只读查询](docs/FROZEN_INPUTS.md) · [作业恢复](docs/RECOVERY.md) · [受限上传](docs/STAGING.md) · [提交与启动器](docs/RUNTIME.md) · [任务确认网页](docs/TASKS.md) · [架构决定](docs/ARCHITECTURE.md) · [评测规范](docs/EVALUATION.md)
- [部署](docs/DEPLOYMENT.md) · [安全](docs/SECURITY.md)
- [依赖与许可](docs/LICENSE_INVENTORY.md) · [变更记录](CHANGELOG.md)

第一周由 Codex 负责 P-A-B 真实流程验证；首次跑通后再配置第二周使用的独立 API。第二周及后续软件运行不依赖开发者的 ChatGPT/Codex 登录。所有目标物理计算只能通过记账入口在批准的 HPC 计算节点运行。公开代码不包含论文附件、科研数据库、集群配置、凭据、作者参考源码或隐藏答案。服务默认仅本机/内网可访问。无 LAMMPS 官方背书。

原创部分已获授权采用 Apache-2.0；第三方权利不变，见 [版权说明](COPYRIGHT.md)。

管理员可按固定发布版本和势函数要求在 HPC 准备官方引擎源码，控制端仅保存审计
记录；见 [计算环境准备](docs/ENGINE_PREPARATION.md)。该步骤不编译、不运行模拟，
不能据此宣称环境或论文复现已通过。
