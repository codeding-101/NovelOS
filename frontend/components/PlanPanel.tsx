"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  ChapterPlan,
  ForeshadowingPlan,
  PlanGenerateContextSummary,
  WriteChapterResponse,
} from "@/lib/types";

interface Props {
  novelId: string;
  provider: string;
  /** 章节数量变化时重新拉取计划：正文增删会改变计划的 PLANNED/WRITTEN 状态 */
  chaptersVersion: number;
  onRefresh: () => Promise<void>;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

interface GenerateMeta {
  provider: string;
  model: string;
  warnings: string[];
  summary: PlanGenerateContextSummary | null;
}

const STATUS_CLASS: Record<string, string> = {
  PLANNED: "proposed",
  WRITTEN: "canon",
  DISCARDED: "rejected",
};

const URGENCY_CLASS: Record<string, string> = {
  HIGH: "error",
  MEDIUM: "warning",
  LOW: "",
};

export function PlanPanel({
  novelId,
  provider,
  chaptersVersion,
  onRefresh,
  setStatus,
  setError,
}: Props) {
  const [plans, setPlans] = useState<ChapterPlan[]>([]);
  const [forecast, setForecast] = useState<ForeshadowingPlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [writing, setWriting] = useState<string | null>(null);
  const [meta, setMeta] = useState<GenerateMeta | null>(null);
  const [result, setResult] = useState<{ planId: string; response: WriteChapterResponse } | null>(null);
  // 生成表单
  const [fromChapter, setFromChapter] = useState("");
  const [count, setCount] = useState(3);
  const [steer, setSteer] = useState("");
  const [overwrite, setOverwrite] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextPlans, nextForecast] = await Promise.all([
        api.listPlans(novelId),
        api.foreshadowingPlan(novelId, 5, 8),
      ]);
      setPlans([...nextPlans].sort((a, b) => a.chapter_number - b.chapter_number));
      setForecast(nextForecast);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, [novelId, setError]);

  useEffect(() => {
    void load();
  }, [load, chaptersVersion]);

  async function generate() {
    setGenerating(true);
    setError("");
    setStatus("规划 Agent 正在读取 Canon、人物与伏笔欠账…");
    try {
      const from = fromChapter.trim() ? Number(fromChapter) : undefined;
      const response = await api.generatePlans(novelId, {
        ...(from && from > 0 ? { from_chapter: from } : {}),
        count,
        steer: steer.trim(),
        provider,
        overwrite,
      });
      setMeta({
        provider: response.provider,
        model: response.model,
        warnings: response.warnings,
        summary: response.context_summary,
      });
      setStatus(
        `规划完成：生成 ${response.plans.length} 章计划（provider=${response.provider}），` +
          `跳过 ${response.context_summary.skipped.length} 章`,
      );
      await load();
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setGenerating(false);
    }
  }

  async function writeFromPlan(plan: ChapterPlan, save: boolean) {
    setWriting(plan.id);
    setError("");
    setStatus(save ? `正在按第${plan.chapter_number}章计划写草稿并入库…` : `正在按第${plan.chapter_number}章计划写草稿…`);
    try {
      const response = await api.writeFromPlan(plan.id, { provider, targetWords: 1500, save });
      setResult({ planId: plan.id, response });
      setStatus(
        `草稿完成：${response.draft.word_count} 字，语义召回 ${response.retrieved.semantic_hits.length} 条` +
          (response.saved_chapter_id ? "，已存为章节" : ""),
      );
      if (save) {
        await load();
        await onRefresh();
      }
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setWriting(null);
    }
  }

  async function removePlan(plan: ChapterPlan) {
    setError("");
    try {
      await api.deletePlan(plan.id);
      setPlans((current) => current.filter((item) => item.id !== plan.id));
      if (result?.planId === plan.id) setResult(null);
      setStatus(`已删除第${plan.chapter_number}章的计划`);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <div>
      <div className="issue">
        <div className="issue-title">生成规划</div>
        <div className="field-row">
          <label>
            起始章号（留空则从下一章开始）
            <input
              type="number"
              value={fromChapter}
              placeholder="自动"
              onChange={(event) => setFromChapter(event.target.value)}
            />
          </label>
          <label>
            生成章数
            <input
              type="number"
              min={1}
              max={10}
              value={count}
              onChange={(event) => setCount(Number(event.target.value) || 1)}
            />
          </label>
          <label style={{ flex: "0 0 auto", flexDirection: "row", alignItems: "center" }}>
            <input
              type="checkbox"
              checked={overwrite}
              onChange={(event) => setOverwrite(event.target.checked)}
              style={{ width: "auto" }}
            />
            覆盖已有计划
          </label>
        </div>
        <textarea
          placeholder="作者要求（steer，可选）：例如「这几章要收掉玄铁令的伏笔，节奏放快」"
          value={steer}
          onChange={(event) => setSteer(event.target.value)}
          rows={2}
          style={{ width: "100%", marginTop: 6 }}
        />
        <div className="field-row" style={{ marginTop: 8 }}>
          <button className="primary" onClick={() => void generate()} disabled={generating}>
            {generating ? "规划中…" : "生成计划"}
          </button>
          <button onClick={() => void load()} disabled={loading}>
            {loading ? "刷新中…" : "刷新列表"}
          </button>
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          规划会调用模型读取 Canon、人物状态与伏笔欠账；已有正文的章节会被跳过。
        </div>
      </div>

      {meta && (
        <div className="issue">
          <div className="issue-title">本次规划</div>
          <div>
            provider={meta.provider}
            {meta.model ? `/${meta.model}` : ""}
            {meta.summary && (
              <>
                ｜前缘章 第{meta.summary.frontier_chapter}章｜Canon {meta.summary.canon_facts} 条｜人物{" "}
                {meta.summary.characters} 位｜欠账伏笔 {meta.summary.foreshadowing_debt} 条｜前文摘要{" "}
                {meta.summary.recent_summaries} 条
              </>
            )}
          </div>
          {meta.warnings.length > 0 && (
            <ul>
              {meta.warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          )}
          {meta.summary && meta.summary.skipped.length > 0 && (
            <div className="hint">
              跳过：
              {meta.summary.skipped
                .map((item) => `第${item.chapter_number}章（${item.reason}）`)
                .join("；")}
            </div>
          )}
        </div>
      )}

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          伏笔回收建议
          <span className="spacer" />
          <span className="hint" style={{ color: "inherit" }}>
            前缘章 第{forecast?.frontier_chapter ?? 1}章｜未回收 {forecast?.open_count ?? 0}｜欠账{" "}
            {forecast?.overdue_count ?? 0}
          </span>
        </div>
        {(forecast?.suggestions.length ?? 0) === 0 && (
          <div className="hint">暂无回收建议（没有拖太久的伏笔）。</div>
        )}
        {forecast?.suggestions.map((item) => (
          <div key={item.foreshadowing_id} className="item-row">
            <div className="row-head">
              <span className={`badge ${URGENCY_CLASS[item.urgency] ?? ""}`}>{item.urgency}</span>
              <b>{item.name}</b>
              <span className="badge">{item.status}</span>
              {item.overdue && <span className="badge error">已欠账</span>}
              <span className="spacer" />
              <span className="hint">已 {item.age} 章未推进</span>
            </div>
            <div className="payload">
              建议在第 {item.suggested_chapter} 章回应｜相关人物：
              {item.related_characters.join("、") || "—"}
            </div>
            {item.reason && <div className="payload">理由：{item.reason}</div>}
            {item.expected_payoff && <div className="payload">期望回收：{item.expected_payoff}</div>}
          </div>
        ))}
      </div>

      <div className="issue">
        <div className="issue-title">计划列表（共 {plans.length} 条）</div>
        {loading && plans.length === 0 && <div className="hint">正在加载计划…</div>}
        {!loading && plans.length === 0 && (
          <div className="hint">还没有计划，先在上方生成一份。</div>
        )}
      </div>
      {plans.length > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th>章号</th>
              <th>标题</th>
              <th>状态</th>
              <th>目标</th>
              <th>必须出现</th>
              <th>禁止出现</th>
              <th>涉及人物</th>
              <th>推进伏笔</th>
              <th>理由</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {plans.map((plan) => (
              <tr key={plan.id}>
                <td>第{plan.chapter_number}章</td>
                <td>{plan.title || "（无标题）"}</td>
                <td>
                  <span className={`badge ${STATUS_CLASS[plan.status] ?? ""}`}>{plan.status}</span>
                </td>
                <td>{plan.goals || "—"}</td>
                <td>{plan.must_include.join("、") || "—"}</td>
                <td>{plan.forbidden.join("、") || "—"}</td>
                <td>{plan.characters.join("、") || "—"}</td>
                <td>{plan.advance_foreshadowing.join("、") || "—"}</td>
                <td>{plan.rationale || plan.steer || "—"}</td>
                <td>
                  <button
                    onClick={() => void writeFromPlan(plan, false)}
                    disabled={writing !== null}
                  >
                    按此写草稿
                  </button>{" "}
                  <button
                    className="primary"
                    onClick={() => void writeFromPlan(plan, true)}
                    disabled={writing !== null}
                  >
                    存为章节
                  </button>{" "}
                  <button
                    className="danger"
                    onClick={() => void removePlan(plan)}
                    disabled={writing !== null}
                  >
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {result && (
        <div className="issue" style={{ marginTop: 10 }}>
          <div className="issue-title">
            {result.response.draft.title || "草稿"}｜{result.response.draft.word_count} 字｜语义召回{" "}
            {result.response.retrieved.semantic_hits.length} 条
          </div>
          {result.response.warnings.length > 0 && (
            <ul>
              {result.response.warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          )}
          {result.response.retrieved.semantic_hits.length > 0 && (
            <details>
              <summary>查看语义召回片段</summary>
              <ul className="hint">
                {result.response.retrieved.semantic_hits.map((hit, index) => (
                  <li key={index}>
                    {hit.chapter_number ? `第${hit.chapter_number}章 ` : ""}
                    {hit.chapter_title}：{hit.excerpt}（score={hit.score.toFixed(4)}）
                  </li>
                ))}
              </ul>
            </details>
          )}
          <pre className="draft">{result.response.draft.content}</pre>
        </div>
      )}
    </div>
  );
}
