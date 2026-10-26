"use client";
import { FormEvent, useEffect, useRef, useState } from "react";
import { platformRequest } from "@/lib/platform";
import shell from "../platform.module.css";
import styles from "../budgets/budgets.module.css";
import local from "./memory.module.css";
import { StateFields, StateSummary, StoryState, Timeline } from "./StoryState";

type Source = {
  revision_id: string;
  asset_id: string;
  title: string;
  number: number;
  sha256: string;
  current: boolean;
};
type Proposal = {
  id: string;
  entry_key: string;
  kind: string;
  title: string;
  content?: string;
  base_proposal_id: string | null;
  submission_id: string | null;
  status: string;
  version: number | null;
  is_current: boolean;
  source_changed: boolean;
  blockers: string[];
  sources: Source[];
  expires_at: string;
  decision_reason: string | null;
  story_state: StoryState | null;
  story_conflicts: { id: string; entry_key: string; title: string }[];
};
type Asset = {
  id: string;
  title: string;
  revision_id: string;
  head_version: number;
};
const kinds: Record<string, string> = {
  character: "人物",
  relationship: "关系",
  world_rule: "世界规则",
  event: "事件",
  foreshadow: "伏笔",
  promise: "承诺",
};
const statuses: Record<string, string> = {
  pending: "待审核",
  approved: "已批准",
  rejected: "已驳回",
};
const reasons: Record<string, string> = {
  MEMORY_CHANGED: "正式记忆已更新",
  SOURCE_CHANGED: "来源已有新版本",
  PROPOSAL_EXPIRED: "提案已过期",
  STORY_STATE_OVERLAP: "同一对象和属性的故事区间重叠",
};
async function all<T>(path: string): Promise<T[]> {
  const result: T[] = [];
  for (let offset = 0; ; offset += 100) {
    const rows = await platformRequest<T[]>(
      `${path}?offset=${offset}&limit=100`,
    );
    result.push(...rows);
    if (rows.length < 100) return result;
  }
}

export default function MemoryPage() {
  const [works, setWorks] = useState<{ id: string; title: string }[]>([]);
  const [workId, setWorkId] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [canon, setCanon] = useState<Proposal[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [history, setHistory] = useState<Proposal[]>([]);
  const [detail, setDetail] = useState<Proposal | null>(null);
  const [before, setBefore] = useState<Proposal | null>(null);
  const [evidence, setEvidence] = useState<{
    title: string;
    content: string;
  } | null>(null);
  const [key, setKey] = useState("");
  const [kind, setKind] = useState("character");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [storyState, setStoryState] = useState<StoryState | null>(null);
  const [timelineEpoch, setTimelineEpoch] = useState(0);
  const [sources, setSources] = useState<string[]>([]);
  const [base, setBase] = useState<string | null>(null);
  const [submission, setSubmission] = useState("");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [status, setStatus] = useState("pending");
  const [offset, setOffset] = useState(0);
  const [canonOffset, setCanonOffset] = useState(0);
  const [pending, setPending] = useState(true);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requestKey = useRef("");
  const busy = pending || expired;
  const dirty = Boolean(
    key || title || content || reason || submission || storyState,
  );
  const prefix = `/works/${workId}/memory`;
  function mayLeave() {
    return !dirty || window.confirm("尚未提交的输入会被清除，继续？");
  }
  function resetForm() {
    setKey("");
    setTitle("");
    setContent("");
    setStoryState(null);
    setSources([]);
    setBase(null);
    setSubmission("");
    requestKey.current = "";
  }
  function edited(action: () => void) {
    requestKey.current = "";
    action();
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
    function protect(event: BeforeUnloadEvent) {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty]);
  async function load(
    id: string,
    filter = status,
    start = 0,
    search = appliedQuery,
    canonicalStart = canonOffset,
  ) {
    const path = `/works/${id}/memory`;
    const [current, rows, saved] = await Promise.all([
      platformRequest<Proposal[]>(
        `${path}?q=${encodeURIComponent(search)}&offset=${canonicalStart}&limit=20`,
      ),
      platformRequest<Proposal[]>(
        `${path}/proposals?status=${filter}&offset=${start}&limit=20`,
      ),
      all<Asset>(`/works/${id}/assets`),
    ]);
    setCanon(current);
    setProposals(rows);
    setAssets(saved);
    setStatus(filter);
    setOffset(start);
    setCanonOffset(canonicalStart);
    setAppliedQuery(search);
    setTimelineEpoch((value) => value + 1);
  }
  async function selectProposal(id: string) {
    const selected = await platformRequest<Proposal>(
      `${prefix}/proposals/${id}`,
    );
    const prior = selected.base_proposal_id
      ? await platformRequest<Proposal>(
          `${prefix}/proposals/${selected.base_proposal_id}`,
        )
      : null;
    setDetail(selected);
    setBefore(prior);
    setReason("");
    setConfirmed(false);
    setEvidence(null);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      if (!requestKey.current) requestKey.current = crypto.randomUUID();
      const result = await platformRequest<{ id: string }>(
        `${prefix}/proposals`,
        {
          method: "POST",
          body: JSON.stringify({
            entry_key: key,
            kind,
            title,
            content,
            story_state: storyState,
            source_revision_ids: sources,
            base_proposal_id: base,
            submission_id: submission || null,
            request_key: requestKey.current,
          }),
        },
      );
      resetForm();
      await load(workId, "pending", 0);
      await selectProposal(result.id);
      setNotice("候选记忆已保存，审核前不会进入正式记忆。");
    });
  }
  function decide(outcome: string) {
    if (!detail) return;
    void run(async () => {
      await platformRequest(`${prefix}/proposals/${detail.id}/decision`, {
        method: "POST",
        body: JSON.stringify({ outcome, reason }),
      });
      await load(workId);
      await selectProposal(detail.id);
      setHistory([]);
      setNotice(
        outcome === "approved"
          ? "审核已通过，正式记忆已更新；正文与定稿状态未改变。"
          : "提案已驳回，原记录保留。",
      );
    });
  }
  return (
    <main className={shell.shell}>
      <header className={shell.header}>
        <div className={shell.wordmark}>
          NovelOS<span>记忆与审核</span>
        </div>
        <a href="/">返回控制台</a>
      </header>
      <h1>记忆与审核</h1>
      <p>
        候选与正式记忆分开保存。当前所有记忆变更均须用户审核；来源变更、正式版本变化或提案过期时不能直接生效。
      </p>
      <p>
        可为记忆填写结构化故事状态及生效区间；时点由用户约定，不自动推断或抽取。
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
        <p role="alert">登录已过期，请在另一页面重新登录；当前输入保留。</p>
      )}
      <label className={styles.work}>
        当前作品
        <select
          aria-label="当前作品"
          disabled={busy}
          value={workId}
          onChange={(e) => {
            const id = e.target.value;
            if (!id || !mayLeave()) return;
            void run(async () => {
              await load(id, "pending", 0, "", 0);
              setWorkId(id);
              resetForm();
              setQuery("");
              setDetail(null);
              setBefore(null);
              setEvidence(null);
              setReason("");
              setHistory([]);
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
            onSubmit={submit}
            className={styles.panel}
            aria-label="记忆提案表单"
          >
            <h2>{base ? "提议修改正式记忆" : "新建记忆提案"}</h2>
            <div className={styles.fields}>
              <label>
                记忆标识
                <input
                  required
                  maxLength={128}
                  disabled={busy || !!base}
                  value={key}
                  onChange={(e) => edited(() => setKey(e.target.value))}
                />
              </label>
              <label>
                记忆类型
                <select
                  aria-label="记忆类型"
                  disabled={busy}
                  value={kind}
                  onChange={(e) => edited(() => setKind(e.target.value))}
                >
                  {Object.entries(kinds).map(([id, label]) => (
                    <option key={id} value={id}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                记忆标题
                <input
                  required
                  maxLength={256}
                  disabled={busy}
                  value={title}
                  onChange={(e) => edited(() => setTitle(e.target.value))}
                />
              </label>
            </div>
            <p>
              同一作品使用稳定标识区分条目，例如“人物-林舟-身份”。修改已有条目请从下方正式记忆选择“提出修改”。
            </p>
            <label>
              记忆内容
              <textarea
                aria-label="记忆内容"
                required
                maxLength={32768}
                disabled={busy}
                value={content}
                onChange={(e) => edited(() => setContent(e.target.value))}
              />
            </label>
            <StateFields
              state={storyState}
              disabled={busy}
              onChange={(value) => edited(() => setStoryState(value))}
            />
            <label>
              来源版本（可多选，最多 20 项）
              <select
                multiple
                size={5}
                aria-label="记忆来源版本"
                disabled={busy}
                value={sources}
                onChange={(e) =>
                  edited(() =>
                    setSources(
                      Array.from(e.target.selectedOptions).map((o) => o.value),
                    ),
                  )
                }
              >
                {assets.map((a) => (
                  <option key={a.id} value={a.revision_id}>
                    {a.title} · v{a.head_version}
                  </option>
                ))}
              </select>
            </label>
            <label>
              任务候选 ID（可选）
              <input
                maxLength={32}
                disabled={busy}
                value={submission}
                onChange={(e) => edited(() => setSubmission(e.target.value))}
              />
            </label>
            <p>
              只可关联同作品已接收的任务候选，且须选中其输入版本。提案保存后不可编辑，7
              天内审核；失效或驳回后应另建提案。
            </p>
            <div className={styles.actions}>
              <button disabled={busy || !sources.length || sources.length > 20}>
                提交记忆提案
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  if (mayLeave()) resetForm();
                }}
              >
                清空提案表单
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
                刷新来源版本
              </button>
            </div>
          </form>
          <section className={styles.panel} aria-label="正式记忆">
            <h2>正式记忆</h2>
            <div className={styles.fields}>
              <label>
                检索正式记忆
                <input
                  disabled={busy}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
              </label>
            </div>
            <div className={styles.actions}>
              <button
                disabled={busy}
                onClick={() =>
                  void run(() => load(workId, status, offset, query, 0))
                }
              >
                检索记忆
              </button>
            </div>
            {!canon.length && (
              <p>没有符合条件的正式记忆。待审核和已驳回提案不会出现在这里。</p>
            )}
            {canon.map((item) => (
              <article key={item.id} className={styles.record}>
                <h3>
                  {item.title} · v{item.version}
                </h3>
                <p>
                  {kinds[item.kind]} · {item.entry_key}
                </p>
                <pre className={local.text}>{item.content}</pre>
                <StateSummary state={item.story_state} />
                {item.source_changed && (
                  <p>来源已有新版本，需复查；当前记忆保留原审批及证据。</p>
                )}
                <div className={styles.actions}>
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (!mayLeave()) return;
                      edited(() => {
                        setBase(item.id);
                        setKey(item.entry_key);
                        setKind(item.kind);
                        setTitle(item.title);
                        setContent(item.content || "");
                        setStoryState(item.story_state);
                        setSources(
                          item.sources
                            .filter((s) => s.current)
                            .map((s) => s.revision_id),
                        );
                        setSubmission("");
                      });
                    }}
                  >
                    提出修改 {item.title}
                  </button>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        setHistory(
                          await platformRequest(
                            `${prefix}/history?entry_key=${encodeURIComponent(item.entry_key)}&limit=100`,
                          ),
                        );
                      })
                    }
                  >
                    版本历史 {item.title}
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (
                        reason &&
                        !window.confirm("切换会清除未提交的审核说明，继续？")
                      )
                        return;
                      void run(() => selectProposal(item.id));
                    }}
                  >
                    查看依据 {item.title}
                  </button>
                </div>
              </article>
            ))}
            <div className={styles.actions}>
              <button
                disabled={busy || canonOffset === 0}
                onClick={() =>
                  void run(() =>
                    load(
                      workId,
                      status,
                      offset,
                      appliedQuery,
                      canonOffset - 20,
                    ),
                  )
                }
              >
                正式记忆上一页
              </button>
              <button
                disabled={busy || canon.length < 20}
                onClick={() =>
                  void run(() =>
                    load(
                      workId,
                      status,
                      offset,
                      appliedQuery,
                      canonOffset + 20,
                    ),
                  )
                }
              >
                正式记忆下一页
              </button>
            </div>
          </section>
          <Timeline
            key={`${workId}:${timelineEpoch}`}
            workId={workId}
            disabled={busy}
          />
          {!!history.length && (
            <section className={styles.panel} aria-label="记忆版本历史">
              <h2>最近 100 个已批准版本</h2>
              {history.map((item) => (
                <div className={styles.record} key={item.id}>
                  <h3>
                    v{item.version} ·{" "}
                    {item.is_current ? "当前版本" : "历史版本"}
                  </h3>
                  <pre className={local.text}>{item.content}</pre>
                  <StateSummary state={item.story_state} />
                  <p>审核说明：{item.decision_reason}</p>
                </div>
              ))}
            </section>
          )}
          <section className={styles.panel} aria-label="提案列表">
            <h2>记忆提案</h2>
            <label>
              审核状态
              <select
                aria-label="审核状态"
                disabled={busy}
                value={status}
                onChange={(e) =>
                  void run(() => load(workId, e.target.value, 0))
                }
              >
                <option value="pending">待审核</option>
                <option value="approved">已批准</option>
                <option value="rejected">已驳回</option>
                <option value="all">全部</option>
              </select>
            </label>
            {!proposals.length && <p>此状态下暂无提案。</p>}
            {proposals.map((item) => (
              <div className={styles.record} key={item.id}>
                <button
                  disabled={busy}
                  onClick={() => {
                    if (
                      reason &&
                      !window.confirm("切换会清除未提交的审核说明，继续？")
                    )
                      return;
                    void run(() => selectProposal(item.id));
                  }}
                >
                  审核提案 {item.title}
                </button>
                <p>
                  {statuses[item.status]}
                  {item.blockers.length
                    ? ` · ${item.blockers.map((x) => reasons[x]).join("、")}`
                    : ""}
                </p>
              </div>
            ))}
            <div className={styles.actions}>
              <button
                disabled={busy || offset === 0}
                onClick={() =>
                  void run(() => load(workId, status, offset - 20))
                }
              >
                提案上一页
              </button>
              <button
                disabled={busy || proposals.length < 20}
                onClick={() =>
                  void run(() => load(workId, status, offset + 20))
                }
              >
                提案下一页
              </button>
            </div>
          </section>
          {detail && (
            <section className={styles.panel} aria-label="提案审核详情">
              <h2>{detail.title}</h2>
              <p>
                {statuses[detail.status]} · {detail.entry_key} · 有效期至{" "}
                {new Date(detail.expires_at).toLocaleString()}
              </p>
              {before && (
                <>
                  <h3>修改前</h3>
                  <pre className={local.text}>{before.content}</pre>
                  <StateSummary state={before.story_state} />
                </>
              )}
              <h3>提案内容</h3>
              <pre className={local.text}>{detail.content}</pre>
              <StateSummary state={detail.story_state} />
              {!!detail.story_conflicts.length && (
                <p className={local.stateSummary}>
                  重叠条目（最多显示 20 项）：
                  {detail.story_conflicts
                    .map((row) => `${row.title}（${row.entry_key}）`)
                    .join("、")}
                </p>
              )}
              {!!detail.blockers.length && (
                <p role="status">
                  不能批准：{detail.blockers.map((x) => reasons[x]).join("、")}
                  。请基于最新内容重新提交。
                </p>
              )}
              <h3>来源证据</h3>
              {detail.sources.map((s) => (
                <div key={s.revision_id} className={styles.record}>
                  <p>
                    {s.title} · v{s.number} ·{" "}
                    {s.current ? "当前来源" : "来源已更新"}
                  </p>
                  <p>SHA-256：{s.sha256}</p>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void run(async () =>
                        setEvidence(
                          await platformRequest(
                            `/works/${workId}/assets/${s.asset_id}/revisions/${s.revision_id}`,
                          ),
                        ),
                      )
                    }
                  >
                    查看来源 {s.title} v{s.number}
                  </button>
                </div>
              ))}
              {evidence && (
                <section aria-label="来源原文">
                  <h3>{evidence.title}</h3>
                  <pre className={local.text}>{evidence.content}</pre>
                </section>
              )}
              {detail.decision_reason && (
                <p>审核说明：{detail.decision_reason}</p>
              )}
              {detail.status === "pending" && (
                <>
                  <label>
                    审核说明
                    <textarea
                      aria-label="审核说明"
                      disabled={busy}
                      maxLength={4096}
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                    />
                  </label>
                  <label className={styles.check}>
                    <input
                      type="checkbox"
                      disabled={busy}
                      checked={confirmed}
                      onChange={(e) => setConfirmed(e.target.checked)}
                    />
                    已核对提案、来源及修改前内容，同意其作为正式记忆
                  </label>
                  <div className={styles.actions}>
                    <button
                      disabled={
                        busy ||
                        !reason.trim() ||
                        !confirmed ||
                        !!detail.blockers.length
                      }
                      onClick={() => decide("approved")}
                    >
                      批准并生效
                    </button>
                    <button
                      disabled={busy || !reason.trim()}
                      onClick={() => decide("rejected")}
                    >
                      驳回提案
                    </button>
                  </div>
                </>
              )}
              <div className={styles.actions}>
                <button
                  disabled={busy}
                  onClick={() => {
                    if (
                      reason &&
                      !window.confirm("刷新会清除未提交的审核说明，继续？")
                    )
                      return;
                    void run(() => selectProposal(detail.id));
                  }}
                >
                  刷新提案状态
                </button>
              </div>
            </section>
          )}
        </>
      )}
    </main>
  );
}
