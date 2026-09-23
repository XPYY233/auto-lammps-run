# 第一篇开发候选

以下三篇已从本机文献候选中匹配，并核验出版页面和实际作者资源。仅公开出版信息，不公开本地库身份、参考源码副本、数值答案或任务包。它们不是隐藏测试。

| 候选 | 已验证资源 | 入选障碍与建议 |
|---|---|---|
| [Ultra-fast interpretable machine-learning potentials](https://doi.org/10.1038/s41524-023-01092-7) | [作者仓库](https://github.com/uf3/uf3)，固定 bc3279d9f986bd0b1f3720764af5196d989bea40；熔点 LAMMPS 脚本及 UF2 表文件存在 | 优先继续审计：论文方法与当前脚本在系统规模、时间步和采样阶段存在实质差异，不能直接称原文复现 |
| [PACE and application to copper and silicon](https://doi.org/10.1038/s41524-021-00559-9) | [作者实现](https://github.com/ICAMS/lammps-user-pace)，固定 99aa6e685cce24c24f81ce35b241d1b480d1ad05；[Zenodo 数据](https://zenodo.org/records/4734036)列出 Cu/Si 势函数及 Cu 数据 | 铜的结构性质可作为低成本范围候选，但已发布资源尚不足以证明目标图表的完整作者流程可直接运行 |
| [Automated discovery of a robust interatomic potential for aluminum](https://doi.org/10.1038/s41467-021-21376-0) | [ANI-Al 仓库](https://github.com/atomistic-ml/ani-al)，固定 5db4cf050d129ffc3530aacb75d5b4cfb0f41780；模型和数据存在 | NeuroChem 与 LAMMPS 集成及目标脚本未核实，暂不选为首篇 |

建议的第一任务范围（待解决矛盾后确认）：UF2 钨两相共存熔点任务，包含完整初始化、两相制备、NPH 生产、末段温度统计及相共存检查；覆盖一个有物理意义的外推验证任务。不包括全论文模型训练、全势函数横向性能、硅或声子结果。结果是否直接来自 LAMMPS 逐项判断；作者 tungsten_properties notebook 使用独立 UFCalculator，不能整页算入 LAMMPS 目标。当前不批准运行，亦未将任何候选标为合格参考。

需要核实：论文对应历史版本、势文件哈希、质量参数来源、完整一致的阶段设置、作者参考可重复性、集群所需包、相识别规则与容差、允许的初始温度/重复次数。作者方法中的按结果变更初温不能藏在一次正式提交内，必须预先拆分或冻结任务。

对 melting_uf.in 的仓库路径历史查询仅返回 2021 年提交 e83cdeaf22c362d30fb17af1cbcb459dea16c7c3；尚未找到与 2023 年论文方法一致的该路径修订。不能假定仓库最新输入就是最终发表条件。

成本尚无 HPC 实测，不能给可信核时或费用预测。建议先申请一项**有上限的开发验证**：单作业最多 8 核 × 30 分钟 = 4 核时、内存最多 8 GiB、并发 1、存储最多 1 GiB；初阶段总上限 12 核时（参考及 Agent 计算各自记账）。这是待批准的停止上限，不是耗时估计或完成承诺；不能为塞入上限缩小原任务。模型调用上限暂为 0。完整任务若超过此上限，应停止并重新提出预算。

原创代码许可建议 Apache-2.0，尚未应用。预算、任务范围、评分规则和许可均由用户确认后分别冻结；新权限不能从历史项目配置推定。
