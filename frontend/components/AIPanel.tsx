"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  StyleReviewView,
  formatMetric,
  formatScore,
  metricLabel,
  scoreClass,
} from "@/components/StyleReviewView";
import type {
  Bible,
  ContinuityReport,
  ExtractionRun,
  Fragment,
  RevisionResponse,
  StyleReview,
  StyleReviewSummary,
  WriteChapterResponse,
} from "@/lib/types";

const KIND_LABELS: Record<string, string> = {
  CHARACTER_STATE: "人物状态",
  EVENT: "事件",
  TIMELINE: "时间线",
  FACT: "新事实",
  FORESHADOWING: "伏笔",
  RELATIONSHIP: "关系",
  LOCATION: "地点",
};

const REVIEW_STATUS_LABELS: Record<string, string> = {
  PENDING: "待确认",
  ACCEPTED: "已接受",
  REJECTED: "已驳回",
};

interface Props {
  novelId: string;
  chapterId: string | null;
  chapterNumber: number | null;
  nextChapterNumber: number;
  provider: string;
  bible: Bible | null;
  storedReport: ContinuityReport | null;
  /** 碎片被改动过的次数，变化时重新读取与本章相关的碎片。 */
  fragmentsVersion: number;
  onFragmentsChanged: () => void;
  ensureSaved: () => Promise<boolean>;
  onRefresh: () => Promise<void>;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

export function AIPanel({
  novelId,
  chapterId,
  chapterNumber,
  nextChapterNumber,
  provider,
  bible,
  storedReport,
  fragmentsVersion,
  onFragmentsChanged,
  ensureSaved,
  onRefresh,
  setStatus,
  setError,
}: Props) {
  const [tab, setTab] = useState<"review" | "writer" | "style">("review");
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<ExtractionRun | null>(null);
  const [report, setReport] = useState<ContinuityReport | null>(null);
  const [applyLog, setApplyLog] = useState<string>("");
  const [draft, setDraft] = useState<WriteChapterResponse | null>(null);

  // 文风评审
  const [styleReview, setStyleReview] = useState<StyleReview | null>(null);
  const [styleHistory, setStyleHistory] = useState<StyleReviewSummary[]>([]);
  const [styleBusy, setStyleBusy] = useState(false);

  // 写作表单
  const [goals, setGoals] = useState("");
  const [mustInclude, setMustInclude] = useState("");
  const [forbidden, setForbidden] = useState("");
  const [targetWords, setTargetWords] = useState(1200);
  const [writeCharacters, setWriteCharacters] = useState<string[]>([]);
  // 修订闭环：勾选后「生成草稿」改走 revise-chapter
  const [autoRevise, setAutoRevise] = useState(false);
  const [revision, setRevision] = useState<RevisionResponse | null>(null);

  // 想法碎片：安排给本章的 + 还没安排的
  const [chapterFragments, setChapterFragments] = useState<Fragment[]>([]);
  const [inboxFragments, setInboxFragments] = useState<Fragment[]>([]);
  const [fragmentLoadError, setFragmentLoadError] = useState("");
  const [mustFragments, setMustFragments] = useState<string[]>([]);
  const [placingFragment, setPlacingFragment] = useState<string | null>(null);

  const loadFragments = useCallback(async () => {
    if (!chapterNumber) {
      setChapterFragments([]);
      setInboxFragments([]);
      return;
    }
    try {
      const [placed, inbox] = await Promise.all([
        api.fragments(novelId, { targetChapter: chapterNumber, limit: 20 }),
        api.fragments(novelId, { unplacedOnly: true, limit: 20 }),
      ]);
      setChapterFragments(placed.slice(0, 5));
      setInboxFragments(inbox.slice(0, 5));
      setFragmentLoadError("");
    } catch (error) {
      setChapterFragments([]);
      setInboxFragments([]);
      setFragmentLoadError(error instanceof Error ? error.message : String(error));
    }
  }, [novelId, chapterNumber]);

  useEffect(() => {
    void loadFragments();
  }, [loadFragments, fragmentsVersion]);

  async function placeToThisChapter(fragment: Fragment) {
    if (!chapterNumber) {
      setError("请先在左侧选择一个章节");
      return;
    }
    setPlacingFragment(fragment.id);
    setError("");
    try {
      await api.placeFragment(fragment.id, chapterNumber);
      setStatus(
        `已安排到第${chapterNumber}章：${fragment.title || fragment.text.slice(0, 20)}`,
      );
      await loadFragments();
      onFragmentsChanged();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setPlacingFragment(null);
    }
  }

  function toggleMustFragment(id: string) {
    setMustFragments((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    );
  }

  /** 两组碎片合并去重：安排给本章的和未安排的在界面上是两份列表，但勾选状态共用。 */
  const pickableFragments = useMemo(() => {
    const seen = new Set<string>();
    return [...chapterFragments, ...inboxFragments].filter((fragment) => {
      if (seen.has(fragment.id)) return false;
      seen.add(fragment.id);
      return true;
    });
  }, [chapterFragments, inboxFragments]);

  /** 勾选的碎片正文直接附进 must_include，写作时要求优先体现。 */
  function withMustFragments(base: string): string[] {
    const texts = pickableFragments
      .filter((fragment) => mustFragments.includes(fragment.id))
      .map((fragment) => fragment.text);
    return [...splitList(base), ...texts];
  }

  const loadStyleHistory = useCallback(async () => {
    if (!chapterNumber) {
      setStyleHistory([]);
      return;
    }
    try {
      setStyleHistory(await api.listStyleReviews(novelId, { chapterNumber, limit: 10 }));
    } catch {
      setStyleHistory([]);
    }
  }, [novelId, chapterNumber]);

  useEffect(() => {
    setStyleReview(null);
    void loadStyleHistory();
  }, [loadStyleHistory]);

  async function guard(action: () => Promise<void>) {
    if (!chapterId) {
      setError("请先在左侧选择一个章节");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (!(await ensureSaved())) return;
      await action();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function runExtract() {
    await guard(async () => {
      setStatus("正在运行 ExtractorAgent…");
      const result = await api.extract(chapterId as string, provider);
      setRun(result);
      setApplyLog("");
      setStatus(`抽取完成：${result.items.length} 条待确认（provider=${result.provider}）`);
      await onRefresh();
    });
  }

  async function runContinuity() {
    await guard(async () => {
      setStatus("正在运行 ContinuityChecker…");
      const result = await api.continuity(chapterId as string, provider);
      setReport(result);
      setStatus(`审校完成：${result.errors.length} 个错误、${result.warnings.length} 个警告`);
      await onRefresh();
    });
  }

  async function reviewItem(itemId: string, status: "ACCEPTED" | "REJECTED") {
    await guard(async () => {
      await api.reviewItem(itemId, status);
      const refreshed = await api.runs(novelId, chapterId as string);
      setRun(refreshed[0] ?? null);
      await onRefresh();
    });
  }

  async function applyRun() {
    if (!run) return;
    await guard(async () => {
      const result = await api.applyRun(run.id, false);
      setApplyLog(
        `${result.message}｜写入 ${result.applied.length} 条，跳过 ${result.skipped.length} 条` +
          (result.skipped.length
            ? "（跳过原因：" +
              result.skipped
                .map((item) => String(item.reason ?? ""))
                .slice(0, 3)
                .join("；") +
              "）"
            : ""),
      );
      const refreshed = await api.runs(novelId, chapterId as string);
      setRun(refreshed[0] ?? null);
      await onRefresh();
    });
  }

  async function acceptAllPending() {
    if (!run) return;
    await guard(async () => {
      const pending = run.items.filter((item) => item.review_status === "PENDING");
      for (const item of pending) {
        await api.reviewItem(item.id, "ACCEPTED");
      }
      const refreshed = await api.runs(novelId, chapterId as string);
      setRun(refreshed[0] ?? null);
      setStatus(`已接受 ${pending.length} 条待确认项，可点击「应用确认项」写库`);
    });
  }

  async function generateChapter() {
    if (!goals.trim()) {
      setError("请先填写本章目标");
      return;
    }
    setBusy(true);
    setError("");
    try {
      setStatus("ChapterWriter 正在检索 Canon 并生成草稿…");
      const result = await api.writeChapter(
        novelId,
        {
          goals,
          must_include: withMustFragments(mustInclude),
          forbidden: splitList(forbidden),
          characters: writeCharacters,
          chapter_number: nextChapterNumber,
          target_words: targetWords,
        },
        provider,
      );
      setDraft(result);
      setRevision(null);
      setStatus(`生成完成：${result.draft.word_count} 字（provider=${result.provider}）`);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  /** 写作 → 文风/一致性评审 → 自动改稿；这里只生成草稿，落库仍由「另存为新章节」完成。 */
  async function reviseChapter() {
    if (!goals.trim()) {
      setError("请先填写本章目标");
      return;
    }
    setBusy(true);
    setError("");
    try {
      setStatus("修订闭环运行中：写作 → 评审 → 改稿（会多次调用模型）…");
      const result = await api.reviseChapter(
        novelId,
        {
          goals,
          must_include: withMustFragments(mustInclude),
          forbidden: splitList(forbidden),
          characters: writeCharacters,
          chapter_number: nextChapterNumber,
          target_words: targetWords,
          save: false,
          max_rounds: 2,
          target_score: 85,
          use_model_critic: true,
        },
        provider,
      );
      setRevision(result);
      setDraft(null);
      setStatus(
        `自动修订完成：final_score=${formatScore(result.final_score)}，共 ${result.rounds.length} 轮，` +
          `${result.accepted ? "已达标" : "未达标"}（provider=${result.provider}）`,
      );
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function runStyleReview(useModel: boolean) {
    await guard(async () => {
      setStyleBusy(true);
      setStatus(useModel ? "正在做文风评审（规则指标 + 模型读感）…" : "正在做文风评审（仅规则层）…");
      try {
        const review = await api.reviewChapterStyle(chapterId as string, {
          useModel,
          persist: true,
        });
        setStyleReview(review);
        setStatus(
          `文风评审完成：score=${formatScore(review.score)}，${review.issues.length} 条提示` +
            `（provider=${review.provider}）`,
        );
        await onRefresh();
        await loadStyleHistory();
      } finally {
        setStyleBusy(false);
      }
    });
  }

  async function saveDraftAsChapter() {
    const pending = revision?.draft ?? draft?.draft ?? null;
    if (!pending) return;
    setBusy(true);
    try {
      await api.createChapter(novelId, {
        title: pending.title,
        content: pending.content,
        summary: revision ? "由修订闭环自动改稿生成" : "由 ChapterWriter 生成的草稿",
        story_time: pending.story_time,
        location: pending.location,
        status: "DRAFT",
      });
      setStatus("草稿已另存为新章节（状态：草稿）");
      setDraft(null);
      setRevision(null);
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="column">
      <div className="column-header">
        AI 助手
        <span className="spacer" />
        <span className="badge">{provider}</span>
      </div>
      <div className="tabs" style={{ padding: "6px 8px 0" }}>
        <div className={`tab ${tab === "review" ? "active" : ""}`} onClick={() => setTab("review")}>
          抽取与审校
        </div>
        <div className={`tab ${tab === "writer" ? "active" : ""}`} onClick={() => setTab("writer")}>
          章节写作
        </div>
        <div className={`tab ${tab === "style" ? "active" : ""}`} onClick={() => setTab("style")}>
          文风
        </div>
      </div>
      <div className="column-body">
        {!chapterId && <div className="hint">选择章节后可使用 AI 功能。</div>}

        {chapterId && tab === "review" && (
          <>
            <div className="field-row" style={{ marginBottom: 8 }}>
              <button onClick={runExtract} disabled={busy}>
                运行抽取
              </button>
              <button onClick={runContinuity} disabled={busy}>
                一致性检查
              </button>
              <button onClick={applyRun} disabled={busy || !run}>
                应用确认项
              </button>
              <button onClick={acceptAllPending} disabled={busy || !run}>
                全部接受
              </button>
            </div>
            {applyLog && <div className="hint">{applyLog}</div>}

            {!report && storedReport && storedReport.report_id && (
              <div className="hint">
                下面是本章上一次审校结果（{storedReport.provider}/{storedReport.model}，
                {storedReport.created_at?.slice(0, 19).replace("T", " ")}）
              </div>
            )}
            {(report ?? storedReport) && renderIssues(report ?? (storedReport as ContinuityReport))}

            {run && (
              <div>
                <div className="hint">
                  抽取任务 {run.id}（{run.provider}/{run.model}，状态 {run.status}）
                  {run.warnings.length > 0 && `；提示：${run.warnings.join("；")}`}
                </div>
                {run.items.length === 0 && <div className="hint">本章未抽取到任何条目。</div>}
                {run.items.map((item) => (
                  <div key={item.id} className="item-row">
                    <div className="row-head">
                      <span className="badge">{KIND_LABELS[item.kind] || item.kind}</span>
                      <span
                        className={`badge ${
                          item.review_status === "ACCEPTED"
                            ? "canon"
                            : item.review_status === "REJECTED"
                              ? "rejected"
                              : "proposed"
                        }`}
                      >
                        {REVIEW_STATUS_LABELS[item.review_status]}
                      </span>
                      <span className="spacer" />
                      <button
                        onClick={() => reviewItem(item.id, "ACCEPTED")}
                        disabled={busy || item.review_status === "ACCEPTED"}
                      >
                        接受
                      </button>
                      <button
                        onClick={() => reviewItem(item.id, "REJECTED")}
                        disabled={busy || item.review_status === "REJECTED"}
                      >
                        驳回
                      </button>
                    </div>
                    <div className="payload">{describePayload(item.kind, item.payload)}</div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        {chapterId && tab === "writer" && (
          <>
            <div className="hint">
              写作前系统会先检索相关 Canon（人物状态、事实、时间线、未回收伏笔、前文摘要），
              检索快照会随生成记录一并保存。
            </div>

            <div className="issue" style={{ marginTop: 6 }}>
              <div className="issue-title">这一章的想法碎片</div>
              <div className="hint">
                碎片是你自己的素材：安排给这一章的会优先体现，写成正文后可在「碎片」标签里
                核对哪句话变成了哪段正文。第 {chapterNumber ?? "?"} 章相关：
              </div>
              {chapterFragments.length === 0 && (
                <div className="hint">还没有为这一章安排碎片。</div>
              )}
              {chapterFragments.map((fragment) => (
                <div key={fragment.id} className="item-row">
                  <div className="row-head">
                    <span className="badge">
                      {fragment.target_chapter ? `第${fragment.target_chapter}章` : "未安排"}
                    </span>
                    <span className="badge">P{fragment.priority}</span>
                    <b>{fragment.title || fragment.text.slice(0, 20)}</b>
                    <span className="spacer" />
                    <button
                      onClick={() => void placeToThisChapter(fragment)}
                      disabled={placingFragment !== null || fragment.target_chapter === chapterNumber}
                    >
                      {fragment.target_chapter === chapterNumber ? "已在本章" : "安排到本章"}
                    </button>
                  </div>
                  <div className="payload fragment-text">{fragment.text}</div>
                  {fragment.intent && <div className="payload">意图：{fragment.intent}</div>}
                </div>
              ))}
              <div className="hint" style={{ marginTop: 6 }}>
                还没安排的碎片（前 5 条）：
              </div>
              {inboxFragments.length === 0 && <div className="hint">没有未安排的碎片。</div>}
              {inboxFragments.map((fragment) => (
                <div key={fragment.id} className="item-row">
                  <div className="row-head">
                    <span className="badge">未安排</span>
                    <span className="badge">P{fragment.priority}</span>
                    <b>{fragment.title || fragment.text.slice(0, 20)}</b>
                    <span className="spacer" />
                    <button
                      onClick={() => void placeToThisChapter(fragment)}
                      disabled={placingFragment !== null}
                    >
                      {placingFragment === fragment.id ? "安排中…" : "安排到本章"}
                    </button>
                  </div>
                  <div className="payload fragment-text">{fragment.text}</div>
                </div>
              ))}
              {fragmentLoadError && (
                <div className="hint">碎片读取失败：{fragmentLoadError}</div>
              )}
            </div>

            <textarea
              placeholder="本章目标（必填）"
              value={goals}
              onChange={(event) => setGoals(event.target.value)}
              rows={3}
              style={{ width: "100%", marginTop: 6 }}
            />
            <div className="field-row" style={{ marginTop: 6 }}>
              <label>
                必须出现（逗号分隔）
                <input value={mustInclude} onChange={(event) => setMustInclude(event.target.value)} />
              </label>
              <label>
                禁止出现（逗号分隔）
                <input value={forbidden} onChange={(event) => setForbidden(event.target.value)} />
              </label>
            </div>
            <div style={{ marginTop: 6 }}>
              <div className="hint">
                必须优先体现的碎片（勾中的碎片正文会作为 must_include 一起传给写作）：
              </div>
              {pickableFragments.length === 0 && (
                <div className="hint">暂无可勾选的碎片（可在「碎片」标签里记几条）。</div>
              )}
              {pickableFragments.map((fragment) => (
                <label key={fragment.id} className="fragment-pick">
                  <input
                    type="checkbox"
                    checked={mustFragments.includes(fragment.id)}
                    onChange={() => toggleMustFragment(fragment.id)}
                    style={{ width: "auto", marginTop: 3 }}
                  />
                  <span>
                    {fragment.target_chapter ? (
                      <span className="badge">第{fragment.target_chapter}章</span>
                    ) : (
                      <span className="badge">未安排</span>
                    )}{" "}
                    {fragment.text.replace(/\s+/g, " ").slice(0, 50)}
                  </span>
                </label>
              ))}
            </div>
            <div className="field-row" style={{ marginTop: 6 }}>
              <label>
                目标字数
                <input
                  type="number"
                  value={targetWords}
                  onChange={(event) => setTargetWords(Number(event.target.value) || 1200)}
                />
              </label>
              <label>
                相关人物
                <select
                  multiple
                  value={writeCharacters}
                  onChange={(event) =>
                    setWriteCharacters(
                      Array.from(event.target.selectedOptions).map((option) => option.value),
                    )
                  }
                  style={{ minHeight: 78 }}
                >
                  {(bible?.characters ?? []).map((character) => (
                    <option key={character.id} value={character.name}>
                      {character.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="field-row" style={{ marginTop: 8 }}>
              <button
                className="primary"
                onClick={autoRevise ? reviseChapter : generateChapter}
                disabled={busy}
              >
                {autoRevise ? "自动修订" : "生成草稿"}
              </button>
              {(draft || revision) && (
                <button onClick={saveDraftAsChapter} disabled={busy}>
                  另存为新章节
                </button>
              )}
            </div>
            <label style={{ flexDirection: "row", alignItems: "center", marginTop: 6 }}>
              <input
                type="checkbox"
                checked={autoRevise}
                onChange={(event) => setAutoRevise(event.target.checked)}
                style={{ width: "auto" }}
              />
              自动修订（写作→评审→改稿）
            </label>
            <div className="hint">
              勾选后走修订闭环：先生成草稿，再按文风与一致性评审结果自动改稿（最多 2 轮，目标分
              85），耗时与模型费用都高于单次生成；无论哪种方式，落库都由「另存为新章节」完成。
            </div>

            {draft && (
              <div style={{ marginTop: 10 }}>
                <div className="hint">
                  {draft.draft.title}｜{draft.draft.word_count} 字｜检索到 Canon
                  {draft.retrieved.canon_facts.length} 条、人物
                  {draft.retrieved.characters.length} 位、未回收伏笔
                  {draft.retrieved.foreshadowing.length} 条
                </div>
                {draft.warnings.length > 0 && (
                  <div className="issue warning">
                    {draft.warnings.map((warning, index) => (
                      <div key={index}>{warning}</div>
                    ))}
                  </div>
                )}
                <pre className="draft">{draft.draft.content}</pre>
                <details>
                  <summary>查看写作前检索到的 Canon 明细</summary>
                  <ul className="hint">
                    {draft.retrieved.canon_facts.map((fact, index) => (
                      <li key={index}>
                        {String(fact.subject)} {String(fact.predicate)} {String(fact.object)}（第
                        {String(fact.source_chapter)}章）
                      </li>
                    ))}
                  </ul>
                </details>
              </div>
            )}

            {revision && (
              <div style={{ marginTop: 10 }}>
                <div className="field-row" style={{ alignItems: "center" }}>
                  <span className={`score-value ${scoreClass(revision.final_score)}`}>
                    {formatScore(revision.final_score)}
                  </span>
                  <span className="badge">final_score</span>
                  {revision.accepted ? (
                    <span className="badge ok">已达标</span>
                  ) : (
                    <span className="badge warning">未达标（无更高分改稿）</span>
                  )}
                  <span className="spacer" />
                  <span className="hint">
                    {revision.draft.title}｜{revision.draft.word_count} 字｜{revision.provider}/
                    {revision.model}
                  </span>
                </div>
                {revision.warnings.length > 0 && (
                  <div className="issue warning">
                    {revision.warnings.map((warning, index) => (
                      <div key={index}>{warning}</div>
                    ))}
                  </div>
                )}
                <div className="hint" style={{ marginTop: 6 }}>
                  轮次轨迹：
                </div>
                <table className="grid metric-table">
                  <thead>
                    <tr>
                      <th>轮次</th>
                      <th>阶段</th>
                      <th>score</th>
                      <th>字数</th>
                      <th>文风 codes</th>
                      <th>一致性 codes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {revision.rounds.map((round, index) => (
                      <tr key={`${round.round}-${index}`}>
                        <td>第{round.round}轮</td>
                        <td>
                          <span className="badge">{round.stage}</span>
                        </td>
                        <td>{formatScore(round.score)}</td>
                        <td>{round.word_count}</td>
                        <td>{round.style_codes.join("、") || "—"}</td>
                        <td>{round.continuity_codes.join("、") || "—"}</td>
                      </tr>
                    ))}
                    {revision.rounds.length === 0 && (
                      <tr>
                        <td colSpan={6}>没有轮次记录。</td>
                      </tr>
                    )}
                  </tbody>
                </table>

                <div className="hint" style={{ marginTop: 6 }}>
                  指标变化：改善 {revision.metric_deltas.improved} 项，变差{" "}
                  {revision.metric_deltas.worsened} 项
                </div>
                <table className="grid metric-table">
                  <thead>
                    <tr>
                      <th>指标</th>
                      <th>改稿前</th>
                      <th>改稿后</th>
                      <th>变化</th>
                      <th>判定</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(revision.metric_deltas.deltas ?? {}).map(([key, delta]) => (
                      <tr key={key}>
                        <td>{metricLabel(key)}</td>
                        <td>{formatMetric(delta.before)}</td>
                        <td>{formatMetric(delta.after)}</td>
                        <td>{formatMetric(delta.delta)}</td>
                        <td>
                          {Math.abs(delta.delta) < 1e-9 ? (
                            <span className="badge">持平</span>
                          ) : delta.better === null ? (
                            <span className="badge">区间指标</span>
                          ) : delta.better ? (
                            <span className="delta-better">改善</span>
                          ) : (
                            <span className="delta-worse">变差</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <div className="hint" style={{ marginTop: 6 }}>
                  检索到 Canon {revision.retrieved.canon_facts.length} 条、人物{" "}
                  {revision.retrieved.characters.length} 位、未回收伏笔{" "}
                  {revision.retrieved.foreshadowing.length} 条｜goal={revision.goal || "—"}
                  {revision.generation_id ? `｜生成记录 ${revision.generation_id}` : ""}
                </div>
                <pre className="draft">{revision.draft.content}</pre>
              </div>
            )}
          </>
        )}

        {chapterId && tab === "style" && (
          <>
            <div className="field-row" style={{ marginBottom: 8 }}>
              <button
                className="primary"
                onClick={() => void runStyleReview(true)}
                disabled={busy || styleBusy}
              >
                {styleBusy ? "评审中…" : "评审本章（含模型读感）"}
              </button>
              <button onClick={() => void runStyleReview(false)} disabled={busy || styleBusy}>
                仅规则层
              </button>
              <button onClick={() => void loadStyleHistory()} disabled={styleBusy}>
                刷新历史
              </button>
            </div>
            <div className="hint">
              评审读的是本章已保存的正文（未保存的改动会先保存）；结果会落库，可在历史里对比。
              「仅规则层」只跑确定性指标，不调用模型。
            </div>

            {styleReview ? (
              <div style={{ marginTop: 8 }}>
                <StyleReviewView review={styleReview} />
              </div>
            ) : (
              <div className="hint" style={{ marginTop: 8 }}>
                还没有本次评审结果。
              </div>
            )}

            <div className="issue" style={{ marginTop: 10 }}>
              <div className="issue-title">历史评审（第 {chapterNumber ?? "?"} 章）</div>
              {styleHistory.length === 0 && <div className="hint">本章还没有历史评审记录。</div>}
              {styleHistory.map((item) => (
                <div key={item.id} className="item-row">
                  <div className="row-head">
                    <span className={`score-value ${scoreClass(item.score)}`}>
                      {formatScore(item.score)}
                    </span>
                    <span className="badge">{item.label}</span>
                    <span className="spacer" />
                    <span className="hint">
                      {item.created_at.slice(0, 19).replace("T", " ")}
                    </span>
                  </div>
                  <div className="payload">
                    {item.codes.filter(Boolean).join("、") || "无 issue"}
                    {item.provider ? `｜${item.provider}${item.model ? `/${item.model}` : ""}` : ""}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function splitList(text: string): string[] {
  return text
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** 渲染一致性检查报告：错误在前、警告在后，每条都带上证据与修改建议。 */
function renderIssues(report: ContinuityReport) {
  const issues = [...report.errors, ...report.warnings];
  return (
    <div style={{ marginBottom: 12 }}>
      <div className="hint">
        审校结果（{report.provider}/{report.model}）：{report.errors.length} 个错误、
        {report.warnings.length} 个警告
        {report.dropped_issues.length > 0 &&
          `；已丢弃 ${report.dropped_issues.length} 条无证据的模型候选`}
      </div>
      {issues.length === 0 && (
        <div className="issue">
          <div className="issue-title">未发现与 Canon 的冲突</div>
        </div>
      )}
      {issues.map((issue, index) => (
        <div key={index} className={`issue ${issue.level}`}>
          <div className="issue-title">
            <span className={`badge ${issue.level}`}>{issue.level === "error" ? "错误" : "警告"}</span>{" "}
            {issue.code}
          </div>
          <div>{issue.message}</div>
          <ul>
            {issue.evidence.map((evidence, position) => (
              <li key={position}>
                证据[{evidence.source_chapter}] {evidence.quote || evidence.detail}
              </li>
            ))}
          </ul>
          {issue.suggested_fix && <div className="fix">建议：{issue.suggested_fix}</div>}
        </div>
      ))}
    </div>
  );
}

function describePayload(kind: string, payload: Record<string, any>): string {
  switch (kind) {
    case "FACT":
      return `${payload.subject} ${payload.predicate} ${payload.object}${
        payload.note ? `（${payload.note}）` : ""
      }`;
    case "CHARACTER_STATE":
      return `${payload.name}｜动作：${payload.action || "—"}｜状态变化：${
        payload.status_change || "—"
      }｜地点：${payload.location || "—"}${payload.notes ? `｜依据：${payload.notes}` : ""}`;
    case "EVENT":
      return `${payload.time || ""} ${payload.location || ""}｜${payload.description || ""}`;
    case "TIMELINE":
      return `${payload.story_time}｜${payload.event}｜${payload.location || ""}`;
    case "FORESHADOWING":
      return `${payload.name}：${payload.description || ""}`;
    case "RELATIONSHIP":
      return `${payload.character_a} ↔ ${payload.character_b}｜${payload.relation || ""}｜${
        payload.change || ""
      }`;
    case "LOCATION":
      return `地点：${payload.location || JSON.stringify(payload)}`;
    default:
      return JSON.stringify(payload);
  }
}
