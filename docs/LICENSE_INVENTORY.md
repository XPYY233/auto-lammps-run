# 许可与复用清单

2026-09-23 初审；尚无软件发行包。

| 资源 | 已核实 | 决定 |
|---|---|---|
| 本项目新代码 | 核心使用 Python 标准库，网页调用 FastAPI/Uvicorn；第三方改编另列 | 用户已确认 Apache-2.0；见 LICENSE |
| 既有 SIGA-LAMMPS | 本机 LICENSE 为 MIT；HPC 与 adapter 代码存在 | PR 1b 改编 hpc/slurm.py 状态映射，版本 dd2e0e7；保留 third_party/SIGA-LAMMPS-LICENSE.txt，范围见 FROZEN_INPUTS.md |
| Auto Research | COPYRIGHT 仅允许有限本机使用，未授予一般改编再分发 | PR 2b 独立实现 CSV 数据格式适配，审计版本 69b83f1；无源码复制，见 LITERATURE_IMPORT.md |
| DeepSeekHarness | 本机源码存在；完整依赖/许可证审计未做 | 不内嵌或重打包 |
| uf3/uf3 | GitHub 及 LICENSE 元数据为 Apache-2.0 | 索引固定版本；作者参考源码仅私有评估端保存 |
| ICAMS/lammps-user-pace | GitHub 许可证识别为 NOASSERTION | 逐文件核查未完成，不再分发 |
| PACE Zenodo 4734036 | 数据与势函数文件可见 | 具体授权需再核，不镜像 |
| ANI-Al | 模型和数据仓库可见 | 运行依赖和许可待核，不镜像 |
| materialsvirtuallab/mlearn | 固定提交 10c427a5480c6281c15c64efaf869b03be04818f；根 LICENSE 为 BSD-3-Clause，版权 Materials Virtual Lab 2019 | 仅私人资源库保存原始 Cu SNAP 模型及许可证；无作者工作流代码复制，无公开再分发；见 POTENTIALS.md |
| FastAPI 0.141.1 / Uvicorn 0.52.0 / HTTPX 0.28.1 | PyPI 元数据分别为 MIT / BSD-3-Clause / BSD-3-Clause；HTTPX 仅测试使用 | 通过官方包安装，不内嵌第三方源码；发行前继续核查完整传递依赖 |
| Bubblewrap / libseccomp | 仅调用系统安装版本的程序/API，无源码或二进制内嵌；部署版本、依赖及许可证仍需固定审计 | 不随原创代码重新许可，不打包分发 |
| LAMMPS、OVITO、PyMuPDF/ASE | 尚未作为本项目运行依赖安装或打包 | 接入前单独核对版本及许可，不套用项目原创许可 |
| 论文正文、图表、附件 | 不因用户本机可读而获得再分发权 | 不进入公有仓库 |

第一版不修改 LAMMPS 内核，不暗示任何第三方背书。
