# Phase 14 — 通用完成契约、降级与有界重规划

## 设计原则

1. **用户目标优先于模型自行生成的步骤细节。** Analyzer 将必需完成条件存入 `TaskSpec.completion_criteria`，将用户明确表达的可选偏好放入 `optional_criteria`。
2. **要求有来源编号。** `requirements.py` 从 TaskSpec 确定性生成 `R1..Rn`（必需）与 `O1..On`（可选），Planner 和 Replanner 的每个新增步骤必须引用存在的 `requirement_ids`；初始计划要覆盖全部必需编号，重规划不能遗漏尚未被已验证历史步骤覆盖的必需编号。缺少、伪造或遗漏必需编号均拒绝计划。
3. **完成必须有证据。** Executor 的 `finish_step` 只是声明；Step Verifier 检查原始 ToolCallRecords，Task Verifier 再核对任务级完成条件。可选条件缺失不得阻止满足全部硬约束的任务完成。
4. **失败先分类。** `failure_analysis.py` 按真实 `StepExecutionOutcome` / 验证反馈区分 action limit、global budget、tool error、evidence gap；分类本身不是成功证明。
5. **有界重规划。** 单个 Step 保留 `max_actions_per_step=10`；局部上限耗尽后允许 Replanner 在剩余 `max_replans` 和整个任务 `max_total_actions` 内改变策略。全局预算达到上限时禁止重新创建步骤以获得额外额度。
6. **只复用真正只读的历史观察。** Tool 必须由 Host metadata 同时显式标记 `read_only=true`、`replay_safe=true`；成功记录才能设置 `ToolCallRecord.reusable_evidence`。此前未验证的完成声明、写入动作、高风险点击的结果绝不自动标记为可复用证据。
7. **部分完成不等于成功。** 有已验证 StepResults、但还有必需条件未满足时，终态为 `partial` 并输出证据与缺失信息；没有已验证成果时为 `failed`；Verifier/Runtime 异常或用户取消不能伪装为降级成功。

## Benchmark

在仓库根目录、已安装测试依赖的 Python 环境中运行：

```powershell
python -m benchmarks.phase14_contract_benchmark
```

报告写入 `benchmarks/results/<UTC>-phase14-contract/`：

- `summary.json` — 8 个功能边界的覆盖、通过/失败/跳过数、环境信息。
- `report.md` — 面向代码评审的总结。
- `pytest.xml` — 每一条断言场景的 JUnit 结果。
- `pytest.log` — 完整测试输出。

该基准联合新编写的 Phase14 状态机测试、已有的真实 Chromium 页面测试、MCP/文件生成测试、审批和危险操作边界测试、SQLite/Journaling 恢复测试。**它不将 pytest 通过率当成真实模型的任务成功率。**

## 真实模型基准

在已配置 DeepSeek `LLM_API_KEY` 和对应模型的机器上运行：

```powershell
python -m benchmarks.phase14_real_llm
```

执行至多两次真实模型 + Chromium 只读任务，使用本地可控网页：

- `phase14-orders.html`：5 条原始采购订单，有旧报价与干扰项。
- `phase14-audit.html`：6 条审核记录；更新报价、待审核、取消、零库存、只存在于审核页的记录。
- 需要交叉关联两页、选择两笔符合条件的订单，给出审核后价格、库存以及两个来源 URL。
- 第 2 个任务明确规定「有独立详情页可选附上」，缺失时应省略，而不是为了非必要条件继续重复探索。

**独立 Oracle** 检查 Graph 状态、两个来源的访问记录、预设 ID/名称/价格/库存、来源 URL、工具白名单和没有未经批准的动作。其结果与 Graph 自报 `completed` 分开统计，单列 False Completion。运行详情和总结写在 `benchmarks/results/<UTC>-phase14-real-llm/`。

### 2026-10-21 验证记录

| 项目 | 结果 |
| --- | --- |
| pytest 全量 | 342 passed, 1 skipped |
| Phase14 跨功能基准 | 182 passed, 1 skipped |
| 真实 DeepSeek 双来源冲突核对 | PASS + External Oracle PASS |
| 真实 DeepSeek 可选详情页降级 | PASS + External Oracle PASS |
| 两轮真实模型 False Completion | 0/2 |

## 已知边界与后续方向

- Requirement ID 能检查引用来源、未知 ID、遗漏必需条件；**不能形式化证明 Planner 的自然语言步骤在语义上完全等价于用户请求**。此问题需要独立的语义审查或更明确的机器可执行完成条件，不能靠映射假装已解决。
- 当前部分完成只使用经过 Step Verifier 验证的正式 StepResults；如果第一步未通过验证，即使原始工具数据有价值，也不会把它作为已完成结果直接提升。
- 历史只读网页观察可能过期，故允许复用不等于自动判定正确；任务是否需要重新检查时效仍由 Verifier 依据任务约束判断。
- 此次两轮真实模型均成功，但未在真实 DeepSeek 测试中强行诱发高风险审批、工具崩溃、动作超限与重规划；这些边界由离线状态机和 Chromium/审批测试覆盖。
