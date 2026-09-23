# 现状审计：2026-09-23

审计为只读，未修改已有科研项目。私有路径、账号设置与审计材料不出本机。

| 对象 | 观察到的证据 | 结论 |
|---|---|---|
| SIGA-LAMMPS | 源码版本 dd2e0e7f7a7381ff8286fd1152ae1ee4958e319b；hpc/client.py、slurm.py、adapter/mcp_server.py 及测试存在；另有未提交文档 | 保留作为基线，README 的旧运行成绩未在本轮重验 |
| 原执行入口 | render_job_script、submit、status、accounting、cancel、read_log、fetch_results | 可复用领域边界，不能直接视为符合新评测约束 |
| 提交记账 | Slurm submit 在成功解析作业号后才记录；审计写失败吞掉 OSError；部分状态位于进程内字典 | 会遗漏拒绝/不明请求；需提交前事务记账、并发额度和恢复对账 |
| 连接与脚本 | AutoAddPolicy；目录拼接进入 bash 命令；存在本地模拟配置 | 需严格主机认证、路径与命令校验及禁本地目标计算 |
| Auto Research | 当前 handoff 已读；document_recognition、visual_evidence、evidence_export 中存在身份校验、图表索引与字段导出 | 可复用数据接口；本轮未验证提取科学准确率，也不复制受限源码 |
| 文献字段 | 导出含 DOI、source_page/source_locator、source_excerpt、material/method/conditions | 可作为任务条件来源，仍需新增结果来源分类和输入/目标隔离 |
| Zotero | 本地 API 已成功读；直接只读 SQLite 被锁后停止访问，改用 API | 使用标准 API，未停止 Zotero、修改库或解锁 |
| HPC | 既有配置存在；只读 SSH 检查 sbatch/squeue 入口成功 | 仅证明认证与命令可达，未证明计算节点/包/科学结果可用 |
| 运行模型 | 本地提供方配置和凭据文件存在 | 未读取/输出密钥，未调用收费 API；新项目协议与预算未确认 |

既有工作台安装验收和历史模型调用不属于本项目成果。现有 Claude 特定入口与历史运行记录仍需进一步定位，不能仅凭 SIGA/dsh README 认定等价。

首次 Zotero 临时脚本只取父条目查询首页，得到不完整候选计数；新客户端处理分页、子条目与多层父引用并在真实接口复验。早期计数不作为最终候选覆盖率。原始异常与改进记录留在本机，不删除失败或包装成首次通过。
