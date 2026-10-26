"use client";
import { useState } from "react";
import { platformRequest } from "@/lib/platform";
import styles from "../budgets/budgets.module.css";
import local from "./memory.module.css";

export type StoryState = {
  subject_key: string;
  predicate: string;
  value: string;
  valid_from: number;
  valid_until: number | null;
};

export function StateSummary({ state }: { state: StoryState | null }) {
  return state ? (
    <p className={local.stateSummary}>
      故事状态：{state.subject_key} · {state.predicate} = {state.value}；
      生效区间 [{state.valid_from}, {state.valid_until ?? "无结束时点"})
    </p>
  ) : (
    <p>未指定结构化状态，不参与故事时点查询。</p>
  );
}

export function StateFields({
  state,
  onChange,
  disabled,
}: {
  state: StoryState | null;
  onChange: (state: StoryState | null) => void;
  disabled: boolean;
}) {
  return (
    <fieldset disabled={disabled} className={local.stateFields}>
      <legend>故事状态（可选）</legend>
      <label className={styles.check}>
        <input
          type="checkbox"
          checked={!!state}
          onChange={(e) =>
            onChange(
              e.target.checked
                ? {
                    subject_key: "",
                    predicate: "",
                    value: "",
                    valid_from: 0,
                    valid_until: null,
                  }
                : null,
            )
          }
        />
        指定故事状态与生效区间
      </label>
      <p>
        时点是作品内约定的非负整数，按故事发生顺序排列，不是章节编号或审核日期。
        开始时点包含在区间内，结束时点不包含；留空表示持续有效。同一对象的同一属性不能有重叠的正式区间。
      </p>
      {state && (
        <div className={styles.fields}>
          <label>
            对象标识
            <input
              aria-label="对象标识"
              required
              maxLength={128}
              value={state.subject_key}
              onChange={(e) =>
                onChange({ ...state, subject_key: e.target.value })
              }
            />
          </label>
          <label>
            状态属性
            <input
              aria-label="状态属性"
              required
              maxLength={128}
              value={state.predicate}
              onChange={(e) =>
                onChange({ ...state, predicate: e.target.value })
              }
            />
          </label>
          <label>
            状态取值
            <input
              aria-label="状态取值"
              required
              maxLength={1024}
              value={state.value}
              onChange={(e) => onChange({ ...state, value: e.target.value })}
            />
          </label>
          <label>
            开始时点
            <input
              aria-label="开始时点"
              type="number"
              required
              min={0}
              max={1000000000000}
              step={1}
              value={Number.isNaN(state.valid_from) ? "" : state.valid_from}
              onChange={(e) =>
                onChange({ ...state, valid_from: e.target.valueAsNumber })
              }
            />
          </label>
          <label>
            结束时点（不含，可留空）
            <input
              aria-label="结束时点"
              type="number"
              min={Number.isFinite(state.valid_from) ? state.valid_from + 1 : 1}
              max={1000000000000}
              step={1}
              value={state.valid_until ?? ""}
              onChange={(e) =>
                onChange({
                  ...state,
                  valid_until:
                    e.target.value === "" ? null : e.target.valueAsNumber,
                })
              }
            />
          </label>
        </div>
      )}
    </fieldset>
  );
}

type TimedMemory = {
  id: string;
  title: string;
  entry_key: string;
  version: number;
  story_state: StoryState;
  source_changed: boolean;
};
export function Timeline({
  workId,
  disabled,
}: {
  workId: string;
  disabled: boolean;
}) {
  const [at, setAt] = useState("0");
  const [subject, setSubject] = useState("");
  const [rows, setRows] = useState<TimedMemory[] | null>(null);
  const [applied, setApplied] = useState<{
    at: string;
    subject: string;
  } | null>(null);
  const [offset, setOffset] = useState(0);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  async function search(start = 0, filter = { at, subject }) {
    setPending(true);
    setError("");
    setRows(null);
    try {
      const found = await platformRequest<TimedMemory[]>(
        `/works/${workId}/memory/timeline?at=${encodeURIComponent(filter.at)}&offset=${start}&limit=20${filter.subject ? `&subject_key=${encodeURIComponent(filter.subject)}` : ""}`,
      );
      setRows(found);
      setOffset(start);
      setApplied(filter);
    } catch (err) {
      setError(err instanceof Error ? err.message : "查询失败");
    } finally {
      setPending(false);
    }
  }
  const busy = disabled || pending;
  return (
    <section className={styles.panel} aria-label="故事时点查询">
      <h2>故事时点查询</h2>
      <p>
        按当前批准的设定查询故事过去或未来的状态。待审核、已替代及未填写时点的条目不参与查询。
        查询不代表当时角色已知的信息；来源过期的正式状态会保留并提示复查。
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        <div className={styles.fields}>
          <label>
            查询故事时点
            <input
              aria-label="查询故事时点"
              type="number"
              required
              min={0}
              max={1000000000000}
              step={1}
              disabled={busy}
              value={at}
              onChange={(e) => setAt(e.target.value)}
            />
          </label>
          <label>
            查询对象（精确匹配，可留空）
            <input
              aria-label="查询对象"
              maxLength={128}
              disabled={busy}
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
            />
          </label>
        </div>
        <button disabled={busy}>查询故事状态</button>
      </form>
      {error && <p role="alert">{error}</p>}
      {rows && applied && (
        <>
          <p className={local.stateSummary}>
            查询时点：{applied.at}；对象：{applied.subject || "全部"}
            。这是本次查询结果，可重新查询以获取最新审批。
          </p>
          {!rows.length && (
            <p>此时点没有匹配的正式状态，不代表状态未知期间不存在事实。</p>
          )}
          {rows.map((row) => (
            <article className={styles.record} key={row.id}>
              <h3>
                {row.title} · v{row.version}
              </h3>
              <p>{row.entry_key}</p>
              <StateSummary state={row.story_state} />
              {row.source_changed && <p>来源已有新版本，需复查。</p>}
            </article>
          ))}
          <div className={styles.actions}>
            <button
              disabled={busy || !offset}
              onClick={() => void search(offset - 20, applied)}
            >
              时点结果上一页
            </button>
            <button
              disabled={busy || rows.length < 20}
              onClick={() => void search(offset + 20, applied)}
            >
              时点结果下一页
            </button>
          </div>
        </>
      )}
    </section>
  );
}
