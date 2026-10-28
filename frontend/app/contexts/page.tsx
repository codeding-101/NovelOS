"use client";
import { FormEvent, useEffect, useRef, useState } from "react";
import { platformRequest } from "@/lib/platform";
import shell from "../platform.module.css";
import styles from "../budgets/budgets.module.css";
import local from "./contexts.module.css";
import { StateSummary, StoryState } from "../memory/StoryState";

type Work = { id: string; title: string };
type Asset = {
  id: string;
  title: string;
  revision_id: string;
  head_version: number;
};
type Memory = {
  id: string;
  title: string;
  entry_key: string;
  content: string;
  version: number;
  source_changed: boolean;
  story_state: StoryState | null;
};
type Choice = { proposal_id: string; title: string; required: boolean };
type Recommendations = {
  query: string;
  scanned: number;
  matched: number;
  scan_limit: number;
  items: { memory: Memory; score: number; matched_terms: string[] }[];
};
type Issue = { code: string; reference_id: string | null };
type Freshness = {
  fresh: boolean;
  issues: Issue[];
  checked_at: string;
  assembly_ready?: boolean;
};
type Snapshot = {
  id: string;
  title: string;
  input_revision_id: string;
  story_at: number | null;
  status: string;
  capacity_bytes: number;
  required_bytes: number;
  used_bytes: number;
  sha256: string;
  blockers: Issue[];
  created_at: string;
};
type Detail = Snapshot & {
  document: string;
  freshness: Freshness;
  memories: {
    proposal_id: string;
    title: string;
    entry_key: string;
    required: boolean;
    included: boolean;
    omission_reason: string | null;
  }[];
};
type Source = {
  revision_id: string;
  asset_id: string;
  number: number;
  sha256: string;
};
type Document = {
  objective: string;
  story_at: number | null;
  input: Source & { title: string; content: string };
  memories: {
    proposal_id: string;
    title: string;
    content: string;
    story_state: StoryState | null;
    sources: Source[];
  }[];
};
const reasons: Record<string, string> = {
  INPUT_CHANGED: "输入正文已有新版本",
  SOURCE_CHANGED: "记忆来源已有新版本",
  MEMORY_CHANGED: "正式记忆已被替代",
  STORY_TIME_MISMATCH: "记忆不适用于指定故事时点",
  REQUIRED_CAPACITY_EXCEEDED: "必需内容超过容量",
  OPTIONAL_CAPACITY_EXCEEDED: "可选内容因容量不足省略",
  REQUIRED_CONTEXT_BLOCKED: "必需内容阻断，本次未组装可选内容",
};
async function all<T>(path: string): Promise<T[]> {
  const rows: T[] = [];
  for (let offset = 0; ; offset += 100) {
    const page = await platformRequest<T[]>(
      `${path}?offset=${offset}&limit=100`,
    );
    rows.push(...page);
    if (page.length < 100) return rows;
  }
}

export default function ContextPage() {
  const [works, setWorks] = useState<Work[]>([]);
  const [workId, setWorkId] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [choices, setChoices] = useState<Choice[]>([]);
  const [query, setQuery] = useState("");
  const [recommendations, setRecommendations] =
    useState<Recommendations | null>(null);
  const [appliedQuery, setAppliedQuery] = useState("");
  const [memoryOffset, setMemoryOffset] = useState(0);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [check, setCheck] = useState<Freshness | null>(null);
  const [evidence, setEvidence] = useState<{
    title: string;
    content: string;
  } | null>(null);
  const [title, setTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [revision, setRevision] = useState("");
  const [storyAt, setStoryAt] = useState("");
  const [capacity, setCapacity] = useState("65536");
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState(true);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requestKey = useRef("");
  const busy = pending || expired;
  const prefix = `/works/${workId}/contexts`;
  const payload: Document | null = detail ? JSON.parse(detail.document) : null;
  useEffect(() => {
    setRecommendations(null);
  }, [workId, query, storyAt]);
  function edited(action: () => void) {
    action();
    requestKey.current = "";
    setDirty(true);
  }
  async function run(action: () => Promise<void>) {
    setPending(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setPending(false);
    }
  }
  useEffect(() => {
    void run(async () => setWorks(await all("/works")));
    const expire = () => setExpired(true);
    window.addEventListener("novelos-session-expired", expire);
    return () => window.removeEventListener("novelos-session-expired", expire);
  }, []);
  useEffect(() => {
    const protect = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty]);
  async function loadMemories(id: string, search = "", start = 0) {
    const rows = await platformRequest<Memory[]>(
      `/works/${id}/memory?q=${encodeURIComponent(search)}&offset=${start}&limit=20`,
    );
    setMemories(rows);
    setMemoryOffset(start);
    setAppliedQuery(search);
  }
  async function recommend() {
    setRecommendations(null);
    const at = storyAt === "" ? null : Number(storyAt);
    if (at !== null && (!Number.isSafeInteger(at) || at < 0 || at > 10 ** 12))
      throw new Error("目标故事时点须为 0 至 1000000000000 的整数。");
    const params = new URLSearchParams({ q: query.trim(), limit: "20" });
    if (at !== null) params.set("at", String(at));
    setRecommendations(
      await platformRequest<Recommendations>(
        `/works/${workId}/memory/retrieve?${params}`,
      ),
    );
  }
  async function loadSnapshots(id: string, start = 0) {
    setSnapshots(
      await platformRequest(`/works/${id}/contexts?offset=${start}&limit=20`),
    );
    setOffset(start);
  }
  async function select(id: string) {
    setDetail(await platformRequest(`${prefix}/${id}`));
    setCheck(null);
    setEvidence(null);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      if (!requestKey.current) requestKey.current = crypto.randomUUID();
      const created = await platformRequest<{ id: string }>(prefix, {
        method: "POST",
        body: JSON.stringify({
          title,
          objective,
          input_revision_id: revision,
          story_at: storyAt === "" ? null : Number(storyAt),
          capacity_bytes: Number(capacity),
          memories: choices.map(({ proposal_id, required }) => ({
            proposal_id,
            required,
          })),
          request_key: requestKey.current,
        }),
      });
      await select(created.id);
      await loadSnapshots(workId);
      setDirty(false);
      setNotice("快照已保存，请查看组装结果与省略清单；尚未发送给执行者。");
    });
  }
  async function preview(source: Source) {
    setEvidence(
      await platformRequest(
        `/works/${workId}/assets/${source.asset_id}/revisions/${source.revision_id}`,
      ),
    );
  }
  return (
    <main className={shell.shell}>
      <header className={shell.header}>
        <div className={shell.wordmark}>
          NovelOS<span>上下文快照</span>
        </div>
        <a href="/">返回控制台</a>
      </header>
      <h1>上下文快照</h1>
      <p>
        固定任务目标、正文和已批准记忆的版本。必需信息完整保留，容量不足时明确阻断；可选信息按选择顺序整条取舍并记录原因。
      </p>
      <p>
        本页用于准备与核对。容量按 UTF-8 字节计算，不等同于模型
        token。已组装且依据有效的快照可在任务中心绑定；自动摘要与真实模型调用尚未开放。
      </p>
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className={styles.notice}>
          {notice}
        </p>
      )}
      {expired && (
        <p role="alert">登录已过期，请在另一页面登录；当前输入保留。</p>
      )}
      <label className={styles.work}>
        当前作品
        <select
          aria-label="当前作品"
          disabled={busy}
          value={workId}
          onChange={(event) => {
            const id = event.target.value;
            if (
              !id ||
              (dirty && !window.confirm("切换作品会清除尚未保存的配置，继续？"))
            )
              return;
            void run(async () => {
              const [saved, official, history] = await Promise.all([
                all<Asset>(`/works/${id}/assets`),
                platformRequest<Memory[]>(`/works/${id}/memory?limit=20`),
                platformRequest<Snapshot[]>(`/works/${id}/contexts?limit=20`),
              ]);
              setMemories(official);
              setSnapshots(history);
              setMemoryOffset(0);
              setOffset(0);
              setAppliedQuery("");
              setWorkId(id);
              setAssets(saved);
              setChoices([]);
              setQuery("");
              setTitle("");
              setObjective("");
              setRevision("");
              setStoryAt("");
              setCapacity("65536");
              setDetail(null);
              setCheck(null);
              setEvidence(null);
              setDirty(false);
              requestKey.current = "";
            });
          }}
        >
          <option value="">选择作品</option>
          {works.map((w) => (
            <option key={w.id} value={w.id}>
              {w.title}
            </option>
          ))}
        </select>
      </label>
      {workId && (
        <>
          <form
            className={styles.panel}
            aria-label="上下文配置"
            onSubmit={submit}
          >
            <h2>组装新快照</h2>
            <div className={styles.fields}>
              <label>
                快照名称
                <input
                  aria-label="快照名称"
                  required
                  maxLength={256}
                  disabled={busy}
                  value={title}
                  onChange={(e) => edited(() => setTitle(e.target.value))}
                />
              </label>
              <label>
                输入正文版本
                <select
                  aria-label="输入正文版本"
                  required
                  disabled={busy}
                  value={revision}
                  onChange={(e) => edited(() => setRevision(e.target.value))}
                >
                  <option value="">选择已保存版本</option>
                  {assets.map((a) => (
                    <option key={a.id} value={a.revision_id}>
                      {a.title} · v{a.head_version}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                容量上限（字节）
                <input
                  aria-label="容量上限（字节）"
                  type="number"
                  min={1}
                  max={8388608}
                  step={1}
                  required
                  disabled={busy}
                  value={capacity}
                  onChange={(e) => edited(() => setCapacity(e.target.value))}
                />
              </label>
              <label>
                目标故事时点（可选）
                <input
                  aria-label="目标故事时点"
                  type="number"
                  min={0}
                  max={1000000000000}
                  step={1}
                  disabled={busy}
                  value={storyAt}
                  onChange={(e) => edited(() => setStoryAt(e.target.value))}
                />
              </label>
            </div>
            <label>
              任务目标
              <textarea
                aria-label="任务目标"
                required
                maxLength={4000}
                disabled={busy}
                value={objective}
                onChange={(e) => edited(() => setObjective(e.target.value))}
              />
            </label>
            <p>
              任务目标、完整正文及引用信息始终必需。时点留空表示不按故事时间筛选；未填写时点的文字记忆不会被自动推断。请选择任务确实需要的设定，平台尚不自动判断是否遗漏核心事实。
            </p>
            <h3>检索正式记忆</h3>
            <label>
              记忆关键词
              <input
                aria-label="记忆关键词"
                maxLength={256}
                disabled={busy}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <div className={styles.actions}>
              <button
                type="button"
                disabled={busy}
                onClick={() => void run(() => loadMemories(workId, query))}
              >
                检索可选记忆
              </button>
              <button
                type="button"
                disabled={busy || !query.trim()}
                onClick={() => void run(recommend)}
              >
                按相关性推荐
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  void run(async () =>
                    setAssets(await all(`/works/${workId}/assets`)),
                  )
                }
              >
                刷新正文版本
              </button>
            </div>
            <section aria-label="相关记忆推荐" className={local.wrap}>
              <h3>相关记忆推荐</h3>
              <p>
                按关键词和目标故事时点查找来源有效的正式记忆，最多展示前 20 条。
                分数只表示文字匹配程度；推荐不会自动加入快照，也不能保证找齐必要设定。
                修改关键词或时点后需重新推荐。下方字面检索列表独立保留。
              </p>
              {recommendations && (
                <>
                  <p role="status">
                    查询“{recommendations.query}”：检查{" "}
                    {recommendations.scanned} 条有效记忆， 匹配{" "}
                    {recommendations.matched} 条，展示{" "}
                    {recommendations.items.length} 条。
                  </p>
                  {!recommendations.items.length && (
                    <p>
                      没有相关的有效记忆，请调整关键词或核对来源与故事时点。
                    </p>
                  )}
                  {recommendations.items.map(
                    ({ memory: m, score, matched_terms }) => (
                      <article className={styles.record} key={m.id}>
                        <h4>
                          {m.title} · v{m.version}
                        </h4>
                        <p>
                          匹配分数：{score}；匹配词：{matched_terms.join("、")}
                        </p>
                        <pre className={local.text}>{m.content}</pre>
                        <StateSummary state={m.story_state} />
                        <button
                          type="button"
                          disabled={
                            busy ||
                            choices.length >= 50 ||
                            choices.some((c) => c.proposal_id === m.id)
                          }
                          onClick={() =>
                            edited(() =>
                              setChoices([
                                ...choices,
                                {
                                  proposal_id: m.id,
                                  title: m.title,
                                  required: true,
                                },
                              ]),
                            )
                          }
                        >
                          推荐加入 {m.title}
                        </button>
                      </article>
                    ),
                  )}
                  <p>
                    这些是查询时的结果，来源仍可能变化；保存快照时会重新核对。超过{" "}
                    {recommendations.scan_limit} 条有效记忆时，请使用字面检索。
                  </p>
                </>
              )}
            </section>
            {!memories.length && (
              <p>没有匹配的正式记忆。候选与驳回条目不会进入选择列表。</p>
            )}
            {memories.map((m) => (
              <article className={styles.record} key={m.id}>
                <h4>
                  {m.title} · v{m.version}
                </h4>
                <p>{m.entry_key}</p>
                <pre className={local.text}>{m.content}</pre>
                <StateSummary state={m.story_state} />
                {m.source_changed && (
                  <p>
                    来源已有新版本：作为必需内容会阻断，作为可选内容会省略。
                  </p>
                )}
                <button
                  type="button"
                  disabled={
                    busy ||
                    choices.length >= 50 ||
                    choices.some((c) => c.proposal_id === m.id)
                  }
                  onClick={() =>
                    edited(() =>
                      setChoices([
                        ...choices,
                        { proposal_id: m.id, title: m.title, required: true },
                      ]),
                    )
                  }
                >
                  加入快照 {m.title}
                </button>
              </article>
            ))}
            <div className={styles.actions}>
              <button
                type="button"
                disabled={busy || !memoryOffset}
                onClick={() =>
                  void run(() =>
                    loadMemories(workId, appliedQuery, memoryOffset - 20),
                  )
                }
              >
                记忆上一页
              </button>
              <button
                type="button"
                disabled={busy || memories.length < 20}
                onClick={() =>
                  void run(() =>
                    loadMemories(workId, appliedQuery, memoryOffset + 20),
                  )
                }
              >
                记忆下一页
              </button>
            </div>
            <section aria-label="已选记忆">
              <h3>已选记忆（{choices.length}/50）</h3>
              <p>
                必需记忆优先保留；其余按下列顺序尝试加入。删除条目会将其排除在本次选择范围之外。
              </p>
              {choices.map((choice, index) => (
                <div className={styles.record} key={choice.proposal_id}>
                  <p>
                    {index + 1}. {choice.title}
                  </p>
                  <label className={styles.check}>
                    <input
                      type="checkbox"
                      aria-label={`必需 ${choice.title}`}
                      checked={choice.required}
                      disabled={busy}
                      onChange={(e) =>
                        edited(() =>
                          setChoices(
                            choices.map((c) =>
                              c.proposal_id === choice.proposal_id
                                ? { ...c, required: e.target.checked }
                                : c,
                            ),
                          ),
                        )
                      }
                    />
                    必需内容
                  </label>
                  <div className={styles.actions}>
                    <button
                      type="button"
                      disabled={busy || !index}
                      onClick={() =>
                        edited(() => {
                          const rows = [...choices];
                          [rows[index - 1], rows[index]] = [
                            rows[index],
                            rows[index - 1],
                          ];
                          setChoices(rows);
                        })
                      }
                    >
                      前移 {choice.title}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        edited(() =>
                          setChoices(
                            choices.filter(
                              (c) => c.proposal_id !== choice.proposal_id,
                            ),
                          ),
                        )
                      }
                    >
                      移除 {choice.title}
                    </button>
                  </div>
                </div>
              ))}
            </section>
            <div className={styles.actions}>
              <button disabled={busy}>保存上下文快照</button>
            </div>
          </form>
          <section className={styles.panel} aria-label="快照历史">
            <h2>快照历史</h2>
            {!snapshots.length && <p>尚未保存快照。</p>}
            {snapshots.map((s) => (
              <div key={s.id} className={styles.record}>
                <button
                  disabled={busy}
                  onClick={() => void run(() => select(s.id))}
                >
                  查看快照 {s.title}
                </button>
                <p>
                  保存时：{s.status === "READY" ? "已组装" : "已阻断"} ·{" "}
                  {s.used_bytes} / {s.capacity_bytes} 字节 ·{" "}
                  {new Date(s.created_at).toLocaleString()}
                </p>
              </div>
            ))}
            <div className={styles.actions}>
              <button
                disabled={busy || !offset}
                onClick={() =>
                  void run(() => loadSnapshots(workId, offset - 20))
                }
              >
                快照上一页
              </button>
              <button
                disabled={busy || snapshots.length < 20}
                onClick={() =>
                  void run(() => loadSnapshots(workId, offset + 20))
                }
              >
                快照下一页
              </button>
              <button
                disabled={busy}
                onClick={() => void run(() => loadSnapshots(workId))}
              >
                刷新快照列表
              </button>
            </div>
          </section>
          {detail && payload && (
            <section className={styles.panel} aria-label="快照详情">
              <h2>{detail.title}</h2>
              <p>
                保存时状态：{detail.status === "READY" ? "已组装" : "已阻断"}
                。快照不可编辑，调整配置后保存为新快照。
              </p>
              <p>
                必需内容 {detail.required_bytes} 字节；组装内容{" "}
                {detail.used_bytes} 字节；容量 {detail.capacity_bytes}{" "}
                字节。计数包含正文、目标、记忆及其序列化引用信息。
              </p>
              <p className={local.wrap}>SHA-256：{detail.sha256}</p>
              <p>目标故事时点：{detail.story_at ?? "未指定"}</p>
              {!!detail.blockers.length && (
                <div aria-label="组装阻断原因">
                  {detail.blockers.map((i, n) => (
                    <p className={local.wrap} key={n}>
                      {reasons[i.code] || i.code} {i.reference_id}
                    </p>
                  ))}
                </div>
              )}
              <h3>依据复查</h3>
              <p>
                本次读取：
                {detail.freshness.fresh
                  ? "已纳入的版本依据未变化"
                  : "依据已变化，需重新组装"}
                。复查结果只代表检查时点，不是执行授权。
              </p>
              {detail.freshness.issues.map((i, n) => (
                <p key={n} className={local.wrap}>
                  {reasons[i.code]} {i.reference_id}
                </p>
              ))}
              <button
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const checked = await platformRequest<Freshness>(
                      `${prefix}/${detail.id}/check`,
                      { method: "POST" },
                    );
                    setDetail(await platformRequest(`${prefix}/${detail.id}`));
                    setCheck(checked);
                  })
                }
              >
                重新核对快照依据
              </button>
              {check && (
                <p role="status">
                  复查结果：
                  {check.assembly_ready
                    ? "组装条件通过"
                    : "组装条件不通过"} ·{" "}
                  {new Date(check.checked_at).toLocaleString()}
                </p>
              )}
              <h3>纳入与省略清单</h3>
              {!detail.memories.length && <p>本快照未选择正式记忆。</p>}
              {detail.memories.map((m) => (
                <p key={m.proposal_id} className={local.wrap}>
                  {m.title} · {m.required ? "必需" : "可选"} ·{" "}
                  {m.included
                    ? "完整纳入"
                    : `已省略：${reasons[m.omission_reason || ""]}`}
                </p>
              ))}
              <h3>任务目标</h3>
              <pre className={local.text}>{payload.objective}</pre>
              <h3>
                固定正文：{payload.input.title} · v{payload.input.number}
              </h3>
              <pre className={local.text}>{payload.input.content}</pre>
              <button
                disabled={busy}
                onClick={() => void run(() => preview(payload.input))}
              >
                查看输入原版本
              </button>
              {payload.memories.map((m) => (
                <article className={styles.record} key={m.proposal_id}>
                  <h3>{m.title}</h3>
                  <pre className={local.text}>{m.content}</pre>
                  <StateSummary state={m.story_state} />
                  {m.sources.map((s) => (
                    <div key={s.revision_id}>
                      <p className={local.wrap}>
                        来源 v{s.number} · SHA-256：{s.sha256}
                      </p>
                      <button
                        disabled={busy}
                        onClick={() => void run(() => preview(s))}
                      >
                        查看记忆来源 {m.title} v{s.number}
                      </button>
                    </div>
                  ))}
                </article>
              ))}
              {evidence && (
                <section aria-label="快照来源原文">
                  <h3>{evidence.title}</h3>
                  <pre className={local.text}>{evidence.content}</pre>
                </section>
              )}
              <details>
                <summary>完整快照文档（含引用信息）</summary>
                <pre className={local.text}>{detail.document}</pre>
              </details>
            </section>
          )}
        </>
      )}
    </main>
  );
}
