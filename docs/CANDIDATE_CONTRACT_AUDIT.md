# 候选方案契约审计（2026-09-28）

用户要求：先审核问题，不要急于重试。本文只记录可复现的证据与判断。

## 一、结论

应用"方案准备"阶段的连续失败**主要是产品缺陷，而非模型能力不足**：
校验器强制执行的契约中，有相当比例的规则**从未出现在给模型的提示词里**，
模型只能猜测；每猜错一条，就消耗一轮准备。

## 二、规则来源

| 文件 | 规则数 | 示例 |
|---|---|---|
| `auto_lammps/analysis.py` | 约 30 条 | `Declare two to sixteen labeled columns`；`Analysis must use a distinct declared output file`；`Operation table is not declared`；`Operation columns are not distinct declared columns`；`Freeze an inclusive analysis window before execution` |
| `auto_lammps/agent_candidates.py` | 约 20 条 | `Unsupported workflow command`；`Workflow writes must use declared flat /output/ filenames`；`Declare exactly the analysis files written by the workflow`；`Select an exact supplied potential pin`；`Clarification proposals must not contain a runnable candidate`；`Missing or excessive candidate text` |

## 三、覆盖率抽查（基线 = 我改动之前 `6d9f983^`）

未覆盖（模型无从得知）：

- 表列数 2–16（每表至少 x、y 两列）
- `analysis.files` 必须与工作流实际写出的文件一致
- `tables`/`operations` 的 `file` 必须属于 `analysis.files`
- `operations` 只能引用已声明的 table
- 操作 `x`、`y` 必须取自声明列
- `potential_pin` 必须逐字复制给定 pin
- 工作流写文件必须使用声明的 `<前缀><名>`

已覆盖：结构几何字段齐全、`orientation` 必须是 `cubic_axes`、支持单位清单、
允许命令/fix/compute 清单、操作 `method` 取值。

抽查结果：**12 条中 7 条未覆盖**。

历史证据：`git log -S "two to sixteen" -- auto_lammps/agent_candidates.py` 输出为空，
即该校验规则（由 `fa775ef` 引入）从未被写入提示词。

## 四、实测行为

- 失败序列（每条一轮）：`Operation table is not declared` → `invalid_json` →
  `Select an exact supplied potential pin` → `Analysis must use a distinct declared output file` →
  `Workflow writes must use declared flat /output/ filenames` → `All geometry fields must be explicit` →
  `This builder requires conventional cubic [100], [010], [001] axes` → `Declare two to sixteen labeled columns`。
- 最后一轮中，模型对 6 个文件都只声明 1 列；自动修复（最多 3 轮）重复同一缺陷。
- 其中两条失败源于**驱动方的答复格式**（取向字面量、列数），与应用无关。

## 五、与第一周成功的接口差异

| | 第一周 B | 应用当前路径 |
|---|---|---|
| 模型产物 | 完整 LAMMPS 输入脚本 | 结构化 JSON 方案 |
| 校验 | `bounded_command_and_output_screen`（命令数与输出登记） | 结构化方案校验（约 30 条）→ 渲染脚本 → 同一筛查 |
| 结果 | 成功提交 | 未通过方案校验 |

应用路径多出的这层结构化校验是**执行安全与分析/图表所必需**，不能简单删除；
但它要求提示词与校验器严格同步，而这正是当前缺失的机制。

## 六、修正建议（按优先级）

1. **契约单一事实来源**：提示词中的上限、清单与格式规则由校验器常量/表派生，禁止手写副本。
2. **一致性测试**：为每条校验规则断言提示词覆盖，缺失即 CI 失败（防止再次漂移）。
3. **结构化输出**：方案按 JSON Schema 生成，形状违规在生成阶段即不可能发生；语义校验保留。
4. **可选（需用户裁定）**：允许"模型直接写脚本 + 现有有界筛查"，与第一周接口对齐；
   代价是失去类型化表格、数值分析与图表基础。

## 七、复现命令

```bash
git show 6d9f983^:auto_lammps/agent_candidates.py | grep -n "plan is {tables,operations}"
git log -S "two to sixteen" --oneline -- auto_lammps/agent_candidates.py   # 空
grep -n "labeled columns" -B 6 auto_lammps/analysis.py
```
