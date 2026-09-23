# Auto-LAMMPS

面向课题组自行部署的文献复现工作台。目标流程：文献条件提取 → 作者参考运行 → 独立 Agent 生成 → HPC 提交 → 分析 → 独立科学比较。

**当前为开发基础，不是已完成的复现产品。** 已实现只读 Zotero 文献发现和受信控制端的持久提交账本；账本仅完成离线验证。生产调度适配器、网页、模型连接器和独立评分尚未实现。没有目标模拟结果。

## 现在可以运行

需要 Python 3.11+、运行中的 Zotero 及其本地 API。当前工具仅使用 Python 标准库。

```sh
python3 -m unittest discover -s tests -v
python3 -m auto_lammps --query LAMMPS --output "$HOME/.local/share/auto-lammps-private/discovery.json"
```

输出必须位于 Git 仓库之外，文件权限为仅本人读写；已有文件不会覆盖。程序只读取本机 API，不下载 PDF、不读取数据库、不调用模型、不执行 LAMMPS。搜索命中只是线索，不能证明论文中任意图表由 LAMMPS 生成。schema v2 分别报告参考条目数和不同 DOI 字符串数，保留重复 DOI 组；不得把条目数当作独立论文数。

## 开发与部署

- [目标与三周路线](docs/GOALS.md) · [进度入口](docs/PROGRESS.md)
- [现状审计](docs/AUDIT.md) · [开发候选](docs/CANDIDATES.md)
- [提交账本](docs/LEDGER.md) · [架构决定](docs/ARCHITECTURE.md) · [评测规范](docs/EVALUATION.md)
- [部署](docs/DEPLOYMENT.md) · [安全](docs/SECURITY.md)
- [依赖与许可](docs/LICENSE_INVENTORY.md) · [变更记录](CHANGELOG.md)

软件运行模型使用独立配置的 API；开发者的 ChatGPT/Codex 登录不作为运行后端。所有目标物理计算只能通过记账入口在批准的 HPC 计算节点运行。公开代码不包含论文附件、科研数据库、集群配置、凭据、作者参考源码或隐藏答案。服务默认仅本机/内网可访问。无 LAMMPS 官方背书。

原创部分已获授权采用 Apache-2.0；第三方权利不变，见 [版权说明](COPYRIGHT.md)。
