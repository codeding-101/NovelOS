# NovelOS

一个中文长篇小说的写作台。目标是让 AI 帮你写到 100 万字，设定不打架、文字不像机器写的。

它不替你写小说。你记下想法碎片（一句话、一个画面、一段对白都行），系统负责检索已有设定、
把碎片展开成正文、然后逐条告诉你哪里可能有问题。所有 AI 产出都必须带证据，所有设定改动都必须你点确认。

当前版本 0.6.0。后端 FastAPI + SQLite，前端 Next.js 15，默认模型 DeepSeek Flash（可换任意 OpenAI 兼容端点）。

## 跑起来

后端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt      # 国内可以加 -i https://mirrors.aliyun.com/pypi/simple/
Copy-Item .env.example .env                                          # 要真实模型就填 DEEPSEEK_API_KEY
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

没填密钥也能跑：会自动降级到离线规则提供者，并在界面上把这次降级当 warning 说出来，
不会假装一切正常。接口文档在 http://127.0.0.1:8000/docs 。

前端：

```powershell
cd frontend
npm install
npm run dev          # http://127.0.0.1:3000
```

前端只请求相对路径 `/api/*`，由 `next.config.mjs` 转发到 8000。这里改了一处默认配置：
`experimental.proxyTimeout` 设成 600 秒。Next 的开发服务器代理默认 30 秒就断，
而章节完成、修订闭环、全量扫描都是分钟级的模型调用——不放开的话，浏览器里点「完成本章」
只会看到 500，后端日志里连请求都没有。这个坑我踩过一次，不想再踩。

装测试小说：界面上「创建小说」→「装载测试小说」，或者

```powershell
curl -X POST "http://127.0.0.1:8000/api/novels/<novel_id>/seed"
```

种子小说是《剑起青云》20 章，5 个人物、10 个事件、5 条伏笔、13 条 Canon 事实，
里面故意埋了 3 处矛盾用来验证审校（见下文「测试」一节）。

## 它怎么帮作者

入口是碎片。你可以往 `fragments` 里扔任何东西：半句话、一个画面、一句对白、
对某个人物的想法。碎片有类型（奇思妙想/场景/对白/人物/冲突…）、意图、标签、相关人物、目标章号。
它们同时进全文索引和向量索引，所以写作时会自动带上相关碎片，规划时会看到还没安排的碎片，
问答也能召回「作者当初是怎么想的」。状态从 INBOX 走到 PLACED 再到 REALIZED，成文后回填哪条碎片
进了哪一章。

成文得交对应表。`FragmentRealizer` 输出正文时，每条碎片都得给出 `fragment_id` + `prose` +
`uses_quote` + `treatment`，代码会双向校验：`uses_quote` 必须真在碎片原文里、`prose` 必须真在成文里，
对不上的对应关系直接丢掉写进 warnings。用不上的碎片必须写进 `undeveloped` 并说明原因。
每次成文还会用确定性规则反向扫一遍，凡是 Canon 里没有的「主语+谓语+宾语」陈述
（比如「林默的佩剑是玄铁重剑」）都报出来，让你决定收下还是改掉。

写一章之前，系统会先把设定喂进去：强制检索相关人物的 Canon 事实、人物当前状态、最近事件、
时间线、未回收伏笔、世界观规则、前文摘要，再加上语义检索到的前文片段（讲同一件事但用词不同的段落）。
如果这本书锁过文风，检索快照里还会带一段把基线翻译成硬要求的文字（节奏、推进密度、标点、
句式、别重复哪些桥段）。这份快照随生成记录一起入库，事后能查「写的时候到底读到了什么」。

写完会逐条核对，这是 0.5 之后加的重点，分四层：

- 声称核对：把正文拆成一条条设定断言，跟 Canon 和世界观规则比。命中就是一致；同一主谓给出不同取值
  就是冲突（带章号证据）；Canon 里没有这条就是待确认。模型只负责拆句，判定全在 Python 里做，
  每条断言必须附正文里逐字存在的片段。
- 文风评审：三十多项确定性指标对比这本书自己的基线，加上一层模型读感（视角漂移、角色腔调、
  信息复述、情绪靠旁白直说），模型意见也必须能逐字对上原文。
- 推进与节奏：注水段落、名词解释、标点均匀、情绪太平、句式循环、跨章复读。
- 用词与收束：用同一批词打转、堆生僻字、章末总结、升华议论、过渡太顺、收尾桥段反复用。

改稿有两道护栏。声音护栏：改完先比「声音保留分」，磨掉作者特征就回退保留原稿
（实测有一次 DeepSeek 改稿把分数从 53.3 压到 25.4，被拦下了）。篇幅护栏是不对称的：
暴跌到 0.6 倍以下直接拒（那是偷工，会静默丢内容），涨到 1.2 倍以上只提醒，超过 1.8 倍才拒。
中间地带放宽，是因为「把总结句改成动作和对白」本来就会变长——之前 1.5 倍的闸门
把一次正确方向的改稿整篇打回过。

## 实测数字

改稿闭环对一段典型「AI 味」样段的实测（DeepSeek，指标都是每千字/比例）：

| 指标 | 改稿前 | 改稿后 |
| --- | --- | --- |
| 套话密度 | 58.02 | 0.00 |
| 抽象词密度 | 13.65 | 0.00 |
| 长句占比 | 0.222 | 0.000 |
| 章内自重复率 | 0.0115 | 0.0000 |
| 对白占比 | 0.000 | 0.287 |
| 短句占比 | 0.000 | 0.543 |

同一个闭环对着 0.6 版的样稿（故意写成「章末总结 + 升华议论」）：总结回扣句 33.98 → 5.13，
升华议论 4.85 → 0.00，用词多样性 0.956 → 0.921（没有压塌），生僻字占比保持 0.00
（没靠堆生僻字假装人味）。改后的文字把「掌柜什么也没说」换成了一段对白。

碎片成文：用作者的四条碎片建画像后让 DeepSeek 成文，声音保留分 76.6，文风得分 86.0，
3/3 条碎片全部展开，对应表逐条可核对，命中的特征词是作者惯用的「后来才」「才知道」「把刀」。

有个指标陷阱值得记下来：`burstiness`（句长变异）在拆长句的改稿里会下降，
这次从 0.662 降到 0.462。它下降不代表节奏变差，所以测试里的判据是
「AI 味指标不得变差，且 burstiness 不低于下限」，而不是越高越好。

## 两条红线

**AI 写不到 CANON。** 唯一的 AI 侧写入口是 `query_service.propose_canon_fact`，
函数里把状态硬编码成 `PROPOSED`，代码中不存在 AI 直接写 CANON 的路径。
章节完成工作流的前 10 步（保存 → 抽取 → 标 PROPOSED → 审校 → 出报告）跑完，Canon 一条都不会变；
后面 5 步（更新 Canon / 人物 / 事件 / 时间线 / 伏笔）必须你在审校面板点「应用确认项」才执行。
升级一条事实时，同主谓的旧事实自动标 `SUPERSEDED`，并记下被谁取代。

**每条结论都要有证据。** `ContinuityIssue` 在 Pydantic 层强制要求 `evidence` 非空、
每条证据的 `source_chapter` 不能为空。模型叙事通道返回的候选如果没带证据，
会被丢弃并记进报告的 `dropped_issues`，不会出现在给你看的列表里。
离线提供者故意返回一条无证据候选，专门用测试守住这个门槛。

判定一致性时还有一层保护：属性句和日期归属都由 `services/text_rules.py` 的确定性规则判，
所以模型漏抽也不会漏检。第 14 章那种「林默腰间的佩剑，换成了赤霄剑」的句子，
即使模型什么都没抽到，硬规则也能从正文里读出来跟 Canon 对上。

## 怎么一步步长成现在这样

- 0.1：Canon 状态机、带证据的审校、10 个 AITool、MemorySearch、章节完成工作流。
- 0.2：混合检索（关键词 + 向量 RRF）、按章生效的时点视图、章节规划、全量扫描与看板、
  多模型、agent 模式工具调用、幂等迁移。
- 0.3：文风度量引擎与作者基线、StyleCritic、RevisionLoop（无进展即停）、7 类全局不变量、
  承诺账本、质量面板。
- 0.4：碎片变成一等公民、FragmentRealizer 与对应表、声音画像与改稿声音护栏、情感引导（只提问）。
- 0.5：声称核对、文风锁定与逐章漂移、四类世界观规则的结构化判定、6 项推进与节奏指标。
- 0.6：8 项用词与收束指标、跨章桥段同质化；明确不追求困惑度——太平和太飘两侧都拦，
  换词的方向是更具体，不是更罕见。

安全问题一直是同两条（见上），没有变过。

## 目录结构

```
backend\
  app\
    main.py           FastAPI 入口（启动时建目录、建表、跑迁移）
    config.py         配置（.env / 环境变量）
    database.py       引擎与会话
    migrations.py     幂等增量迁移（补列、补索引、回填历史数据）
    models.py         SQLite Schema（SQLAlchemy 2.0）
    schemas.py        Pydantic 出入参 + 各 Agent 的结构化输出契约
    timeutil.py       中文字数统计、中文数字与故事时间解析
    ai\
      base.py         AIProvider 抽象（含工具调用）
      chat.py         OpenAI 兼容提供者（DeepSeek / Grok / OpenAI / 本地共用）
      deepseek.py     DeepSeek Flash 预设
      offline.py      离线规则提供者（无密钥、测试用）
      embeddings.py   本地 n-gram 向量 / 远程 embedding
      factory.py      提供者注册表
      prompts.py      提示词
      tools.py        10 个 AITool + 参数 Schema
      agents\         抽取 / 审校 / 记忆检索 / 写作 / 规划 / 文风 / 修订闭环 / 碎片成文 / 声称核对 / 工具循环
    services\         章节、检索、向量、抽取、审校、写作、规划、伏笔、扫描看板、流程编排、种子数据
    routers\          novels / chapters / entities / insight / plans / quality / fragments / ai
  seed\               测试小说：设定圣经、实体数据、20 章正文
  tests\              230 个离线用例 + 19 个真实模型联调用例
frontend\
  app\                page.tsx（工作台）、layout.tsx、globals.css
  components\         ChapterPanel / EditorPanel / AIPanel / KnowledgePanel / DashboardPanel /
                      PlanPanel / FragmentBoard / RealizePanel / QualityPanel / StyleReviewView
  lib\                api.ts、types.ts
data\                 SQLite 库 + data/novels/<书名>/chNNN.md（运行数据，不入版本库）
```

## 接口一览

76 个路径 / 102 个操作，完整文档见 `/docs`。

| 分组 | 端点 |
| --- | --- |
| 小说 | `GET/POST /api/novels`、`GET/PATCH/DELETE /api/novels/{id}`、`/stats`、`/reindex`、`/recount`、`/seed`、`/bible` |
| 章节 | `GET/POST /api/novels/{id}/chapters`（支持 `offset/limit`，返回 `X-Total-Count`）、`GET /api/novels/{id}/search`、`GET/PUT/DELETE /api/chapters/{id}`、`POST /api/chapters/{id}/complete`、`GET/POST /api/chapters/{id}/continuity` |
| 设定库 | 人物/关系/事件/Canon 事实/伏笔/时间线/世界观规则的 CRUD，`POST /api/canon-facts/{id}/confirm\|reject` |
| AI | `POST /api/chapters/{id}/extract`、`POST /api/novels/{id}/ai/ask`（`mode=simple\|agent`）、`POST /api/novels/{id}/ai/write-chapter`、`GET /api/ai/providers`、`GET /api/ai/tools`、`POST /api/ai/tools/{name}`、抽取任务的审阅与应用 |
| 洞察 | `GET /api/novels/{id}/state?chapter=N`、`/dashboard`、`POST /sweep`、`GET /sweeps`、`GET /retrieval`、`GET /vectors`、`POST /vectors/reindex`、`GET /foreshadowing-plan` |
| 规划 | `GET/POST /api/novels/{id}/plans`、`POST /api/novels/{id}/plans/generate`、`DELETE /api/plans/{id}`、`POST /api/plans/{id}/write` |
| 质量 | `GET/POST /api/novels/{id}/style/baseline`、`GET /style/reviews`、`POST /style/review-text`、`POST /api/chapters/{id}/style-review`、`GET/POST /api/novels/{id}/invariants`、`GET/POST /commitments`、`POST /api/commitments/{id}/fulfill\|abandon`、`POST /api/novels/{id}/ai/revise-chapter` |
| 碎片 | `GET/POST /api/novels/{id}/fragments`、`/fragments/bulk`、`/fragments/stats`、`/fragments/relevant`、`/fragments/intent/{n}`、`/fragments/realize`、`/fragments/prompts`、`GET/PATCH/DELETE /api/fragments/{id}`、`POST /api/fragments/{id}/place`、`GET/POST /api/novels/{id}/style/voice` |
| 声称核对与文风锁定 | `POST /api/novels/{id}/claims/verify`、`GET /api/novels/{id}/claims`、`GET /api/claims/{id}`、`POST /api/novels/{id}/style/baseline/{profile_id}/lock`、`GET /api/novels/{id}/style/drift` |

## 测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest                    # 230 passed（跳过的是需要真实模型的用例）
$env:RUN_LIVE="1"; .\.venv\Scripts\python.exe -m pytest tests\test_live_deepseek.py   # 19 个真实模型用例
```

每条需求落在哪个机制、由哪个用例守着，另存了一份：[docs/requirements-to-tests.md](docs/requirements-to-tests.md)。

| 用例文件 | 管什么 |
| --- | --- |
| `test_seed_dataset.py` | 种子小说规模、Markdown 落盘、中文全文检索、人物动态状态 |
| `test_extraction.py` | 事实抽取；抽出的只能是 PROPOSED；确认后才升级并取代旧事实 |
| `test_continuity.py` | 3 处埋设矛盾能否找到；干净章节零误报；每条问题都有来源；无证据候选被丢弃 |
| `test_memory_search.py` | 前文问答；引用幻觉会被改写；PROPOSED 不当 CANON；查不到就说 UNKNOWN |
| `test_character_state.py` | 人物状态历史与按章回溯，确认后才更新 |
| `test_chapter_writer.py` | 按前文生成下一章；检索快照；必须/禁止内容；草稿落库 |
| `test_workflow.py` | 前 10 步自动、后 5 步需确认；Canon 在工作流中不变 |
| `test_api.py` | REST 接口、参数校验、工具 Schema、404/409/422、空项目 |
| `test_v2_retrieval.py` | 切块、向量可复现且归一化、索引幂等、双通道融合、阈值 |
| `test_v2_state.py` | 时点视图；确认变更后回溯旧章不再误报；不用未来状态冒充过去状态 |
| `test_v2_planner.py` | 计划编号与落库、避开死亡/昏迷人物、优先推进最久伏笔、按计划写作 |
| `test_v2_sweep.py` | 规则模式覆盖全书并复现埋设问题、复用已有抽取、看板聚合 |
| `test_v2_agent_tools.py` | 工具 schema、agent 循环、引用护栏、降级、轮数上限、agent 不写 Canon、迁移 |
| `test_v3_style.py` | 指标可复现、套话/均匀句式能识别、基线阈值、确定性改稿、指标对比方向 |
| `test_v3_invariants.py` | 7 类不变量逐条植入并检出，干净种子零误报 |
| `test_v3_revision.py` | 期限换算与逾期、承诺兑现/放弃、修订闭环（不恶化、无进展即停、落库） |
| `test_v4_fragments.py` | 碎片 CRUD 与状态流转、语义召回、原话留痕、对应关系双向校验、改稿磨平声音就回退 |
| `test_v5_quality.py` | 6 项节奏指标不误报已认可章节、6 条规则逐条检出、锁定后窗口收紧、漂移报告、断言核对三态、规则冲突覆盖同句的待确认、世界观规则判定 |
| `test_v6_wording.py` | 8 项用词指标、种子章节零误报、堆砌辞藻只在有既往文本时才判「飘」、总结/升华/过渡/抽象替代、桥段跨章计数、旧基线缺指标时显式提示 |
| `test_live_deepseek.py` | 真实模型：抽取与审校、问答、写作、规划、工具调用、扫描、碎片成文、改稿降 AI 味但不磨平作者、断言核对抓植入的幻觉、锁定文风与规则进提示、改稿真的删掉总结与升华、读感评审能自己点名总结式收尾 |

种子小说里故意埋的三处矛盾：

| 章节 | 埋了什么 | 该报什么 |
| --- | --- | --- |
| 第 14 章 | 「林默腰间的佩剑，换成了赤霄剑。」 | `FACT_CONFLICT`，证据 = 第 3 章 + 第 14 章 |
| 第 15 章 | 第 8 章已死的赵铁山现身说话出手 | `DEAD_CHARACTER_ACTIVE`，证据 = 第 8 章 + 第 15 章 |
| 第 18 章 | 「那桩血案，是天启三年三月里的事。」（Canon 是四月初五） | `TIMELINE_INVERSION`，证据 = 第 5 章 + 第 18 章 |

第 9 章（赵铁山墓前）和各章开篇的日期句是对照项，用来确认审校不会误报死者复现和时间线漂移。

## 界面

左边章节列表（能搜索跳转），中间正文编辑器（标题/故事时间/地点/字数/保存/完成本章），
右边 AI 助手，底部 11 个面板：人物、时间线、世界观、事件、伏笔、Canon、总览、规划、碎片、质量、QA。

- 审校面板逐条列问题、证据和改法，可以逐项接受/驳回，再点「应用确认项」落库。
- Canon 面板按状态过滤，确认/驳回 PROPOSED，显示每条事实的生效章和失效章；「时点视图」能看第 N 章时的世界状态。
- 总览面板：规则扫描或深度扫描、每章健康矩阵、错误类型统计、伏笔欠账、全局不变量、承诺逾期、
  文风基线与平均分、向量索引重建、混合检索试跑。
- 质量面板：建文风基线、评一段草稿、重跑不变量、维护承诺账本、设定断言核对（贴草稿或选章节，
  分成冲突/待确认/一致三组，逐条给原文片段和依据）、文风锁定与逐章漂移。
- 规划面板：生成接下来 N 章的计划、看伏笔回收建议、按计划写草稿或存成章节。
- 章节「文风」标签：对本章做规则层或「规则+模型读感」评审，列出历史评审。
- 写作面板：可勾「自动修订」，展示每轮分数、字数、文风/一致性 codes 与指标前后对比。
- QA 面板问自然语言问题，返回答案、置信度和证据清单；可以切到 Agent 模式让模型自己调工具，并回显调用明细。
- 窄屏（<1100px）自动改成单列堆叠，宽表格横向滚动。

## 现在还不靠谱的地方

这些是实打实的限制，用之前最好知道：

**模型输出会被截断。** 达到 `max_tokens` 时现在会显式报「输出被截断」（以前是静默丢内容：
抽取少几条、规划只写一半都可能看不出来）。抽取和规划的上限已经提到 8192 了，
但还是遇到截断就缩小范围重试。0.5 期间规划就因为 4096 上限反复失败过，
修复重试的提示词当时也没重申输出的顶层结构，所以两次都没救回来。

**用词那几个指标是近似量。** 用词多样性用汉字二元组在 200 字滑动窗口里算类符/形符比
（不想引分词器），它衡量的是「同一批词反复用」，跟语言学意义上的词汇量不是一回事。
「本书没用过的字占比」随着书往后写会系统性下降，所以只用绝对上限判「满篇生僻字」，
不参与逐章漂移统计。桥段同质化靠一张固定短语表，表外的套路识别不到。

**断言核对分两条通道，各有各的漏。** 确定性通道只认显式句式（「X 的佩剑是 Y」），
指代词（「这柄剑」）解析不了；模型通道能处理这类自由表述，但它只负责拆句，
判定全在 Python 里做，所以它拆不到的矛盾也报不出来。查无此设定一律是「待确认」——
宁可信你去确认，也不替你往 Canon 里写。

冲突判定偏保守：取值比对用二元组相似度 0.6 加包含关系，两个取值如果不像短实体名
（长句、带引号），冲突会降级成「请人工确认」而不是直接判冲突。少报一条假冲突，
比多报一条更有价值。

**世界观规则只有四类是结构化的。** 死者复生、唯一持有、能力门槛、不可变特质走确定性判定，
能力门槛按境界阶梯（炼气→筑基→金丹→元婴→化神）比，要求规则文本里写明境界名；
其余自由文本规则只能靠「相关且极性相反」的近似判断，会漏。

**文风那一堆指标都是启发式。** 动作词表、句式骨架、标点熵、情绪档位都写死在
`services/style_service.py` 里，做的是相对比较（跟这本书自己的基线比或者跟经验阈值比），
用途是排序和定位（哪一段最像注水），不是「AI 检测器」。
旧版本建的基线缺新指标时，现在会显式提示（`BASELINE_STALE` + 看板提示），
提醒你重建，而不是让新规则静默失效。

**文风锁定收的是比对窗口**（容差乘 0.5），不会让模型换个风格。锁定前确认那几章确实是你认可的稿子。

**本地向量是词形层面的 n-gram 哈希**，能容忍说法差异，但不是训练出来的语义模型。
要真正的语义召回就把 `NOVELOS_EMBEDDING_PROVIDER` 换成远程 embedding，业务代码不用改。
检索现在是全表读出来在内存里算余弦（实测 123 条记录 3ms、混合检索 11ms），
百万字约 3000 个片段估算百毫秒级；装了 numpy 会自动走矩阵路径，再往上就该换 sqlite-vec
或专用向量库了，接口隔离在 `services/vector_service.py`。

**离线提供者是确定性规则引擎**，只覆盖正文里显式写出的设定，谈不上语义理解；
它的「工具调用」也是脚本化的固定查询序列。真实抽取质量来自 DeepSeek Flash。

跨章复读检查默认只看当前章号之前的 60 章（四元组倒排索引，免得长篇上段落两两比较），
要查更早就调大 `review_chapter(prior_chapters=...)`。
全量扫描的 `rules` 模式不调模型，所以看不到语义级矛盾；`full` 模式逐章调，20 章要几分钟并产生费用。
人物状态冲突只认「死亡 / 重伤昏迷 / 失踪 / 闭关 / 被囚 / 封印」这类无法行动的状态，
更细的（比如「隐姓埋名」）得靠你补人物档案。
多 Agent 目前就是「单 Agent + 工具循环」，多人协作、权限、多租户、封面插图都不在这个范围里。

## 往后要解决的

1. 写作上下文预算。100 万字大概 400–500 章，现在给写手看的是相关人物的 Canon + 最近 3 章摘要
   + 语义 top5 片段 + 相关碎片，跨卷的伏笔、人物弧线、伏笔回收节奏都不够。想做分层记忆
   （章 → 卷 → 全书滚动摘要）加每章的场景状态简报（人在哪、手里有什么、谁知道什么）。
2. 规模化的人机协作。每章现在会产生 10–30 条待确认项，全得人工逐条审，500 章会把人拖死。
   想做风险分级（重复事实、低置信度、涉及死亡与修为的才拦）、批量审阅、快捷键。
3. 情节结构。有章末钩子、冲突密度和规划，但「黄金三章」「每 3–5 章一个小高潮」「卷末大高潮」
   这类网文结构约束还没有硬检查。
4. 跨章语义矛盾。现在靠不变量和抽样人读，想做定期抽样做「全书提问式审校」，
   再给同一 (subject, predicate) 的历史断言做一条时间轴。
5. 成本与吞吐。全流程同步执行，没有队列、并发、增量扫描和 prompt 缓存。20 章能忍，500 章不能。
6. 幻觉的量化。现在能逐条给冲突/待确认，但没有「每万字幻觉率」这种能长期看的指标。
7. 人物级口吻。现在只有整体声音画像，还没有「张三说话短促、李四爱绕」这种按人分的档案；
   想给人物档案加语癖字段（句长偏好、口头禅、称谓习惯），评审时分开校验。

衡量进展的话，这几个数系统现在就能算：每 10 万字的 error 数、承诺兑现率、
与文风基线的偏离度、套话密度与节奏指标。剩下两个（每章的人工审阅工时、读者留存）还没接，
一个只有条目数，一个需要真实读者数据。
