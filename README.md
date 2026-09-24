# NovelOS V0.6

**终局目标**：能生成一本 100 万字、逻辑自洽、没有「AI 味」的中文网络小说。

V0.6 处理的是**「像不像人写的」里最容易量化的那一层：用词与收束**。
AI 的毛病不是词不达意，而是**每次都挑最安全、最合理、最顺手的那个词**，于是文本「可预测」；
收尾总要总结一遍、升华成道理；抽象词替代画面；场景之间用连接词顺滑粘住；同一个桥段反复用。

**这里有个容易走偏的地方，本版明确不这么做**：不追求困惑度。堆生僻字、硬造怪词确实能提高
困惑度，但那是另一种失真。所以用词的意外度是**区间**指标 —— 太平（用同一批词）和太飘
（满篇本书从没用过的字）都要拦，换词的方向是**更具体**，不是**更罕见**。

## V0.6 的六条要求 → 落地机制

| 要求 | 落地机制 | 怎么验证 |
| --- | --- | --- |
| 用词「可预测」 | `word_ttr`（滑动窗口用词多样性）、`word_concentration`（最高频搭配占比）、`verb_variety`（动词多样性）→ 规则 `PREDICTABLE_WORDING` / `REPETITIVE_VOCABULARY` / `VERB_MONOTONE` | `test_predictable_wording_and_verb_monotone_fire` |
| 意外用词、但**不追求困惑度** | 护栏指标 `novel_char_ratio`：本章有多少字是**本书此前从未用过**的 → 超过 35% 报 `WORD_DRIFT`（并列出具体是哪些字），换词提示写的是「换更具体的说法」而不是「换生僻词」 | `test_word_drift_flags_flowery_wording_only_with_history`、live `test_live_rewrite_cuts_*` 里的 `novel_char_ratio ≤ 上限` |
| 删总结 | `summary_per_1k`（「这一切/从那以后/他终于明白…」）→ `SUMMARY_ENDING`，证据是具体那一句 | `test_summary_and_elevation_rules_fire_with_evidence` |
| 减少升华类句子 | `elevation_per_1k`（「所谓/不过是/人这一生…」）→ `ELEVATION_DENSE` | 同上 |
| 换抽象为具体 | `abstract_unsupported_ratio`：抽象词句里有没有动作／对白／器物／数字 → `ABSTRACT_SUBSTITUTION` | `test_abstract_substitution_rule_fires`、`test_concrete_writing_is_not_flagged` |
| 过渡不必过于平滑 | `transition_per_1k`（「于是/随后/第二天/不知不觉…」）→ `SMOOTH_TRANSITION` | `test_smooth_transition_rule_fires` |
| 避免桥段同质化 | `BRIDGE_PATTERNS` 跨章统计：某个收尾桥段在**≥4 个既往章节**里用过，本章又用 → `BRIDGE_REPEAT`，点名它在第几章用过 | `test_bridge_repeat_across_chapters` |

| 能力 | V0.5 | V0.6 |
| --- | --- | --- |
| 用词 | 19 + 6 项指标（节奏、推进、标点、句式） | 再加 8 项**用词与收束**指标；「飘」与「平」两侧都有闸门 |
| 改稿 | 声音护栏（磨平作者就回退） | 改稿要求里明确「换成更具体的词，而不是更生僻的词」「删总结、切开过渡」；篇幅闸门改为不对称（暴跌拒绝、增长只提醒） |
| 提示 | 世界观与锁定文风进提示 | 写作提示新增第 8 条：不总结、不升华、抽象要有落点、该跳就跳、不重复桥段；规划提示新增「相邻章不要同一套路」 |
| 界面 | 质量面板：断言核对、文风锁定 | 指标表补齐 V0.5/V0.6 全部指标的中文名；括号内的指标名去掉单位后缀（不再出现嵌套括号） |
| 基线 | 采样章节算指标分布 | **旧基线缺指标时显式提示**（`BASELINE_STALE` + 看板提示 + 重建入口），不再静默失效 |
| 断言核对 | 同一句可能既报冲突又报待确认 | 规则冲突**覆盖同句的「待确认」条目**，作者不必看两遍 |

V0.5 的能力仍然有效（幻觉兜底、文风锁定、结构化世界观判定、推进与节奏护栏），
V0.1—V0.4 的安全原则一条没改：AI 写不到 CANON；每条问题必须带来源；
模型意见没有原文依据就被丢弃；改稿磨平作者就回退；碎片是创作源，对应关系可核对。

## V0.5 的核心设计

V0.5 处理的是**质量下限**：让 AI 的帮助尽可能少地引入幻觉，让已经定下的文风与世界观
在几十万字的跨度上不漂、不打架，并且把「写了一大堆但什么都没发生」的稿子挡在门外。

### V0.5 的七条要求 → 落地机制

| 要求 | 落地机制 | 怎么验证（都进了测试） |
| --- | --- | --- |
| 尽量减小 AI 幻觉的影响 | **声称核对**（ClaimVerifier）：任意正文 → 逐条断言 → 与 Canon／世界观规则比对，产出 `SUPPORTED / UNVERIFIED / CONFLICT`；模型只负责拆句，**每条都要附正文里逐字存在的片段**，判定在 Python 里做；查无此设定一律 `UNVERIFIED`，绝不自动进 Canon | `tests/test_v5_quality.py::test_claim_*`、live `test_live_claim_verification_*` |
| 前后文风保持一致，选定后不该随意变化 | **文风锁定**：把认可的基线锁起来（`style/baseline/{id}/lock`），此后新章按**收紧一半**的窗口比对，越界报 `STYLE_DRIFT_LOCKED`；写作前把锁定的节奏、推进密度、标点与句式要求写进提示（`style_lock`） | `test_locking_narrows_the_window`、`test_drift_report_lists_chapters`、live `test_live_write_follows_locked_style_and_world_rules` |
| 世界观设定牢记且永不冲突 | 生成前注入世界观规则与 Canon（`WRITE_SYSTEM` 规则 6）；生成后用**结构化规则判定**：死者复生、本命灵剑唯一、能力门槛（按境界阶梯比）、不可变特质（灵根被改写） | `test_no_resurrection_rule_*`、`test_capability_gate_uses_realm_from_canon`、`test_immutable_trait_rule_*`、`test_possession_unique_rule_*` |
| 避免无意义的环境与心理描写 | `filler_paragraph_ratio`（≥30 字、无动作、无对白的长段占比）+ `advancement_per_1k`（每千字剧情动作数）→ 规则 `FILLER_HEAVY` / `LOW_ADVANCEMENT`，并给出具体是哪一段 | `test_filler_heavy_flags_pure_description_with_evidence` |
| 避免大量名词解释等过度解读 | `exposition_per_1k`：只统计**没有推进的**解释句（所谓/指的是/分为/换言之…）→ `EXPOSITION_DUMP`，证据是具体那一句 | `test_exposition_dump_flags_definition_sentences` |
| 避免标点分布过于均匀 | `punctuation_entropy`：标点类型分布的归一化香农熵 → `PUNCT_FLAT`（连带报出强标点只有几处） | `test_uniform_punctuation_is_flagged` |
| 避免陷入固定句式循环 | `pattern_loop_ratio` + `pattern_top`（句首三字 + 收尾标点 + 长度档的骨架重复率）→ `PATTERN_LOOP`；跨章还查 `RESTATING_KNOWN`（本章段落与前文重合度） | `test_pattern_loop_is_flagged_with_skeletons`、`test_restating_known_flags_copied_paragraph` |

## V0.4 的核心设计（仍然是后来的基础）

V0.4 修正了一件比功能更重要的事：**系统的定位**。它不是「让 AI 写小说」，
而是**把作者的想法碎片——奇思妙想、天马行空、对世界的认识与思考——写成有文学气息的文章**。

人类写作有五样东西是模型给不了的，系统必须把它们留给作者：

| 人类写作的特点 | 系统该做什么 | 系统不该做什么 |
| --- | --- | --- |
| **情感深度**（真实经历里的复杂情绪） | 用**提问**把细节与情绪逼出来（`fragments/prompts`），评审时指出「情绪是宣布的，不是演出来的」 | 不生成情绪，不替作者决定「这里该难过」 |
| **语言个性**（风格与不完美） | 建立**作者声音画像**（特征词、标点习惯、断句方式），改稿有**声音护栏**：磨平个性就回退 | 不把作者的用词、破折号、单句成段「标准化」 |
| **长线一致性** | 时点视图 + 7 类全局不变量 + 承诺账本（已有的 V0.2/V0.3 能力） | 不靠记忆硬撑，一切以入库设定为准 |
| **意外与原创** | 碎片是**唯一创作源**；成文必须给出「哪条碎片 → 哪段正文」的**对应表**，作者可逐条核对 | 不从分布里采样，不另起炉灶编情节 |
| **创作动机**（个人欲望与风险） | 意图显性化：每章声明意图与目标章号，评审只对照意图查执行 | 不建议「写什么更爽」，不替作者承担取舍 |

| 能力 | V0.3 | V0.4 |
| --- | --- | --- |
| 创作入口 | 给 AI 一个目标，让它写 | **记下想法碎片**（类型/意图/标签/相关人物/目标章号/优先级），碎片可检索、可安排、可回填 |
| 成文 | ChapterWriter 从 Canon 生成 | **FragmentRealizer**：碎片 → 文学化正文，**逐条对应可核对**，原话可原样保留 |
| 不发明设定 | 靠一致性检查事后发现 | 成文当场用确定性规则扫出**新的设定性陈述**并报给作者 |
| 文风 | 往规范推（降套话、提节奏） | 增加**反向约束**：声音保留分 + `VOICE_WEAK` / `OVER_REGULAR`，改稿降低作者特征就**回退** |
| 情感 | 无 | **情感引导**：系统只提问（3~5 个具体问题），作者的回答变回碎片 |
| 界面 | 9 个标签 | 新增「碎片」标签（快速录入 / 批量粘贴 / 引导问答 / 安排到章 / 成文对应表） |

### 1. 想法碎片是一等公民

`fragments` 表存的是**作者的原始素材**：正文允许不完整、口语化、跳跃；
`intent` 记「他想让它起什么作用」；`target_chapter` 记「他打算放进第几章」。
碎片同时进全文与向量索引，所以：写作时会**自动带上相关碎片**、
规划时会看到**还没安排的碎片**（并按要求安排进某章的 must_include）、
问答检索也能召回「作者当初的想法」。

状态流转：`INBOX`（刚记下）→ `PLACED`（已安排）→ `REALIZED`（已成文）；
成文后系统回填 `realized_excerpt` 与 `treatment`，作者能一眼看出哪条碎片进了哪一章。

### 2. 对应表：把「作者的原话」写进正文

`FragmentRealizer` 的输出契约里，每条碎片都必须有对应的 passage：

```json
{"fragment_id": "frg_xxx", "prose": "……变成的正文……", "uses_quote": "碎片里的原话", "treatment": "QUOTED"}
```

代码会**双向校验**：`uses_quote` 必须真在碎片原文里、`prose` 必须真在成文里，
对不上的对应关系直接丢弃并写进 warnings（宁可少一条，也不要编造的对应关系）。
用不上的碎片必须写进 `undeveloped` 并给原因——系统如实报告它没做的事。

同一份输出还会用确定性属性规则反向扫一遍：成文里凡出现 Canon 没有的
「主语 + 谓语 + 宾语」陈述（例如「林默的佩剑是玄铁重剑」），一律报为
`invented_claims`，由作者决定是接受为新设定，还是改掉。

### 3. 声音保护：不能让改稿把人磨平

V0.3 的文风引擎在**推向规范**，这对「AI 味」是对的，对「语言个性」是危险的。
V0.4 增加了一整套反向指标与兜底：

| 维度 | 内容 |
| --- | --- |
| 作者指纹 | 从作者自己的文字里提**特征词**（句子内部滑动 2~3 字片段，出现在多篇里才算习惯）、**标点分布**、句长习惯、碎片句比例 |
| 声音保留分 | 0~100，四个分项：特征词覆盖率 40%、标点相似度 20%、句长相似度 20%、碎片句相似度 20% |
| 规则 | `VOICE_WEAK`（不像你平时的写法）、`OVER_REGULAR`（标点单一且没有短句成段，像被磨平过）、`VOICE_DRIFT`（标点或句长习惯偏离） |
| 兜底 | `RevisionLoop` 每轮比较声音保留分：下滑超过 8 分 → **回退到上一稿**并写明原因；`STYLE_REVISE_SYSTEM` 明确要求不得改掉作者的用词与断句 |
| 参照系 | 「过于规整」的判据用**作者自己的标点习惯**做下限，而不是通用规范 |

实测（用三段风格很个人的文字建画像）：作者自己的文字得 91.4 分，
换成一段套话流水句只有 39.8 分，并同时报出 `VOICE_WEAK` / `OVER_REGULAR` / `VOICE_DRIFT`。

真实模型上也验证过：用四条「作者碎片」建画像后让 DeepSeek 成文，
界面上同时给出**声音保留分 76.6**（命中的特征词是他惯用的「后来才」「才知道」「把刀」）
与**文风得分 86.0**，3/3 条碎片全部展开、对应表逐条可核对（QUOTED 原话保留）。
另有一次 DeepSeek 改稿把作者声音从 53.3 压到 25.4，被护栏直接拦下并保留原稿 —— 这正是它存在的意义。

### 4. 情感引导：只提问，不代写

`POST /api/novels/{id}/fragments/prompts` 让模型以「写作教练」的身份提问，例如
「他攥紧的那只手里有什么？」「如果这一幕失败，他失去的具体是什么？」。
系统的提示词里写死了三条：只提问、不问空问题、不给答案。
作者的回答以 `origin=PROMPT` + `prompted_by=问题原文` 存成新碎片，
于是**情绪仍然是作者的，系统只负责把它问出来、留下来**。

| 能力 | V0.2 | V0.3 |
| --- | --- | --- |
| 文风 | 无 | **文风度量引擎**：19 项可计算指标 + 以作者自己认可的章节为基线 + 规则层问题定位到原文片段 |
| 读感评审 | 无 | **StyleCritic**：规则层 + 模型层（视角漂移、角色腔调、信息复述、情绪直说、本章无推进），模型意见必须能逐字对上原文，否则丢弃 |
| 写作 | 一次成稿 | **RevisionLoop**：写作 → 评审 → 改稿 → 复评，指标轨迹全留痕，**指标没改善就回退并停止** |
| 一致性 | 单章 vs Canon 快照 | 再加上**全局不变量**：时间单调、境界不回退、持有物唯一、同一天不跨地、知情只增不减、人物长期失踪 |
| 承诺 | 无 | **承诺账本**：抽取「三日后听雨楼见」这类约定，按故事时间推算到期，越期报警（含越界章证据） |
| 界面 | 8 个标签 | 「质量」标签（文风/不变量/承诺）、章节「文风」评审、写作「自动修订」轨迹 |

安全原则不变：AI 写不到 CANON；每条问题必须带来源；模型意见没有原文依据就被丢弃。

## V0.3 的关键设计

### 1. 把「AI 味」拆成数字

`services/style_service.py` 里全部指标都是确定性的（正则 + 统计），因此可测、可回归、可对比：

- **节奏类**：句长变异（burstiness = 句长标准差/均值）、短句占比、长句占比、段长离散度
- **对话类**：对白占比、对白段落占比
- **套话类**：套话密度（每千字）、套话种类比例、章内自重复率、主语重复率、句首多样性
- **空泛类**：抽象词密度、解释性叙述密度（telling）
- **网文节奏**：冲突信号密度、章末钩子分（疑问/转折/截断/对白收尾）

判定**不跟通用标准比，而跟这本书自己的分布比**：作者用自己认可的章节建立基线（`POST /api/novels/{id}/style/baseline`），
之后每章的阈值 = 基线窗口（均值 ± max(1.5σ, 30% 均值, 该指标最小容差)）∩ 绝对上下限。
容差下限保证基线离散度大时窗口不会宽到形同虚设。

实测（种子小说的 20 章作基线）：模型写的「套话流水句」样段得分 67，
套话密度 87.63/千字（本书基线 0.043）、burstiness 0.286（基线 0.586）、对白占比 0（基线 0.236）、
抽象词密度 20.62（基线 0.46）—— 每一项都指向具体的修改动作。

### 2. 改稿闭环为什么可信

`RevisionLoop` 的每一轮都记 `RevisionRound`（轮次、阶段、分数、字数、文风 codes、一致性 codes），
并在结束时给出 `metric_deltas`（每个指标 before → after → 是否改善）。停止条件写得很保守：

- 达到目标分且无一致性错误 → 接受；
- **改稿没有带来任何变化** → 停止（不空转）；
- **分数没有提升** → 回退到最好的一稿并停止；
- 无进展/未达标都会明确写在 warnings 里，交给作者决定。

离线提供者的改稿只做两件确定的事（删填充副词、断超长句），其余问题如实报告为「未处理」——
不假装能改。

**实测（DeepSeek，对一段典型「AI 味」样段）：**

| 指标 | 改稿前 | 改稿后 | 判定 |
| --- | --- | --- | --- |
| 套话密度（每千字） | 58.02 | **0.00** | 改善 |
| 抽象词密度（每千字） | 13.65 | **0.00** | 改善 |
| 长句占比 | 0.222 | 0.000 | 改善 |
| 章内自重复率 | 0.0115 | 0.0000 | 改善 |
| 对白占比 | 0.000 | 0.287 | 从「全旁白」变成有对白 |
| 短句占比 | 0.000 | 0.543 | 句式有了长短差 |

改后的文字从「情绪摘要」变成了可演的一场戏（「苏婉肩一僵，后退半步，鞋跟磕上门槛。」），
模型给的读感意见也确实命中要害（视角漂移、情绪靠旁白直说、对白被虚化、场景无实物、本章零推进）。
**注意一个指标陷阱**：`burstiness` 在这次改稿里从 0.662 降到 0.462 ——
把超长句拆成短句本身就会降低它，所以判断「节奏有没有变差」不能只看 burstiness，
还要看短句占比与整体分布（因此测试里的判据是「AI 味指标不得变差 + burstiness 不低于下限」）。

### 3. 全局不变量（单章审校看不到的问题）

| code | 含义 | 级别 |
| --- | --- | --- |
| `TIME_INVERSION` | 故事时间回退（第 20 章早于第 15 章） | error |
| `REALM_REGRESSION` | 修为/境界倒退（炼气九层 → 炼气五层） | error |
| `EXCLUSIVE_CONFLICT` | 同一时点持有两件不同的佩剑/法宝 | error |
| `KNOWLEDGE_SHRINK` | 设定更新后知情者反而变少 | warning |
| `LOCATION_JUMP` | 同一天出现在两地 | warning |
| `COMMITMENT_OVERDUE` | 约定到期未兑现 | warning |
| `CHARACTER_UNUSED` / `CHARACTER_DORMANT` | 人物建档未出场 / 长期未再登场 | warning |

报告会落库（`invariant_reports`），并且跟着全量扫描一起跑；看板与「质量」标签都能看。

### 4. 承诺账本

抽取带期限的约定（「三日内到驿站」「十日内回山」「今晚子时动手」…）→
用来源章的故事时间 + 期限天数算出**到期故事时间**（`天启三年四月初二` + 三日内 = `天启三年四月初五`）→
只要有后一章的故事时间越过了它而承诺仍未兑现，就报 `COMMITMENT_OVERDUE`，证据含来源章与越界章。

真实例子（就是种子小说本身）：第 4 章赵铁山交代「信三日内到驿站，取到就回山」，
第 7 章已经四月初六，而全书没有任何一章去取那封信 —— 系统把它标成逾期，作者可以选择补写或标记放弃。

到期当天算「今天到期」，越过了才算逾期；`POST /api/commitments/{id}/fulfill|abandon` 由作者收口。

## V0.1/V0.2/V0.3 的能力（仍然有效）

- **V0.1**：Canon 状态机（AI 只能写 PROPOSED）、带证据的一致性审校、AITool、MemorySearch、ChapterWriter 的章节完成工作流。
- **V0.2**：混合检索（关键词 + 向量）、时点视图、章节规划、全量扫描与看板、多模型、agent 模式工具调用、幂等迁移。
- **V0.3**：文风度量引擎（19 项指标 + 作者基线）、StyleCritic（规则层 + 模型读感）、RevisionLoop（写作→评审→改稿，无进展即停）、全局不变量、承诺账本、质量面板。

下面的设计各节与这些能力一一对应。

---

## 一、目录结构

```
D:\Projects\NovelOS\
├─ backend\
│  ├─ app\
│  │  ├─ main.py               FastAPI 入口（lifespan 初始化目录、建表与迁移）
│  │  ├─ config.py             配置（.env / 环境变量，含外部模型与 embedding 配置）
│  │  ├─ database.py           引擎与会话
│  │  ├─ migrations.py         幂等增量迁移（补列、补索引、回填历史数据）
│  │  ├─ models.py             SQLite Schema（SQLAlchemy 2.0）
│  │  ├─ schemas.py            Pydantic 出入参 + 各 Agent 的结构化输出契约
│  │  ├─ timeutil.py           中文字数统计、中文数字与故事时间解析
│  │  ├─ ai\
│  │  │  ├─ base.py            AIProvider 抽象（含工具调用接口）
│  │  │  ├─ chat.py            通用 OpenAI 兼容提供者（DeepSeek/Grok/OpenAI/本地共用）
│  │  │  ├─ deepseek.py        DeepSeek Flash 预设
│  │  │  ├─ offline.py         离线规则提供者（无密钥/测试用，含规划与工具调用脚本）
│  │  │  ├─ embeddings.py      EmbeddingProvider：本地 n-gram 向量 / 远程 embedding
│  │  │  ├─ factory.py         提供者注册表（内置 + NOVELOS_PROVIDERS 配置）
│  │  │  ├─ prompts.py         提示词（内含安全原则）
│  │  │  ├─ json_utils.py      模型输出的 JSON 容错解析
│  │  │  ├─ tools.py           AITool 工具箱（10 个工具 + 参数 Schema）
│  │  │  └─ agents\            抽取 / 审校 / 记忆检索 / 写作 / 规划 / 工具调用循环
│  │  ├─ services\             章节、检索、向量、抽取、审校、写作、规划、伏笔、
│  │  │                       扫描看板、流程编排、种子数据
│  │  └─ routers\              novels / chapters / entities / insight / plans / ai
│  ├─ seed\
│  │  ├─ story_bible.md        测试小说《剑起青云》设定圣经（含 3 处故意矛盾的定义）
│  │  ├─ seed_entities.json    人物/事件/时间线/Canon/伏笔/世界规则
│  │  └─ novel\ch01.md…ch20.md 20 章正文
│  ├─ tests\                   pytest：230 个用例 + 19 个真实模型联调用例
│  ├─ requirements.txt / requirements-dev.txt / requirements-vector.txt
│  └─ pytest.ini
├─ frontend\                   Next.js 15 + React 19 + TypeScript
│  ├─ app\page.tsx             工作台：左章节 / 中正文 / 右 AI / 底部设定库与看板
│  ├─ components\              ChapterPanel / EditorPanel / AIPanel / KnowledgePanel /
│  │                           DashboardPanel（总览）/ PlanPanel（规划）
│  └─ lib\api.ts, lib\types.ts
└─ data\                       SQLite 库 + data/novels/<书名>/chNNN.md
```

## 二、启动

### 1. 后端（FastAPI + SQLite）

```powershell
cd D:\Projects\NovelOS\backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt      # 国内可用 -i https://mirrors.aliyun.com/pypi/simple/
Copy-Item .env.example .env                                          # 按需填 DEEPSEEK_API_KEY
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

- 接口文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/health （返回当前检索引擎与各 AI 提供者可用性）

**没配 `DEEPSEEK_API_KEY` 也能跑**：`resolve_provider` 会自动降级到离线规则提供者，并把这个降级作为 warning 返回，
界面上能看到提示，不会静默失败。

### 2. 前端（Next.js）

```powershell
cd D:\Projects\NovelOS\frontend
npm install
npm run dev          # http://127.0.0.1:3000
```

前端所有请求走相对路径 `/api/*`，由 `next.config.mjs` 的 rewrite 转发到 `http://127.0.0.1:8000`。
开发服务器的代理默认 30 秒就断（前端只看到 `500`，后端日志里连请求都没有），
而章节完成工作流、修订闭环、全量扫描都是分钟级的真实模型调用，
所以配置里把 `experimental.proxyTimeout` 放宽到 600 秒 —— 这不是可选项，删了这些功能在浏览器里就没法用。

### 3. 装载测试小说

界面里：创建小说 → 点「装载测试小说」。或走接口：

```powershell
curl -X POST "http://127.0.0.1:8000/api/novels/<novel_id>/seed"
```

## 三、核心设计

### 1. Canon 状态机（安全原则 1、2、5 的落地）

```
AI / 工具 / 抽取器 ──► PROPOSED ──(作者确认)──► CANON ──(被新事实取代)──► SUPERSEDED
                          └──(作者驳回)──► REJECTED
```

- `services/query_service.propose_canon_fact` 是**唯一**的 AI 侧写入口，函数内部把状态硬编码为 `PROPOSED`，
  代码里不存在「AI 直接写 CANON」的路径。
- 章节完成工作流的第 1—10 步（保存 → 抽取 → 标 PROPOSED → 审校 → 出报告）跑完后，Canon 一条都不会变；
  第 11—15 步（更新 Canon / 人物 / 事件 / 时间线 / 伏笔）必须由作者在审校面板确认后点击「应用确认项」才执行。
- 升级一条事实时，同主谓的旧事实自动标记 `SUPERSEDED` 并记录 `superseded_by`。

### 2. 每个问题都必须有证据（安全原则 4）

`ContinuityIssue` 在 Pydantic 层强制要求 `evidence` 非空，且每条证据的 `source_chapter` 不能为空。
模型叙事通道返回的候选若不带证据，会被丢弃并记入报告的 `dropped_issues`，绝不进入给作者看的问题列表
（离线提供者故意返回一条无证据候选，专门用测试守住这个门槛）。

### 3. 审校双层结构

- **硬规则（确定性、不依赖模型）**：人物状态冲突（死者/无法行动者出现动作）、事实冲突（同主谓不同宾）、
  时间线倒置、未知人物、世界观规则、伏笔长期未回应、重复事实。
  属性句与日期归属都由 `services/text_rules.py` 的规则判定，所以**模型漏抽也不会漏检**。
- **叙事通道（可选调用模型）**：语义层面的可疑之处，输出同样必须带证据。

写第 14 章「林默腰间的佩剑，换成了赤霄剑」这类句子时，即使模型什么都没抽到，
硬规则也能从正文里读出「林默 佩剑 赤霄剑」并与 Canon 的「青霜剑」对上。

### 4. AITool（第五节的 10 个工具）

`search_chapters`、`get_character`、`get_character_state`、`get_relationship`、`get_events`、
`get_timeline`、`get_canon_facts`、`get_foreshadowing`、`get_world_rules`、`propose_canon_fact`。

- 每个工具的参数都是 Pydantic Schema，可用 `GET /api/ai/tools` 取到完整定义，未来可直接切到模型原生 function calling。
- 工具只读为主，唯一的写工具 `propose_canon_fact` 仍然只能写 PROPOSED，且写库由后端完成，模型拿不到 SQL。
- 查不到结果时返回 `status="UNKNOWN"`，而不是编造（安全原则 3）。

### 5. MemorySearch（长期记忆）

自然语言问题 → 识别其中的实体（人物/地点/事实主谓宾/事件名）与其余检索词 →
分别命中 Canon 事实、章节全文、事件、人物档、时间线 → 排序 → 交给模型组织语言。三道门槛：

1. 回答里出现的章节引用必须落在检索到的证据范围内，否则判为幻觉，改用证据摘要作答并记 warning；
2. 证据里含 PROPOSED 事实时，回答必须显式声明「尚未确认」，否则自动补声明；
3. 问题里出现库里查不到的词（例如问「师父喜欢吃什么」），回答必须说明这部分是 UNKNOWN，置信度压到 LOW 或 UNKNOWN。

### 6. ChapterWriter

写作前强制检索：相关人物的 Canon 事实、人物当前状态、最近事件、时间线、未回收伏笔、世界观规则、前文摘要，
再加**语义检索到的前文片段**（讲的是同一件事但用词不同的段落），
这份检索快照随生成记录一起入库，可事后审计「写作时到底读到了什么」。
生成后校验「必须出现」与「禁止出现」清单，禁止内容若是从 Canon 检索结果里带出来的，会先被剔除并给出提示。

## 四、V0.2 新增能力的设计

### 1. 混合检索（语义记忆）

```
查询 → 关键词通道（FTS5 + 设定条目按词命中加权）
     ↘ 向量通道（EmbeddingProvider → 余弦相似度）
     → RRF 融合 + 类型加成（设定类证据 > 正文片段）→ 统一候选列表
```

- `EmbeddingProvider` 有两个实现：`LocalHashingEmbedding`（中文 1/2/3 字符 n-gram 带符号哈希投影，
  本地确定性、无需网络，因此离线测试与无密钥运行都能用）与 `OpenAICompatEmbedding`（任何 `/embeddings` 端点）。
  配好远程 embedding 后，混合检索就是真正的语义召回，业务代码不用改。
  必须说清楚：本地向量是**词形层面**的相似度，不是训练出来的语义模型。
- 章节按段落切成 ~400 字片段（带重叠）；Canon 事实、时间线、事件、伏笔、人物档各作为一条记录。
- 向量以 float32 二进制存储，检索时一次读出做矩阵运算（装了 numpy 走 numpy，没装用纯 Python 回退，
  实测 123 条记录时 3ms／混合检索 11ms）。
- 正文片段与设定条目用**不同的向量门槛**（0.20 / 0.34）：正文相关即可入选，
  设定条目必须足够接近才算「结构化证据」，避免泛泛相关的内容被当成答案依据。

### 2. 时点视图（按章生效的 Canon）

每条 Canon 事实带 `valid_from_chapter` / `valid_until_chapter`：
确认一条新事实时，同主谓的旧事实自动在「新事实生效的那一章」失效。

- `GET /api/novels/{id}/state?chapter=N` 给出第 N 章时的完整世界状态；
- 一致性检查改为**按被检章节的时点**取 Canon、时间线与事件；
- 效果：确认「第 14 章换了佩剑」之后，回头检查第 6 章不会被误判为矛盾
  （第 6 章写的青霜剑在那一刻仍然有效），而第 14 章与之后仍以赤霄剑为准。
- PROPOSED 事实在任何时点视图里都不会出现。

### 3. 章节规划与伏笔调度

- `foreshadow_service.debt()` 计算每条未回收伏笔「自最近强化以来已过多少章」，超过 8 章记为欠账；
  `suggest()` 按紧迫度给出建议回收窗口与理由（含首现章、最近强化章、相关人物）。
- `PlannerAgent` 以「截至最新一章的 Canon + 人物弧线 + 伏笔欠账 + 最近摘要 + 已有计划」为上下文，
  输出严格 JSON 的章节计划（目标 / 必须出现 / 禁止出现 / 出场人物 / 要推进的伏笔 / 理由），落库 `chapter_plans`。
  已死亡或无法行动的人物会被要求写进 forbidden，不得作为出场人物。
- 计划可直接转成写作任务：`POST /api/plans/{id}/write?save=true`。
  计划里已有的正文不会被覆盖（规划时会跳过并说明原因）。

### 4. 全量扫描与总览看板

- `POST /api/novels/{id}/sweep`：`mode=rules` 只跑确定性规则（秒级，不花模型调用，
  并复用该章已有抽取结果）；`mode=full` 先逐章抽取再审校（慢，会消耗模型调用）。
- 每章结果都写进 `continuity_reports`，`GET /api/novels/{id}/dashboard` 汇总成
  每章健康矩阵、错误类型统计、未检查章节、伏笔欠账、PROPOSED 积压与向量索引状态。
- 伏笔拖欠提示只在**写作前沿**（最新一章）出现，回看旧章不会再被历史欠账打扰。

### 5. 多模型

```env
# 任意 OpenAI 兼容端点，按数组配置，随时可加
NOVELOS_PROVIDERS=[{"name":"grok","base_url":"https://api.x.ai/v1","model":"grok-4","api_key_env":"XAI_API_KEY"}]
# 语义检索用的向量提供者：local（默认）| openai
NOVELOS_EMBEDDING_PROVIDER=openai
NOVELOS_EMBEDDING_BASE_URL=https://api.openai.com/v1
NOVELOS_EMBEDDING_MODEL=text-embedding-3-small
```

DeepSeek、Grok、OpenAI、本地 vLLM 共用同一套 `/chat/completions` 实现（`app/ai/chat.py`），
`GET /api/ai/providers` 会列出全部提供者及其可用性与是否支持工具调用。

### 6. agent 模式（模型驱动的工具调用）

`POST /api/novels/{id}/ai/ask` 的 `mode` 可选 `simple`（后端检索 → 模型组织语言）或
`agent`（模型自己选工具取数）。agent 模式下：

- 工具 schema 由 `AIToolKit` 导出为 OpenAI function 格式，模型每轮的参数都要过 Pydantic 校验；
- 轮数上限 4 轮，超限或模型不支持工具调用时会**显式回退**到 simple 模式并给出 warning；
- 回答里的章节引用必须出现在工具返回或检索证据里，否则改用证据摘要作答（防幻觉）；
- PROPOSED 未确认声明、无记载即 UNKNOWN 这两条门槛同样适用。

### 7. 规模与迁移

- 章节列表、扫描记录支持 `offset/limit`，章节列表返回 `X-Total-Count`；前端默认加载 100 章并可「加载更多」。
- `app/migrations.py` 在启动时做幂等增量迁移：补列、补该列的索引、回填历史数据
  （V0.1 的事实补上生效章号，被取代的事实补上失效章号），**已装着 V0.1 数据的库无需重建**。
- 扫描/看板/检索都是只读或基于已有数据，百万字量级的向量索引可随时用
  `POST /api/novels/{id}/vectors/reindex` 重建。

## 五、接口一览（76 个路径 / 102 个操作，完整文档见 /docs）

| 分组 | 端点 |
| --- | --- |
| 小说 | `GET/POST /api/novels`、`GET/PATCH/DELETE /api/novels/{id}`、`/stats`、`/reindex`、`/recount`、`/seed`、`/bible` |
| 章节 | `GET/POST /api/novels/{id}/chapters`（支持 `offset/limit`，返回 `X-Total-Count`）、`GET /api/novels/{id}/search`、`GET/PUT/DELETE /api/chapters/{id}`、`POST /api/chapters/{id}/complete`、`GET/POST /api/chapters/{id}/continuity` |
| 设定库 | 人物/关系/事件/Canon 事实/伏笔/时间线/世界观规则的 CRUD，`POST /api/canon-facts/{id}/confirm|reject` |
| AI | `POST /api/chapters/{id}/extract`、`POST /api/novels/{id}/ai/ask`（`mode=simple|agent`）、`POST /api/novels/{id}/ai/write-chapter`、`GET /api/ai/providers`、`GET /api/ai/tools`、`POST /api/ai/tools/{name}`、审校与抽取任务的审阅/应用 |
| 洞察（V0.2） | `GET /api/novels/{id}/state?chapter=N`、`GET /api/novels/{id}/dashboard`、`POST /api/novels/{id}/sweep`、`GET /api/novels/{id}/sweeps`、`GET /api/novels/{id}/retrieval`、`GET /api/novels/{id}/vectors`、`POST /api/novels/{id}/vectors/reindex`、`GET /api/novels/{id}/foreshadowing-plan` |
| 规划（V0.2） | `GET/POST /api/novels/{id}/plans`、`POST /api/novels/{id}/plans/generate`、`DELETE /api/plans/{id}`、`POST /api/plans/{id}/write` |
| 质量（V0.3） | `GET/POST /api/novels/{id}/style/baseline`、`GET /api/novels/{id}/style/reviews`、`POST /api/novels/{id}/style/review-text`、`POST /api/chapters/{id}/style-review`、`GET /api/novels/{id}/invariants`、`POST /api/novels/{id}/invariants/run`、`GET/POST /api/novels/{id}/commitments`、`POST /api/commitments/{id}/fulfill|abandon`、`POST /api/novels/{id}/ai/revise-chapter` |
| 碎片与设定核对（V0.4/V0.5） | `GET/POST /api/novels/{id}/fragments`、`/fragments/bulk`、`/fragments/stats`、`/fragments/relevant`、`/fragments/intent/{n}`、`/fragments/realize`、`/fragments/prompts`、`GET/PATCH/DELETE /api/fragments/{id}`、`POST /api/fragments/{id}/place`、`GET/POST /api/novels/{id}/style/voice` |
| 断言核对与文风锁定（V0.5） | `POST /api/novels/{id}/claims/verify`、`GET /api/novels/{id}/claims`、`GET /api/claims/{id}`、`POST /api/novels/{id}/style/baseline/{profile_id}/lock`、`GET /api/novels/{id}/style/drift` |

## 六、测试

```powershell
cd D:\Projects\NovelOS\backend
.\.venv\Scripts\python.exe -m pytest                    # 230 passed（跳过的是需要真实模型的用例）
$env:RUN_LIVE="1"; .\.venv\Scripts\python.exe -m pytest tests\test_live_deepseek.py   # 真实模型联调（19 个）
```

覆盖需求第九节的六项能力，以及 V0.2—V0.5 新增的检索、时点、规划、扫描、工具、迁移、文风、修订闭环、碎片成文与断言核对：

| 用例文件 | 验证内容 |
| --- | --- |
| `test_seed_dataset.py` | 测试小说规模（20 章 / 5 人物 / 10 事件 / 5 伏笔 / 13 条 Canon）、Markdown 落盘、中文全文检索、人物动态状态 |
| `test_extraction.py` | 能否正确提取事实；抽出的事实只能是 PROPOSED；确认后才升级并取代旧事实 |
| `test_continuity.py` | 能否找到 3 处故意矛盾；干净章节零误报；每条问题都有来源章节；无证据候选被丢弃；确认状态变更后误报消失 |
| `test_memory_search.py` | 能否正确查询前文；引用幻觉会被改写；PROPOSED 不得被当成 CANON；查不到就问 UNKNOWN |
| `test_character_state.py` | 人物状态维护：状态历史、按章回溯、确认后才更新 |
| `test_chapter_writer.py` | 能否根据前文生成下一章；检索快照、必须/禁止内容、草稿落库 |
| `test_workflow.py` | 第 1—10 步自动、第 11—15 步需确认；Canon 在工作流中不变 |
| `test_api.py` | REST 接口、参数校验、工具 Schema、404/409/422 边界、空项目状态 |
| `test_v2_retrieval.py` | 切块、本地向量可复现且归一化、索引幂等、章节改写后片段替换、向量阈值、双通道融合、纯向量召回、结构化证据优先 |
| `test_v2_state.py` | 时点视图排除后发生的事实；确认变更后回溯旧章不再误报；按章生效窗口；**不用未来状态冒充过去状态**；删章时清理未确认候选事实 |
| `test_v2_planner.py` | 计划连续编号与落库、避开死亡/昏迷人物（提示词 + 代码双重约束）、优先推进最久伏笔、覆盖与跳过语义、计划↔正文状态同步、伏笔欠账与回收建议、按计划写作 |
| `test_v2_sweep.py` | 规则模式覆盖全书并复现 4 处埋设问题、复用已有抽取、指定章节扫描、full 模式逐章抽取、扫描记录、看板聚合与未检查提示、检索/向量接口 |
| `test_v2_agent_tools.py` | 工具 schema 形状、agent 循环执行工具、引用护栏拒绝不存在的章节、不支持工具时显式降级、轮数上限、agent 不写 Canon；多模型注册表；迁移补列/补索引/回填 |
| `test_v3_style.py` | 文风指标可复现、句式均匀/套话密集能被识别、章末钩子、章内复读、基线对比阈值、确定性改稿（去填充词、断长句）、离线提供者不编造读感意见、指标对比方向（含「无变化=持平」） |
| `test_v3_invariants.py` | 7 类全局不变量逐条植入违规并检出（含时间倒置、境界回退、独占冲突、认知回缩、同日跨地），干净种子零误报，跨天移动不误报，改回后恢复干净，接口与留存报告 |
| `test_v3_revision.py` | 期限换算（三日内→四月初五）、到期当天不算逾期、越期含越界章证据、兑现/放弃、离线抽取抓到书里真实约定、修订闭环（指标不恶化、无进展即停、轮数上限、落库最终稿、评审历史）、文风/承诺/看板接口 |
| `test_v4_fragments.py` | 碎片 CRUD 与状态流转、碎片进全文/向量索引并可被语义召回、离线成文保留作者原话（treatment=QUOTED）、**对应关系双向校验**（编造的 fragment_id / 不存在的引用会被丢弃）、**成文里的新设定性陈述会被报出**、成文落章并回填碎片、情感引导只提问、**改稿磨掉作者声音就回退**、规划能看到未安排的碎片、接口与看板 |
| `test_v5_quality.py` | 6 项推进与节奏指标可复现且**不误报已认可的章节**、注水/名词解释/标点均匀/情绪平/句式循环/推进不足逐条植入并检出（都带原文证据）、跨章复读按四元组重合度判出、**锁定文风后窗口收紧一半**、解锁/换锁只保留一个、逐章漂移报告、断言核对三态（一致/冲突/待确认）、**表述不像取值时降级为人确认而不是假冲突**、PROPOSED 不冒充 Canon、四条世界观规则的确定性判定（含回忆句豁免）、模型编造的片段被丢弃、接口 |
| `test_v6_wording.py` | 8 项用词与收束指标可复现、**种子章节零误报**（含跨章桥段检查）、用同一批词写会同时触发可预测/集中度/动词单调、**堆砌辞藻只在有既往文本时才判「飘」且列出具体生僻字**、总结与升华句按每千字密度拦下、过渡连接词密度、抽象替代具体、桥段的跨章计数（≥4 章才报，少于则不算）、锁定指令里写明 V0.6 约束 |
| `test_live_deepseek.py` | 真实 DeepSeek：抽取 + 三处矛盾的审校、问答、写作、规划、agent 工具调用、全量扫描、混合检索与时点视图；文风读感意见必须逐字对上原文；改稿用指标对比证明 AI 味是否真的下降；**碎片成文必须让作者原话留痕**；**改稿不得磨平作者声音**；**断言核对必须抓到植入的幻觉并保留待确认项**；**锁定文风与世界观规则必须进入写作提示**；**改稿要真的删掉总结与升华、且不许靠生僻字假装人味**；**读感评审要能自己指出总结式收尾** |

### 测试小说里故意埋的三处矛盾

| 章节 | 埋设内容 | 期望检出 |
| --- | --- | --- |
| 第 14 章 | 「林默腰间的佩剑，换成了赤霄剑。」 | `FACT_CONFLICT`，证据 = 第 3 章 + 第 14 章 |
| 第 15 章 | 第 8 章已死亡的赵铁山现身、说话、出手 | `DEAD_CHARACTER_ACTIVE`，证据 = 第 8 章 + 第 15 章 |
| 第 18 章 | 「那桩血案，是天启三年三月里的事。」（Canon 为四月初五） | `TIMELINE_INVERSION`，证据 = 第 5 章 + 第 18 章 |

另外第 9 章（赵铁山墓前）与各章开篇的日期句是**对照项**，用来确保审校不会误报死者复现与时间线漂移。

## 五、界面

左侧章节列表（含全文搜索与跳转）、中间正文编辑器（标题 / 故事时间 / 地点 / 字数 / 保存 / 完成本章工作流）、
右侧 AI 助手（抽取与审校、章节写作），底部 11 个面板：人物、时间线、世界观、事件、伏笔、Canon、总览、规划、碎片、质量、QA。

- 审校面板逐条显示问题、证据与修改建议，可逐项接受/驳回、应用确认项；
- Canon 面板可按状态过滤，直接确认/驳回 PROPOSED 事实，并显示每条事实的生效章 / 失效章；
  「时点视图」可查看第 N 章时的 Canon 与人物状态；
- 总览面板：一键规则扫描或深度扫描、每章健康矩阵、错误类型统计、伏笔欠账、全局不变量、承诺逾期、
  文风基线与平均分、向量索引与重建、混合检索试跑；
- 质量面板：建立文风基线、评一段草稿（指标 + 问题 + 原文片段）、重跑全局不变量、维护承诺账本（兑现/放弃/筛选）；
  V0.5 起新增「设定断言核对」（贴一段草稿或选一章 → 一致／冲突／待确认三组，逐条给原文片段与证据）与
  「文风锁定与漂移」（锁定基线、逐章看漂了哪几项指标）；
- 规划面板：生成接下来 N 章的计划、查看伏笔回收建议、按计划写草稿或存成章节；
- 章节「文风」标签：对本章做规则层或「规则 + 模型读感」评审，并列出历史评审；
- 写作面板：可勾选「自动修订（写作→评审→改稿）」，展示每轮分数、字数、文风/一致性 codes 与指标前后对比；
- QA 面板输入自然语言问题，返回答案、置信度与证据清单；可切到「Agent 模式」让模型自己调用工具，并回显工具调用明细；
- 工作流跑完会在编辑器下方列出第 1—11 步的执行状态（V0.3 起含承诺抽取）；
- 窄屏（<1100px）自动改为单列堆叠，设定库表格横向滚动。

## 七、进度盘点与路线图（离百万字终局还有多远）

### 已经能稳定做的事

| 环节 | 现状 | 证据 |
| --- | --- | --- |
| 单章抽取 | 真实模型抽出人物/事件/时间线/事实/伏笔/承诺，全部落 PROPOSED 等作者确认 | 20 章示例 + 联调测试 |
| 单章审校 | 硬规则 + 模型叙事通道，每条问题带来源章节；3 处埋设矛盾全部命中，15 章零误报 | `test_continuity.py` |
| 长程自洽 | 时点视图 + 7 类全局不变量 + 承诺账本（越期报警含越界章） | `test_v3_invariants.py`、`test_v3_revision.py` |
| 幻觉兜底 | 断言核对（一致／冲突／待确认，每条带原文片段与 Canon／规则证据）+ AI 只能写 PROPOSED | `test_v5_quality.py`、联调 |
| 文风不漂 | 锁定基线按 0.5 倍容差比对，漂移逐章点名；生成前把文风要求写进提示 | `test_v5_quality.py`、联调 |
| 记忆检索 | 关键词 + 向量混合召回；agent 模式让模型自己调工具取数 | `test_v2_retrieval.py`、`test_v2_agent_tools.py` |
| 写作 | 按 Canon 约束生成 + 文风/一致性评审 + 自动改稿闭环，指标轨迹可审计 | `test_v3_revision.py` 与联调 |
| 规划 | 按伏笔欠账与人物弧线规划接下来 N 章，可一键转写作任务 | `test_v2_planner.py` |
| 规模基础 | 分页、float32 向量索引、幂等迁移、全量扫描（rules 秒级 / full 逐章） | `test_v2_sweep.py`、`test_v2_agent_tools.py` |

### 还没解决的问题（按对终局的影响排序）

1. **写作上下文预算**：100 万字 ≈ 400–500 章，写手现在读到的是「相关人物的 Canon + 最近 3 章摘要 + 语义 top5 片段 + 相关碎片」。
   跨卷的伏笔、人物弧线、伏笔回收的节奏，靠这些是不够的。
   **下一步**：分层记忆（章 → 卷 → 全书滚动摘要）+ 每章的场景状态简报（人在哪、手里有什么、谁知道什么）。
2. **规模化的人机协作**：现在每章会产生 10–30 条待确认项，全部靠人工逐条审。500 章会把人拖死。
   **下一步**：按风险分级（重复事实/低置信度/涉及死亡与修为的才拦），批量审阅，审阅队列与快捷键。
3. **情节与爽点结构**：现在有章末钩子、冲突密度与规划，但「黄金三章」「每 3–5 章一个小高潮」「卷末大高潮」这类网文结构约束还没有硬检查。
   **下一步**：把节奏结构变成可校验的规划约束 + 每卷的爽点/成长点清单。
4. **模型级全局审校**：跨章的语义矛盾（同一条线索两种解释、人物动机断裂）现在只能靠抽样人读。
   **下一步**：定期抽样做「全书提问式审校」（给模型一组 Canon 事实与若干章节片段，专门找矛盾）。
5. **成本与吞吐**：全流程同步执行，`full` 扫描逐章调用模型；没有队列、并发、增量扫描与 prompt 缓存复用。
   **下一步**：任务队列 + 并发上限 + 只扫变更章节 + 摘要级缓存。
6. **评测集**：现有 3 处埋设矛盾 + 规则回归；文风只能与本书自己的基线比，声音画像需要作者提供足够素材。
   **下一步**：①长程回归（连续生成 30–50 章后再跑全部不变量与承诺检查）；②作者「认可样章」语料库；
   ③接入公开可用的中文小说语料做文风分布对照（注意版权，只用统计量）。
7. **幻觉的量化**：V0.5 能逐条给出「冲突／待确认」，但还没有「每万字幻觉率」这种可比的长期指标，
   也没有把「同一处设定在第 3 章和第 40 章被写成两个样子」这类**跨章语义矛盾**自动串起来（现在靠不变量与抽样人读）。
   **下一步**：把断言核对结果按章累积成幻觉率曲线；对同一 (subject, predicate) 的历史断言做时间轴比对。
8. **角色口吻的个体差异**：现在有整体声音画像与「腔调是否一致」，但还没有「张三说话短促、李四绕」的**人物级**口吻档案。
   **下一步**：给人物档案加语癖字段（句长偏好、口头禅、称谓习惯），评审时按人物分别校验。
9. **向量检索的规模**：现在是内存余弦（实测 123 条 3ms，百万字估算百毫秒级），到几百万字应该换 sqlite-vec 或专用向量库。

### 用数字衡量「离终局多远」

建议把这几个指标当成长期看板（现在系统已经能算出前四个）：

| 指标 | 当前可测 | 目标方向 |
| --- | --- | --- |
| 一致性：每 10 万字的 `error` 数 | ✅ 扫描报告 | 趋近 0 |
| 承诺兑现率：已兑现 / （已兑现 + 逾期） | ✅ 承诺账本 | 趋近 100% |
| 文风偏离度：与本书基线的加权偏差 | ✅ 文风评审 | 稳定在基线窗口内 |
| AI 味指标：套话密度、burstiness、对白占比、章末钩子 | ✅ 文风评审 | 套话趋近 0；节奏指标落在网文区间 |
| 人工审阅工时：每章的待确认项与耗时 | ⚠️ 只有条目数 | 逐卷下降 |
| 读者向指标：追读率、章末留存 | ❌ 未接入 | 需要真实读者数据 |

## 八、已知边界（V0.6 范围）

- **输出会被截断**：模型生成到 `max_tokens` 上限时，`ChatCompletionsProvider` 现在会显式给出
  「输出被截断」warning（以前是静默丢内容：抽取少几条、规划只写一半都可能看不出来）。
  抽取与规划的输出上限已提到 8192；仍遇到截断就缩小范围（按章抽取、减少计划章数）重试。
- **开发服务器的代理会掐断长请求**：Next.js 的 rewrite 默认 30 秒超时，
  章节完成工作流 / 修订闭环 / 全量扫描这类分钟级调用必须靠 `experimental.proxyTimeout` 放宽（已设为 600 秒）。
- **V0.6 的用词指标都是近似量**：用词多样性用**汉字二元组**在 200 字滑动窗口里的类符/形符比近似
  （不引入分词器），它衡量的是「同一批词反复用」而不是语言学意义上的词汇量；
  `novel_char_ratio`（本书没用过的字占比）随书推进系统性下降，所以只用绝对上限判断「满篇生僻字」，
  不参与逐章漂移统计；`BRIDGE_PATTERNS` 是固定的桥段短语表，表外的套路识别不到。
- **改稿的篇幅闸门是不对称的**：暴跌（< 0.6 倍）直接拒绝，因为那是静默丢内容；
  增长到 1.2 倍以上只提醒（把总结句改成动作与对白本来就会变长），超过 1.8 倍才拒绝（那更可能另写了一章）。
- **本版不追求困惑度**：提高困惑度最省事的办法是堆生僻字与怪搭配，那是另一种失真。
  所以用词类指标里，「太平」与「太飘」两侧都有闸门，改稿建议统一指向「更具体的说法」。
- **离线规则提供者是确定性规则引擎**（用于测试与无密钥降级），只覆盖正文中显式表达的设定，不具备语义理解；
  真正的抽取质量来自 DeepSeek Flash。它的「工具调用」也是脚本化的固定查询序列。
- **断言核对的两条通道各有边界**：确定性通道只认显式句式（「X 的佩剑是 Y」），指代词（「这柄剑」）解析不了；
  模型通道负责这类自由表述，但只会拆句，**判定全在 Python 里做**，所以它报不出「它没拆到的矛盾」。
  查无此设定一律是 `UNVERIFIED`：宁可让你确认，也不替你把新设定写进 Canon。
- **判定阈值的取舍**：取值比对用二元组相似度（0.6）+ 包含关系；两个取值若不像短实体名（长句、带引号），
  冲突会**降级**成「请人工确认」而不是直接判冲突 —— 少报一条假冲突比多报一条更有价值。
- **世界观规则分两类**：带结构的四类（死者复生 / 唯一持有 / 能力门槛 / 不可变特质）走确定性判定，
  门槛按境界阶梯（炼气→筑基→金丹→元婴→化神）比较，需要规则文本里写明境界名；
  其余自由文本规则只能靠「相关且极性相反」的近似判断，会有漏报。
- **V0.5 的六项新指标都是启发式**：动作词表、句式骨架（句首三字 + 收尾标点 + 长度档）、
  标点熵、情绪档位都写死在 `style_service.py` 里并按本书基线做相对比较，
  它们的作用是**排序与定位**（哪一段最像注水），不是绝对的「AI 检测器」。
- 文风锁定收紧的是**比对窗口**（容差 ×0.5），不是让模型换一个风格；锁定前请确认那几章确实是你认可的稿子。
- 本地向量是**词形层面**的 n-gram 哈希向量，能容忍说法差异但不等同于训练出来的语义模型；
  需要真正的语义召回时，把 `NOVELOS_EMBEDDING_PROVIDER` 换成远程 embedding 即可，代码不用改。
- 向量检索目前是全表读取后在内存里算余弦：实测 123 条记录时纯 Python 路径 `vector_search` 3ms、
  混合检索 11ms，耗时随记录数线性增长（百万字约 3000 个片段，估算百毫秒级）。
  装了 numpy 会自动走矩阵运算（`backend/requirements-vector.txt`，可选）；
  真正到百万字规模建议换成 sqlite-vec 或专用向量库，接口已隔离在 `services/vector_service.py`。
- 跨章复读检查默认只看**当前章号之前的 60 章**（四元组倒排索引，避免长篇上段落两两比较）；
  要查更早的重复可以调大 `review_chapter(prior_chapters=...)`。
- 全量扫描的 `rules` 模式不调用模型，因此看不到「模型才能发现的语义级矛盾」；
  `full` 模式逐章调用模型，20 章约需数分钟并产生费用，界面已提示。
- 人物状态冲突针对「死亡 / 重伤昏迷 / 失踪 / 闭关 / 被囚 / 封印」这类无法行动的状态；
  更细的状态语义（如「隐姓埋名」）需要作者通过人物档案补充。
- 多 Agent 目前是「单 Agent + 工具循环」；多人协作、权限、云端多租户、封面/插图生成不在当前范围内。
