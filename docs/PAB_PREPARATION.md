# 当前 PAB 准备范围

论文复现验证普通自然语言科研产品，不另建 B 执行链。P 是原文结果与出处，A 是
固定作者源码运行，B 是从筛选输入出发、通过正常科研任务页面得到的独立结果。

第一周 NbTiZrMoV 拉伸基准已由用户验收，继续保留已复现状态和单工况限制。
本轮不重跑该基准。W 空位任务只验证流程，不作为论文准确度样本。

| 新候选 | 来源与当前准备证据 | 未完成 |
| --- | --- | --- |
| [Prediction of defect properties in concentrated solid solutions using a Langmuir-like model](https://doi.org/10.1103/PhysRevMaterials.9.033803) | 本地预印本；[作者仓库固定提交](https://github.com/jwjeffr/impurities/tree/340511223485fa95d63da22ad025b88ac5d2b87e)；FeAl 作者输入、配套 MEAM 与后处理依赖已取得，源码为 MIT | 预印本与出版版对应、目标/评分冻结、作者运行与普通页面 B |
| [Graph neural network framework for energy mapping of hybrid monte-carlo molecular dynamics simulations of Medium Entropy Alloys](https://doi.org/10.48550/arXiv.2411.13670) | 本地预印本；[作者仓库固定提交](https://github.com/mashaekh-tausif/MCMD-GCNN/tree/dd8f351f415ee6bd40eb29763aa07a35139b21ad)；作者生成器、结构与 EAM 已取得 | 数值曲线提取、路径适配与许可范围、目标/评分冻结、作者运行与普通页面 B |

新增第三篇尚未匹配。库中势函数、模型或训练示范不能自动视为作者模拟工作流。
两个仓库的已发表输出均超过 2 GiB；当前只取得所选工作流依赖，不称完整仓库。
原始作者文件与下载字节留在配置 HPC，本机保留元数据和证据回执。

原文通过现有文献工作台的本机模块提取，没有复制受限上游代码或改生产数据库。
图号漏识别、表格框只包含图注、结构提取失败与人工补定位均分别保留。
图表清单不等于数值 P 已核验；选定目标与评分规则必须在新计算前完成。

生成 B 的输入另存，只允许材料、工况、结构及势函数，不携带作者脚本、P/A、
评分答案或可检索这些内容的索引。普通页面验收不冒称正式盲测隔离。
