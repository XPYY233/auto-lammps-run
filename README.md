# Auto-LAMMPS

面向课题组自行部署的自然语言科研计算服务。研究者描述需求，Agent 设计方案，通过领域 Adapter 准备结构、调用势函数、生成 LAMMPS 输入、提交 HPC，并自动分析、可视化和报告。正常研究不要求提供论文。

文献复现是验证同一产品流程的测试体系：文献工作台生成测试需求，参考端运行作者代码建立 P-A 依据，再由产品主流程独立得到 B。论文源码测试集库与供计算使用的势函数库分别维护；目标作者源码不提供给被测 Agent。第一周用一篇真实论文的完整任务验收最小产品链路，随后扩大覆盖。

**当前为开发基础，尚未完成自动科研计算。** 已实现只读 Zotero 文献发现、持久提交账本、输入快照、受控 Slurm 只读查询、事务恢复与受限上传、受信提交及计算节点启动器代码。账本与执行代码仅完成离线验证，指定入口的空作业查询已实测。已有本机任务页及可选 DeepSeek 条件整理入口，后者仅通过合成响应验证、默认不启用。真实计算部署、主 Agent 规划执行和独立评分尚未完成，没有目标模拟结果。

## 现在可以运行

需要 Python 3.11+、运行中的 Zotero 及其本地 API。本地文献与控制工具仅使用 Python 标准库；未部署的 Linux 启动器另外依赖 Bubblewrap/libseccomp。

```sh
python3 -m pip install -e '.[web,test,geometry]'
python3 -m unittest discover -s tests -v
python3 -m auto_lammps --query LAMMPS --output "$HOME/.local/share/auto-lammps-private/discovery.json"
```

输出必须位于 Git 仓库之外，文件权限为仅本人读写；已有文件不会覆盖。程序只读取本机 API，不下载 PDF、不读取数据库、不调用模型、不执行 LAMMPS。搜索命中只是线索，不能证明论文中任意图表由 LAMMPS 生成。schema v2 分别报告参考条目数和不同 DOI 字符串数，保留重复 DOI 组；不得把条目数当作独立论文数。

## 本机任务确认页

```sh
python3 -m auto_lammps.web --data-directory "$HOME/.local/share/auto-lammps-private/workbench" --port 8785
```

浏览器访问 `http://127.0.0.1:8785/`。可以保存需求、核对条件、解决冲突并冻结导出；支持预览文献工作台 CSV 并明确导入输入条件，仍不执行模拟。说明见 [TASKS.md](docs/TASKS.md) 与 [文献导入](docs/LITERATURE_IMPORT.md)。

科研计算不要求论文。管理员配置独立模型与调用额度后，可自动整理自然语言中的条件并显示原文依据和缺项；模型配置与实际验证边界见 [DeepSeek 条件整理](docs/MODEL_RUNTIME.md)。

私人势函数目录及首个领域适配操作已支持固定 SNAP 资源与静态调用检查，见 [势函数资源](docs/POTENTIALS.md)。尚未接入主 Agent 自动选择或实际计算，已收集不代表已验证。

候选方案生成已连接模型接口、ASE 几何准备、势函数绑定和输入快照，见 [Agent 候选方案](docs/AGENT_CANDIDATES.md)。目前使用合成模型响应验证软件串联，真实模型能力、网页接入与 HPC 执行尚未验证。ASE 仅用于建模和文件读写，不在本机做物理计算。

“复现文献与历史”提供候选与已选清单、条件版本及已有账本次数；见 [历史记录边界](docs/PAPER_HISTORY.md)。当前独立评分尚未接入，不产生已复现记录。[第一周逐项核对](docs/WEEK1_AUDIT.md)列出实际完成与未完成要求。

冻结后可自动导出计算任务草稿和独立参考准备资料，见 [资料导出](docs/TASK_PACKAGES.md)。仅完成字段分离；条件内容与允许资源尚需核验，不能将草稿直接用于正式评测。

## 开发与部署

- [目标与三周路线](docs/GOALS.md) · [进度入口](docs/PROGRESS.md)
- [现状审计](docs/AUDIT.md) · [开发候选](docs/CANDIDATES.md)
- [提交账本](docs/LEDGER.md) · [输入冻结与只读查询](docs/FROZEN_INPUTS.md) · [作业恢复](docs/RECOVERY.md) · [受限上传](docs/STAGING.md) · [提交与启动器](docs/RUNTIME.md) · [任务确认网页](docs/TASKS.md) · [架构决定](docs/ARCHITECTURE.md) · [评测规范](docs/EVALUATION.md)
- [部署](docs/DEPLOYMENT.md) · [安全](docs/SECURITY.md)
- [依赖与许可](docs/LICENSE_INVENTORY.md) · [变更记录](CHANGELOG.md)

软件运行模型使用独立配置的 API；开发者的 ChatGPT/Codex 登录不作为运行后端。所有目标物理计算只能通过记账入口在批准的 HPC 计算节点运行。公开代码不包含论文附件、科研数据库、集群配置、凭据、作者参考源码或隐藏答案。服务默认仅本机/内网可访问。无 LAMMPS 官方背书。

原创部分已获授权采用 Apache-2.0；第三方权利不变，见 [版权说明](COPYRIGHT.md)。
