# 需求 → 机制 → 测试对照

README 里不写这些表，是为了让它读起来不像机器生成的需求文档。
但每一条需求落在哪个机制、由哪个用例守着，值得留档。

## V0.6：用词与收束

| 需求 | 机制 | 用例 |
| --- | --- | --- |
| 用词可预测 | `word_ttr`（滑动窗口用词多样性）、`word_concentration`（最高频搭配占比）、`verb_variety` → `PREDICTABLE_WORDING` / `REPETITIVE_VOCABULARY` / `VERB_MONOTONE` | `test_predictable_wording_and_verb_monotone_fire` |
| 意外用词，且不追求困惑度 | `novel_char_ratio`：本章有多少字是本书此前没用过的，超过 35% 报 `WORD_DRIFT` 并列出具体是哪些字；换词提示写的是「更具体的说法」 | `test_word_drift_flags_flowery_wording_only_with_history`、`test_live_rewrite_cuts_summary_and_elevation` |
| 删总结 | `summary_per_1k` → `SUMMARY_ENDING`，证据是那一句 | `test_summary_and_elevation_rules_fire_with_evidence` |
| 减少升华类句子 | `elevation_per_1k` → `ELEVATION_DENSE` | 同上 |
| 换抽象为具体 | `abstract_unsupported_ratio`：抽象词句里有没有动作/对白/器物/数字 → `ABSTRACT_SUBSTITUTION` | `test_abstract_substitution_rule_fires`、`test_concrete_writing_is_not_flagged` |
| 过渡不必过于平滑 | `transition_per_1k` → `SMOOTH_TRANSITION` | `test_smooth_transition_rule_fires` |
| 避免桥段同质化 | 桥段短语跨章统计，≥4 个既往章节用过再出现 → `BRIDGE_REPEAT`，点名在哪几章用过 | `test_bridge_repeat_across_chapters` |
| 旧基线不能静默失效 | 基线缺当前指标时报 `BASELINE_STALE`，漂移报告与看板同步提示 | `test_stale_baseline_is_surfaced_not_silently_ignored`、`test_fresh_baseline_is_not_marked_stale` |
| 同一句不要报两遍 | 规则冲突覆盖同句的「待确认」条目（按去标点后比对） | `test_rule_conflict_shadows_the_unverified_twin` |

## V0.5：质量下限

| 需求 | 机制 | 用例 |
| --- | --- | --- |
| 减小 AI 幻觉影响 | 声称核对：正文 → 逐条断言 → 与 Canon/世界观规则比对，产出一致/冲突/待确认；模型只拆句、必须附逐字片段；查无此设定一律待确认 | `test_claim_verification_catches_planted_setting_conflict`、live `test_live_claim_verification_*` |
| 文风选定后不该随意变化 | 锁定基线后按 0.5 倍容差比对，越界报 `STYLE_DRIFT_LOCKED`；生成前把要求写进提示 | `test_locking_narrows_the_window`、`test_drift_report_lists_chapters`、live `test_live_write_follows_locked_style_and_world_rules` |
| 世界观设定永不冲突 | 结构化判定四类规则：死者复生、唯一持有、能力门槛（按境界阶梯）、不可变特质 | `test_no_resurrection_rule_*`、`test_capability_gate_uses_realm_from_canon`、`test_immutable_trait_rule_*`、`test_possession_unique_rule_*` |
| 避免无意义的环境与心理描写 | `filler_paragraph_ratio` + `advancement_per_1k` → `FILLER_HEAVY` / `LOW_ADVANCEMENT`，指出是哪一段 | `test_filler_heavy_flags_pure_description_with_evidence` |
| 避免名词解释与过度解读 | `exposition_per_1k` 只统计没有推进的解释句 → `EXPOSITION_DUMP` | `test_exposition_dump_flags_definition_sentences` |
| 避免标点分布过于均匀 | `punctuation_entropy` → `PUNCT_FLAT` | `test_uniform_punctuation_is_flagged` |
| 避免固定句式循环 | `pattern_loop_ratio` + `pattern_top` → `PATTERN_LOOP`；跨章再查 `RESTATING_KNOWN` | `test_pattern_loop_is_flagged_with_skeletons`、`test_restating_known_flags_copied_paragraph` |

## V0.4：把人的部分留给作者

| 人的特点 | 机制 | 用例 |
| --- | --- | --- |
| 情感深度 | 情感引导只提问（3~5 个具体问题），作者的回答存成 `origin=PROMPT` 的碎片 | `test_emotion_prompts_*` |
| 语言个性 | 声音画像（特征词/标点/句长/碎句）+ 声音保留分 + `VOICE_WEAK` / `OVER_REGULAR` / `VOICE_DRIFT`；改稿磨平作者就回退 | `test_revision_loop_reverts_when_author_voice_is_worn_away`、live `test_live_rewrite_preserves_author_voice` |
| 意外与原创 | 碎片是创作源；成文必须给出可双向校验的对应表 | `test_realizer_*`、`test_live_realize_keeps_author_fragments` |

## V0.3：文风与全局约束

| 需求 | 机制 | 用例 |
| --- | --- | --- |
| 把 AI 味拆成数字 | 24 项确定性指标 + 以本书认可章节为基线 | `test_v3_style.py` |
| 改稿要可审计 | RevisionLoop 每轮记分与指标轨迹，无进展即停、回退到最好 | `test_v3_revision.py` |
| 单章审校看不到的问题 | 7 类全局不变量 | `test_v3_invariants.py` |
| 约定不能忘 | 承诺账本按故事时间推算到期 | `test_v3_revision.py` |

## V0.1 / V0.2：需求与长期连载基础

| 需求 | 机制 | 用例 |
| --- | --- | --- |
| AI 不得自动改 CANON | `propose_canon_fact` 硬编码 PROPOSED，只有作者确认才升级 | `test_extraction.py`、`test_workflow.py` |
| 每个问题都要有证据 | Pydantic 层强制 evidence 非空 + `source_chapter`；无证据候选进 `dropped_issues` | `test_continuity.py` |
| 不能把 PROPOSED 当 CANON | 时点视图与检索都排除未确认条目 | `test_memory_search.py`、`test_v2_state.py` |
| 查不到就问 UNKNOWN | 检索无证据时回答必须声明 UNKNOWN 并压低置信度 | `test_memory_search.py` |
| 十万字以上要能查前文 | 混合检索（关键词 + 向量 RRF）、时点视图、分页 | `test_v2_retrieval.py`、`test_v2_state.py` |
| 长篇要有规划 | PlannerAgent + 伏笔欠账调度 | `test_v2_planner.py` |
| 要能发现问题 | 全量扫描（rules / full）+ 总览看板 | `test_v2_sweep.py` |
| 换模型不改代码 | AIProvider 抽象 + 提供者注册表 | `test_v2_agent_tools.py` |
| 老库要能升 | 幂等增量迁移 | `test_v2_agent_tools.py` |
