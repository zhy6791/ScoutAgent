# 竞品分析 Agent 项目导学

> 项目简称：竞品分析agent  
> 求职方向：AI 应用开发  
> 源码基线：当前工作区，Git `325f089`，包含未提交修改  
> 事实口径：以当前源码和测试为准，`docs/功能实现技术说明.md` 用于补充设计意图  
> 职责边界：用户尚未说明个人是主导、独立开发还是协作参与，涉及 ownership 的表述均待确认

## 1. 前置知识（面试高频标注）

| 知识点 | 为何需要 | 在本项目中的位置 | 高频度 |
|---|---|---|---|
| LLM 结构化输出与容错 | 模型输出不是稳定 API，需要校验、重试和兜底 | `backend/app/core/llm.py`、`orchestrator.py` | 高 |
| Agent 编排与状态机 | 理解节点、角色、消息和返工的关系 | `orchestrator.run_pipeline()`、`models.Envelope` | 高 |
| RAG/Evidence/Claim | 项目核心是“结论必须能回到来源” | `models.py`、`credibility.py`、`audit.py` | 高 |
| 搜索与网页抓取 | 公开信息采集不是一次 LLM 问答 | `backend/app/core/search.py`、`backend/app/core/fetcher.py`、`backend/app/core/sentiment.py` | 高 |
| SSE 流式通信 | 流水线运行时间长，需要实时推送过程 | `main.stream_task()`、`frontend/src/lib/api.ts` | 中高 |
| 质量门禁与返工 | 规则和语义检查如何共同决定放行 | `audit.py`、`orchestrator.py` | 高 |
| SQLite 事务与并发 | 报告、证据、Trace、订阅都落在本地数据库 | `db.py` | 中高 |
| 定时任务与租约 | 竞品订阅需要防重、恢复和失败退避 | `subscription_scheduler.py`、`monitor_runner.py` | 中高 |
| React 状态管理与可视化 | 工作台需要消费事件并还原 DAG、证据流和 Trace | `taskStore.ts`、`WorkspacePage.tsx` | 中 |
| 测试替身与故障注入 | 外部搜索和 LLM 不适合直接进入单元测试 | `tests/` | 中高 |

## 2. 重点亮点与学习顺序（先看这个）

| 亮点标题 | 为什么重要 | 通用技术关键词 | 先看哪些文件 | 建议学习顺序 |
|---|---|---|---|---:|
| 可扩展任务编排 | 把长耗时调研拆成可观察、可返工的阶段 | Pipeline、状态机、异步生成器、Envelope | `backend/app/core/orchestrator.py`、`models.py` | 1 |
| 证据约束的生成 | 限制 LLM 用错品牌证据或生成无来源结论 | Evidence、Claim、内容指纹、交叉验证 | `models.py`、`credibility.py`、`orchestrator.py` | 2 |
| 双重质量门禁 | 规则检查覆盖可计算条件，LLM 复审覆盖语义问题 | Rule + LLM、fail-closed、反馈路由、返工上限 | `audit.py`、`tests/test_audit_gate.py` | 3 |
| 流式可观测与回放 | 长任务可实时解释，结束后仍可审计耗时和 Token | SSE、Trace、ContextVar、持久化 | `trace.py`、`main.py`、`taskStore.ts` | 4 |
| 联网采集与降级 | 将搜索、正文抓取、舆情和失败隔离为可替换能力 | Provider、抓取、去重、超时、fallback | `backend/app/core/search.py`、`backend/app/core/fetcher.py`、`backend/app/core/sentiment.py` | 5 |
| 持续监控与变化检测 | 把一次调研扩展为周期性情报发现 | Scheduler、租约、Diff、SSRF、快照 | `subscription_scheduler.py`、`monitor_fetcher.py`、`change_pipeline.py` | 6 |

## 3. 必备知识点

- [ ] 能区分“专家画像”“Agent 角色”和“独立运行进程”，避免把 48 位专家说成 48 个自治执行体。
- [ ] 能从 `POST /api/tasks` 讲到 `report_ready/done`，说明直接调用和 SSE 事件的边界。
- [ ] 能解释 Evidence、Claim、Schema、Trace 四类对象分别解决什么问题。
- [ ] 能说明为什么 `analyze` 后先 `audit`，以及 collect 返工为什么必须再次 analyze。
- [ ] 能讲清规则质检、LLM 复审、advisory、blocking issue 和质量门禁的关系。
- [ ] 能解释多搜索提供商、网页抓取和舆情定向搜索的差异。
- [ ] 能说明 LLM 空输出、JSON 失败、搜索失败、部分目标失败分别如何处理。
- [ ] 能解释 ContextVar 如何让线程中的 LLM 调用继承任务、阶段和角色上下文。
- [ ] 能解释 SQLite WAL、写锁、原子租约、幂等键各自解决哪类并发问题。
- [ ] 能明确当前边界：没有 48 个独立 ReAct 循环、没有写后报告复审、知识图谱主要由报告数据在前端派生。

## 4. 推荐阅读（结合仓库）

| 主题 | 通用技术点 | 建议阅读位置 | 预计时间 | 读完能回答什么 |
|---|---|---|---:|---|
| 产品入口与范围澄清 | 联网候选发现、结构化问卷、计划重构 | `frontend/src/pages/HomePage.tsx` → `ClarifyPage.tsx` → `backend/app/core/orchestrator.py` 的 `create_task/_discover_scope/_plan_research` | 45 分钟 | 为什么不是固定问卷；候选竞品如何产生和兜底 |
| 流水线主链 | 异步生成器、阶段状态、并发写作 | `backend/app/core/orchestrator.py` 的 `run_pipeline()` | 90 分钟 | 七个节点实际以什么顺序运行；何时产生返工 |
| 专家画像与组队 | 角色画像、LLM 推荐、后端校验 | `backend/app/data/experts.json`、`docs/AGENTS.md`、`_dispatch_experts()` | 35 分钟 | 这个项目为什么只能谨慎称为角色化 Multi-Agent |
| 联网取证 | Provider 抽象、查询去重、正文抓取 | `backend/app/core/search.py`、`backend/app/core/fetcher.py`、`backend/app/core/sentiment.py` | 60 分钟 | 搜索服务和自抓网页分别做什么；失败怎样降级 |
| 分析与品牌对齐 | Evidence 摘要、Claim 生成、品牌硬校验 | `models.py`、`credibility.py`、`orchestrator.py` 的 `_analyze/_analyze_structured` | 70 分钟 | 如何避免 Trae 结论引用 Cursor 证据 |
| 质检返工 | 双重门禁、分类路由、最新反馈 | `backend/app/core/audit.py`、`tests/test_audit_gate.py` | 60 分钟 | collect/analyze/write 问题怎样进入不同后续路径 |
| 报告与人机协作 | 章节并行写作、证据卡、章节深化 | `_write_single_section/_assemble_report/refine_section`、`ReportPage.tsx` | 55 分钟 | 报告如何生成；章节深化为什么不等于二次联网调研 |
| Trace 与 SSE | 上下文传播、实时事件、持久化回放 | `trace.py`、`main.py`、`frontend/src/lib/api.ts`、`taskStore.ts` | 55 分钟 | 如何分析某阶段耗时和 Token；刷新为何影响前端而非任务逻辑 |
| 订阅监控 | 调度、租约、快照、Diff、安全抓取 | `subscription_scheduler.py`、`monitor_runner.py`、`monitor_fetcher.py`、`snapshot_store.py` | 90 分钟 | 如何防止重复执行、误报、SSRF 和快照失控 |
| 数据与测试 | SQLite 迁移、CRUD、Mock、故障路径 | `backend/app/core/db.py`、`tests/` | 60 分钟 | 哪些结果真正持久化；70 项测试覆盖哪些风险 |

## 5. 自学提醒

若某文件或原理看不懂，请继续追问 AI；本技能负责给学习路径与题目，不提供逐行讲解。

建议每读完一个主题都做一次“白板复述”：先画输入和产物，再说明成功路径、失败路径和当前边界。不要从函数名背诵项目，面试官更关注为什么拆层、模型不可靠时怎么办、数据如何追溯以及如何验证。

## 6. 项目技术定位

这是一个 **AI 应用开发为主，兼具后端、数据与前端可视化的交叉项目**。依据是：核心价值由 LLM 编排、联网检索、Evidence/Claim Schema 和质量门禁产生；FastAPI、SQLite、APScheduler 承担执行与持久化；React、Zustand、SSE、D3/ECharts 负责将运行过程和报告结果变成可操作界面。

它适合展示的能力不是“调用了大模型”，而是：如何把不稳定的模型能力放进一条有数据契约、失败降级、质量检查和过程观测的业务流程。

## 7. 核心原理解析

### 7.1 从自然语言到受约束的调研计划

**问题：** 用户输入通常缺少明确竞品、范围和维度，直接搜索会产生边界漂移。  
**机制：** 系统先联网获取候选线索，再由辅助模型归纳主体、赛道和竞品；固定问卷补齐市场、用户、时效和重点维度；正式启动时再将原始需求与答案重构为结构化计划。品牌合并时优先用户选择，并限制品牌和搜索角度数量。  
**项目落点：** `_discover_scope()`、`_clarify_questions()`、`_plan_research()`。模型负责语义归纳，后端负责字段、数量、顺序和兜底。

### 7.2 角色化编排而不是 48 个自治进程

**问题：** 调研涉及采集、分析、写作和质检，需要体现分工，但为每个角色创建独立进程和工具循环会显著增加成本与一致性问题。  
**机制：** 48 位专家被保存为知识画像，编排器让 LLM推荐成员，再由后端校验 ID、层级和数量。实际执行仍由一个中心流水线控制，角色主要决定提示视角、责任标签、前端展示和统计归属。  
**项目落点：** `experts.json`、`_dispatch_experts()`、`run_pipeline()`。Envelope 是编排器内部结构化消息，不是 Agent 间网络通信。

### 7.3 Evidence 约束的分析生成

**问题：** LLM 容易生成无来源结论，也可能把竞品 A 的证据错误用于主体 B。  
**机制：** 采集阶段生成正式 `evidence_id` 并记录品牌、域名、来源和摘要；分析 Prompt 中的每条摘要显式标注品牌；生成 Claim 后检查所有引用 ID 是否存在，并要求每个被提及品牌都得到同品牌证据支持。置信度再根据证据数量和独立域名计算。  
**项目落点：** `Evidence`、`Claim`、`_evidence_digest()`、`_claim_evidence_matches_brands()`、`make_claim()`。

### 7.4 规则与 LLM 共同组成质量门禁

**问题：** 纯规则看不懂语义，纯 LLM 又不稳定且难以约束。  
**机制：** 规则检查品牌域名覆盖、重点维度和 Schema 容器；LLM 复审识别核心结论无证据、逻辑冲突和关键遗漏。只有规则无 Issue、LLM verdict 为 pass 且阻断问题为空才放行。反馈按 collect、analyze、write 路由，collect 后强制重新 analyze，最多执行三轮分析返工。  
**项目落点：** `evaluate_quality()`、`llm_quality_review()`、`quality_gate_passed()`、`build_rework_envelopes()`。

### 7.5 SSE 与 Trace 构成可观测执行面

**问题：** 深度调研持续时间长，只返回最终报告会让用户无法判断是否卡住，也无法分析成本。  
**机制：** 后端异步生成器持续产生节点、思考、证据、进度、Trace 和报告就绪事件；前端使用 EventSource 消费并写入 Zustand。统一 LLM 层读取 ContextVar，自动记录模型、阶段、耗时和 Token；搜索抓取用手工 Span 补齐非 LLM 动作。任务结束后 Trace 写入 SQLite。  
**项目落点：** `run_pipeline()`、`trace.py`、`stream_task()`、`openTaskStream()`、`taskStore.ingest()`。

### 7.6 从一次调研扩展到持续监控

**问题：** 一次性报告会随竞品更新而过期，重复全量调研成本高。  
**机制：** 订阅调度器周期领取到期任务，对固定页面做条件请求、正文归一化、Hash 和确定性 Diff；轻微变化跳过 LLM，实质变化再语义分类。SQLite 原子租约防止重复领取，指数退避处理失败，变化可转成带正式 Evidence ID 的新调研任务。  
**项目落点：** `subscription_scheduler.py`、`monitor_fetcher.py`、`change_detector.py`、`monitor_research.py`。

## 8. 关键设计决策

| 决策 | 备选方案 | 当前取舍 | 主要风险 | 验证方式 |
|---|---|---|---|---|
| 中心流水线 + 专家画像 | 每个专家独立 ReAct Agent | 控制成本和执行顺序，便于统一 Trace 与返工 | 自主协商和局部工具决策较弱 | 检查每阶段实际 LLM/工具调用和角色归属 |
| 搜索服务 + 自行抓正文 | 自建全网搜索引擎 | 用第三方检索发现 URL，自行抓正文与取证 | Provider 配额、网络和结果偏差 | 记录搜索成功率、有效证据率和 Provider 错误 |
| 规则 + LLM 双重质检 | 单一规则或单一模型 | 可计算条件确定化，语义问题交给模型 | 复审上下文有限，可能漏判或漂移 | 固定样本回放，比较轮次 verdict 和人工标注 |
| 写作前审计 | 写完后统一审计 | 先修正证据和分析，减少无效写作 | 成稿表达没有独立复审 | 增加 `report_audit` 实验并比较返工成本，当前待测 |
| SQLite + JSON 报告 | PostgreSQL + 全范式化 | 本地部署简单，报告版本读取方便 | 多进程高写入、复杂查询和长期扩容受限 | 并发压测、库体积和查询延迟，当前待测 |
| SSE 单向事件流 | WebSocket 或轮询 | 适合服务端持续推送，前端实现简单 | 断线恢复和重复执行语义需额外设计 | 人为刷新、断网、重连，核对任务和事件状态 |
| 确定性 Diff 后再用 LLM | 每次快照都交给模型 | 降低 Token，保留可复核差异 | 归一化或阈值不当造成漏报 | 构造价格、功能、页脚、时间戳变化测试集 |
| 进程内调度器 + SQLite 租约 | 外部队列和独立 Worker | P0 部署轻量，具备基本防重恢复 | 必须常驻进程；多实例扩展能力有限 | 重启、租约过期、多 Worker 并发测试 |

## 9. 量化与验证（含待测，建议）

当前仓库可核验的数据是：专家名册共 48 个画像；`tests/` 当前包含 70 个单元/异步测试用例；监控模块覆盖调度、租约、SSRF、Diff、快照和研究衔接。此前验证记录显示前端构建和静态检查通过，但这些结果不等同于线上业务效果。

建议后续建立以下基线：

| 指标 | 测量方法 | 当前状态 |
|---|---|---|
| 端到端任务成功率 | 固定 20 组主题，在相同模式和 Provider 下重复执行 | 待测 |
| Evidence 有效率 | 人工判断搜索结果中真正支持调研对象的比例 | 待测 |
| Claim—Evidence 支持准确率 | 抽样核对 Claim 与引用证据是否同品牌且语义支持 | 品牌硬校验有测试，语义准确率待测 |
| 质量门禁一致性 | 同一冻结输入重复复审，比较 verdict 和问题分类 | 待测 |
| 返工有效率 | 比较每轮阻断问题数量、规则指标和新增有效证据 | 已记录轮次，聚合指标待测 |
| 搜索和 LLM 成本分布 | 按 Trace 阶段汇总请求数、Token、耗时和失败 | 数据已记录，报表待补 |
| SSE 断线恢复 | 运行中刷新、断网和重连，核对是否重复执行 | 待测 |
| 监控误报/漏报率 | 使用带已知变化的网页样本集回放 Diff 与分类 | 单元测试已有，真实网页集待测 |
| 调度可靠性 | 观察两个以上周期并注入重启、超时和部分失败 | 单元测试已有，长时间运行待测 |
| SQLite 承载边界 | 持续写入报告、Trace 和快照，测体积与 P95 查询时间 | 待测 |

