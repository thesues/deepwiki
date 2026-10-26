---
name: evidence-review
description: 原生 Kanban 研究 Swarm 的阶段证据聚合。主 Agent 只汇总和协调，不亲自解题或执行验证；区分公开候选、同行复现/反例和形式证明，引用原生 stage/comment/task 来源。
metadata:
  hermes:
    requires_tools:
      - research_publications
      - kanban_show
      - kanban_comment
---

# 公开阶段成果与同行报告

**主 Agent 只聚合和协调，不亲自解决问题或执行独立验证。** 不由主 Agent 证明、做实验、找反例或自报 PASS。

1. 用 `research_publications` 按 cursor 阅读共同 root 的公开阶段、验证报告与发布说明。默认 Kanban 另可读取已完成任务的公开交付物；不读取私有 workspace、日志或未发布笔记。
   每条成果标明发布者 Agent/模型；每条验证报告标明验证者、目标 stage、原发布者、结论和实际证据。验证报告本身也是公开成果。
2. 逐项聚合 claim、实际 evidence、复现方法、限制、引用来源。阶段引理、部分实验或反例候选可以发布，不需要完整解题或退出进程。
3. 发布后其他 research worker 可以阅读、独立验证和使用公开成果，同时继续各自探索。验证不是全体 worker 的等待屏障。
4. 自己不能评审自己的成果；统计只使用其他执行身份的最新报告，INCONCLUSIVE 不算 PASS。报告矛盾时列出冲突并协调追加复验，不靠主 Agent 直觉裁定。
5. `PUBLISHED` 为候选公开；`DISPUTED` 存在同行 FAIL；`PEER_SUPPORTED` 为至少两位独立同行报告 PASS 且无 FAIL。后两者仍是模型报告，不是形式数学证书。
6. 本版本没有自动 Lean checker/可信形式验证注册表。原生 Kanban 卡 done 或 goal-loop judge 接受，只表示任务协议完成，不能声称定理已被机器证明。需要形式证明时协调 researcher 提供真实检查日志与锁定环境，并让其他人复验。
7. 可以把未验证公开成果当作研究线索，但必须引用 stage ID 并保留限制；不能悄悄修改原问题、基线，或冒充完全盲发现。
8. 观察新的阶段或复验 publish 后主动汇报，按“谁发布了什么、谁复验了哪条、支持/反驳证据、可靠性与缺口、后续验证安排”组织。没有公开成果就报告进度与阻塞，不生成自己的答案填空。摘要被标为 excerpted 时明确材料未完整读取，不把模型 PASS 报告当机器证明。
