# 部署与配置

当前可部署的是文献发现 CLI，尚无运行网页、计算 worker 或模型 API 服务。从仓库根目录使用 Python 3.11+ 直接运行，不需要安装第三方包。

Zotero 保持运行并启用本地 API；API 不可用时检查客户端设置，不尝试修改或解锁数据库。读取端参照 [Zotero API v3](https://www.zotero.org/support/dev/web_api/v3/basics)。检索仅覆盖已有索引，完整 PDF 覆盖未知。分页检查总量、重复和版本；缺少版本时明确标为 count_only，不声称原子快照。输出是本机私人审计，不可直接发布。

```sh
python3 -m auto_lammps --query LAMMPS --output "$HOME/.local/share/auto-lammps-private/discovery.json"
```

每次用新文件名；数据保留于仓库之外。恢复开发先读 PROGRESS.md，再核实实际 PR 状态。

计划配置：服务器端注入模型凭据、批准的 SSH 别名、调度分区/账户、程序版本、私有工作区、每作业与总核时/内存/存储/并发/模型上限。例子不得包含实际内部路径。预算缺失时服务拒绝执行；不能沿用旧项目额度作为新项目授权。

后台重启需恢复持久任务而非重提。本地后台离线或电脑休眠后无法承诺实时管理；多人正式使用时部署在持续在线服务器并补访问控制，迁移前先验证备份和恢复。
