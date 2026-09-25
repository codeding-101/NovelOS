"use client";

import { useCallback, useEffect, useState, type CSSProperties } from "react";
import { api } from "@/lib/api";
import type { StructureChapter, StructureView } from "@/lib/types";

interface Props {
  novelId: string;
  /** 结构视图只读正文与既有报告，不需要模型 provider；保留以便与其它面板同形。 */
  provider: string;
  /** 章节数变化时重新拉取：正文增删会改变逐章信号、弱区与节奏窗口。 */
  chaptersVersion: number;
  onRefresh: () => Promise<void>;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

type ChapterStatus = "ok" | "weak" | "broken";

const STATUS_LABELS: Record<ChapterStatus, string> = {
  ok: "达标",
  weak: "偏弱",
  broken: "有硬伤",
};

const STATUS_BADGE: Record<ChapterStatus, string> = {
  ok: "ok",
  weak: "warning",
  broken: "error",
};

const STATUS_FILL: Record<ChapterStatus, { background: string; border: string }> = {
  ok: { background: "var(--ok-soft)", border: "var(--ok)" },
  weak: { background: "var(--warning-soft)", border: "var(--warning)" },
  broken: { background: "var(--danger-soft)", border: "var(--danger)" },
};

/** 弱区在逐章表与节奏线上的对应色，按弱区顺序循环取用。 */
const RUN_COLORS = ["var(--danger)", "var(--warning)", "var(--accent)", "var(--ok)"];

const runColor = (index: number) => RUN_COLORS[index % RUN_COLORS.length];

/** 硬伤＝审校错误或断言冲突；只有指标不达标算偏弱；都没有算达标。 */
function chapterStatus(chapter: StructureChapter): ChapterStatus {
  if ((chapter.continuity_errors ?? 0) > 0 || (chapter.claim_conflicts ?? 0) > 0) return "broken";
  return (chapter.verdicts ?? []).length > 0 ? "weak" : "ok";
}

function fixed(value: number | undefined | null, digits: number): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function percent(value: number | undefined | null): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value * 100)}%`
    : "—";
}

/** 单章区间标签：一章写「第 3 章」，多章写「第 3–5 章」。 */
function rangeLabel(start: number, end: number): string {
  return start === end ? `第 ${start} 章` : `第 ${start}–${end} 章`;
}

function isSameRun(
  run: { start_chapter: number; end_chapter: number } | null,
  start: number,
  end: number,
): boolean {
  return run !== null && run.start_chapter === start && run.end_chapter === end;
}

export function StructurePanel({
  novelId,
  chaptersVersion,
  onRefresh,
  setStatus,
  setError,
}: Props) {
  const [view, setView] = useState<StructureView | null>(null);
  const [loading, setLoading] = useState(false);
  /** 节奏线里鼠标悬停的章号，用来在下方显示章号与标题。 */
  const [hovered, setHovered] = useState<number | null>(null);
  /** 当前高亮的弱区下标：悬停/点击弱区条目时，逐章表与节奏线同步标出。 */
  const [activeRun, setActiveRun] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setView(await api.structure(novelId));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setView(null);
    } finally {
      setLoading(false);
    }
  }, [novelId, setError]);

  useEffect(() => {
    void load();
  }, [load, chaptersVersion]);

  async function reload() {
    await load();
    setStatus("结构视图已刷新");
    await onRefresh();
  }

  const chapters = view?.chapters ?? [];
  const weakRuns = view?.weak_runs ?? [];
  const windows = view?.pace_windows ?? [];
  const floors = view?.floors ?? {};
  const hookFloor = typeof floors.hook_floor === "number" ? floors.hook_floor : 0;
  const weakestRun = view?.summary?.weakest_run ?? null;
  const weakestWindow = view?.summary?.weakest_window ?? null;
  const weakChapters = view?.summary?.weak_chapters ?? 0;
  const hoveredChapter =
    hovered === null ? null : chapters.find((item) => item.chapter_number === hovered) ?? null;

  // 章号 → 所属弱区（同一章落在多个弱区时取第一个）
  const runIndexByChapter = new Map<number, number>();
  weakRuns.forEach((run, index) => {
    for (let number = run.start_chapter; number <= run.end_chapter; number += 1) {
      if (!runIndexByChapter.has(number)) runIndexByChapter.set(number, index);
    }
  });

  return (
    <div>
      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          全书结构
          <span className="spacer" />
          {view && <span className="badge">共 {view.chapter_count} 章 / {view.word_count} 字</span>}
          {view && (
            <span className={`badge ${weakChapters > 0 ? "warning" : "ok"}`}>
              弱章 {weakChapters} 章
            </span>
          )}
          <button onClick={() => void reload()} disabled={loading}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
        <div className="field-row">
          {weakestRun ? (
            <>
              <span className="badge error">
                最弱区间 {rangeLabel(weakestRun.start_chapter, weakestRun.end_chapter)}
                （共 {weakestRun.length} 章）
              </span>
              <span className="hint">
                原因：{(weakestRun.reasons ?? []).join("、") || "—"}
              </span>
            </>
          ) : (
            view && <span className="hint">没有连续弱区。</span>
          )}
        </div>
        <div className="field-row" style={{ marginTop: 4 }}>
          {weakestWindow ? (
            <>
              <span
                className={`badge ${weakestWindow.avg_hook < hookFloor ? "warning" : "ok"}`}
              >
                最弱窗口 {rangeLabel(weakestWindow.start_chapter, weakestWindow.end_chapter)}
              </span>
              <span className="hint">
                平均钩子 {fixed(weakestWindow.avg_hook, 2)}｜{weakestWindow.verdict || "—"}
              </span>
            </>
          ) : (
            view && <span className="hint">还没有节奏窗口。</span>
          )}
        </div>
        {!view && <div className="hint">{loading ? "正在读取全书结构…" : "尚未读取结构视图。"}</div>}
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          节奏线
          <span className="spacer" />
          <span className="hint">每格一章，颜色按该章是否达标</span>
        </div>
        {chapters.length === 0 ? (
          <div className="hint">本书还没有正文，结构视图要至少一章正文。</div>
        ) : (
          <>
            <div style={{ overflowX: "auto" }}>
              <div style={{ display: "flex", gap: 2, paddingBottom: 2 }}>
                {chapters.map((chapter) => {
                  const status = chapterStatus(chapter);
                  const runIndex = runIndexByChapter.get(chapter.chapter_number);
                  const dimmed = activeRun !== null && activeRun !== runIndex;
                  const reasons = (chapter.verdicts ?? []).join("；");
                  return (
                    <div
                      key={chapter.chapter_id || `chapter-${chapter.chapter_number}`}
                      title={`第${chapter.chapter_number}章 ${chapter.title || "（无标题）"}｜${
                        STATUS_LABELS[status]
                      }${reasons ? `：${reasons}` : ""}`}
                      onMouseEnter={() => setHovered(chapter.chapter_number)}
                      onMouseLeave={() => setHovered(null)}
                      style={{
                        flex: "1 1 18px",
                        minWidth: 12,
                        maxWidth: 44,
                        height: 26,
                        borderRadius: "var(--radius-sm)",
                        border: `1px solid ${STATUS_FILL[status].border}`,
                        background: STATUS_FILL[status].background,
                        outline: !dimmed && runIndex !== undefined ? `2px solid ${runColor(runIndex)}` : "none",
                        outlineOffset: -3,
                        opacity: dimmed ? 0.35 : 1,
                      }}
                    />
                  );
                })}
              </div>
            </div>
            <div className="field-row" style={{ marginTop: 4, alignItems: "center" }}>
              <span className="hint">
                {hoveredChapter
                  ? `第${hoveredChapter.chapter_number}章 ${hoveredChapter.title || "（无标题）"}｜${
                      STATUS_LABELS[chapterStatus(hoveredChapter)]
                    }${
                      (hoveredChapter.verdicts ?? []).length > 0
                        ? `：${(hoveredChapter.verdicts ?? []).join("；")}`
                        : ""
                    }`
                  : "把鼠标移到格子上看章号与标题。"}
              </span>
              <span className="spacer" />
              {(["ok", "weak", "broken"] as ChapterStatus[]).map((status) => (
                <span key={status} className={`badge ${STATUS_BADGE[status]}`}>
                  {STATUS_LABELS[status]}
                </span>
              ))}
            </div>
          </>
        )}
      </div>

      <div style={{ overflowX: "auto" }}>
        <table className="grid">
          <thead>
            <tr>
              <th>章</th>
              <th>标题</th>
              <th>字数</th>
              <th>钩子分</th>
              <th>推进密度</th>
              <th>注水比例</th>
              <th>审校错误</th>
              <th>断言冲突</th>
              <th>结构事件</th>
              <th>判决与原因</th>
            </tr>
          </thead>
          <tbody>
            {chapters.map((chapter) => {
              const status = chapterStatus(chapter);
              const runIndex = runIndexByChapter.get(chapter.chapter_number);
              const opened = chapter.foreshadowing_opened ?? [];
              const advanced = chapter.foreshadowing_advanced ?? [];
              const due = chapter.commitments_due ?? [];
              const verdicts = chapter.verdicts ?? [];
              const style: CSSProperties = {};
              if (status !== "ok") {
                style.backgroundColor =
                  status === "broken" ? "var(--danger-soft)" : "var(--warning-soft)";
              } else if (activeRun !== null && activeRun === runIndex) {
                style.backgroundColor = "var(--surface-muted)";
              }
              if (activeRun !== null && activeRun !== runIndex) style.opacity = 0.55;
              return (
                <tr key={chapter.chapter_id || `chapter-${chapter.chapter_number}`} style={style}>
                  {/* 弱区标记放在首格：折叠边框模型下 <tr> 自己的左边框不一定会画出来 */}
                  <td
                    style={
                      runIndex === undefined
                        ? undefined
                        : { borderLeft: `3px solid ${runColor(runIndex)}` }
                    }
                  >
                    {chapter.chapter_number}
                  </td>
                  <td>{chapter.title || "—"}</td>
                  <td>{chapter.word_count ?? 0}</td>
                  <td className={chapter.hook_score < hookFloor ? "error-text" : undefined}>
                    {fixed(chapter.hook_score, 2)}
                  </td>
                  <td
                    className={
                      chapter.advancement_per_1k < (floors.advancement_floor ?? 0)
                        ? "error-text"
                        : undefined
                    }
                  >
                    {fixed(chapter.advancement_per_1k, 1)}
                  </td>
                  <td
                    className={
                      chapter.filler_paragraph_ratio > (floors.filler_ceiling ?? 1)
                        ? "error-text"
                        : undefined
                    }
                  >
                    {percent(chapter.filler_paragraph_ratio)}
                  </td>
                  <td>
                    {(chapter.continuity_errors ?? 0) > 0 ? (
                      <span className="badge error">{chapter.continuity_errors}</span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    {(chapter.claim_conflicts ?? 0) > 0 ? (
                      <span className="badge error">{chapter.claim_conflicts}</span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    {opened.length === 0 && advanced.length === 0 && due.length === 0 && (
                      <span className="hint">—</span>
                    )}
                    {opened.length > 0 && <div className="hint">伏笔首现：{opened.join("、")}</div>}
                    {advanced.length > 0 && (
                      <div className="hint">伏笔推进：{advanced.join("、")}</div>
                    )}
                    {due.length > 0 && <div className="hint">承诺到期：{due.join("、")}</div>}
                  </td>
                  <td>
                    <span className={`badge ${STATUS_BADGE[status]}`}>{STATUS_LABELS[status]}</span>
                    {verdicts.length > 0 && (
                      <div className="hint" style={{ color: "inherit" }}>
                        {verdicts.join("；")}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
            {chapters.length === 0 && (
              <tr>
                <td colSpan={10}>
                  {loading ? "正在加载逐章信号…" : "还没有可分析的章节。"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="issue" style={{ marginTop: 10 }}>
        <div className="issue-title">连续弱区（{weakRuns.length}）</div>
        {weakRuns.length === 0 ? (
          <div className="hint">没有连续不达标的区间。</div>
        ) : (
          <>
            {weakRuns.map((run, index) => (
              <div
                key={`run-${run.start_chapter}-${run.end_chapter}`}
                className="item-row"
                onMouseEnter={() => setActiveRun(index)}
                onMouseLeave={() => setActiveRun(null)}
                onClick={() => setActiveRun(index)}
                style={{
                  cursor: "pointer",
                  borderLeft: `3px solid ${runColor(index)}`,
                  opacity: activeRun !== null && activeRun !== index ? 0.55 : 1,
                }}
              >
                <div className="row-head">
                  <span className={`badge ${run.length >= 3 ? "error" : "warning"}`}>
                    {rangeLabel(run.start_chapter, run.end_chapter)}
                  </span>
                  <b>共 {run.length} 章</b>
                  {isSameRun(weakestRun, run.start_chapter, run.end_chapter) && (
                    <span className="badge error">最弱</span>
                  )}
                </div>
                <div className="payload">
                  原因：{(run.reasons ?? []).join("、") || "—"}
                </div>
              </div>
            ))}
            <div className="hint">悬停或点一条，逐章表与节奏线只留下这一段。</div>
          </>
        )}
      </div>

      <div className="issue">
        <div className="issue-title">节奏窗口（每 {floors.pace_window ?? 5} 章一段）</div>
        {windows.length === 0 ? (
          <div className="hint">还没有节奏窗口。</div>
        ) : (
          windows.map((window) => {
            const belowFloor = window.avg_hook < hookFloor;
            return (
              <div
                key={`window-${window.start_chapter}-${window.end_chapter}`}
                className="item-row"
                style={{
                  ...(belowFloor ? { borderLeft: "3px solid var(--warning)" } : {}),
                  ...(belowFloor && window.verdict ? { background: "var(--warning-soft)" } : {}),
                }}
              >
                <div className="row-head">
                  <span className={`badge ${belowFloor ? "warning" : "ok"}`}>
                    {rangeLabel(window.start_chapter, window.end_chapter)}
                  </span>
                  <b>钩子 {fixed(window.avg_hook, 2)}</b>
                  <span className="badge">推进 {fixed(window.avg_advancement, 1)}</span>
                  <span className="badge">冲突 {fixed(window.avg_conflict, 1)}</span>
                  <span className="badge">{window.words ?? 0} 字</span>
                  <span className="badge">峰值 第{window.peak_chapter}章</span>
                </div>
                <div className="payload">{window.verdict || "—"}</div>
              </div>
            );
          })
        )}
      </div>

      <div className="hint">
        阈值是经验下限：章末钩子 ≥ {fixed(floors.hook_floor, 2)}、推进密度 ≥{" "}
        {fixed(floors.advancement_floor, 1)}/千字、注水比例 ≤ {percent(floors.filler_ceiling)}；
        连续不达标会被收成「连续弱区」，也就是读者容易掉队的地方。判断依据是平台点名的「结构失常」
        「大段内容未能推动情节发展」，以及优质内容要求的「主线清晰、节奏合理、剧情层层递进」
        「每 3~5 章一个小高潮」。
      </div>
    </div>
  );
}
