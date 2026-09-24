"use client";

import type { StyleIssue, StyleMetricStat, StyleReview } from "@/lib/types";

/** 评审结果里优先展示的指标与顺序（其余数值指标排在后面）。 */
export const KEY_METRICS: string[] = [
  "total_chars",
  "mean_sentence_len",
  "burstiness",
  "dialogue_ratio",
  "cliche_per_1k",
  "abstract_per_1k",
  "explaining_per_1k",
  "long_sentence_ratio",
  "subject_repeat_ratio",
  "self_repeat_ratio",
  "hook_score",
  "conflict_per_1k",
  "advancement_per_1k",
  "filler_paragraph_ratio",
  "exposition_per_1k",
  "punctuation_entropy",
  "pattern_loop_ratio",
  "word_ttr",
  "word_concentration",
  "verb_variety",
  "summary_per_1k",
  "elevation_per_1k",
  "transition_per_1k",
  "abstract_unsupported_ratio",
  "novel_char_ratio",
];

export const METRIC_LABELS: Record<string, string> = {
  total_chars: "字数",
  sentence_count: "句子数",
  paragraph_count: "段落数",
  mean_sentence_len: "平均句长",
  sentence_len_cv: "句长离散度",
  burstiness: "句长变异（burstiness）",
  short_sentence_ratio: "短句占比",
  long_sentence_ratio: "长句占比",
  paragraph_len_cv: "段长离散度",
  dialogue_ratio: "对白占比",
  dialogue_paragraph_ratio: "对白段落占比",
  cliche_per_1k: "套话密度（每千字）",
  cliche_variety: "套话种类比例",
  abstract_per_1k: "抽象词密度（每千字）",
  explaining_per_1k: "解释性叙述密度（每千字）",
  conflict_per_1k: "冲突信号密度（每千字）",
  subject_repeat_ratio: "主语重复率",
  opener_diversity: "句首多样性",
  self_repeat_ratio: "章内自重复率",
  hook_score: "章末钩子分",
  advancement_per_1k: "推进信号密度（每千字）",
  filler_paragraph_ratio: "无推进段落占比",
  exposition_per_1k: "名词解释密度（每千字）",
  punctuation_entropy: "标点类型熵（越高越有起伏）",
  emotion_flatness: "情绪档位集中度（越低越有起伏）",
  pattern_loop_ratio: "句式骨架重复率",
  word_ttr: "用词多样性（滑动窗口）",
  word_concentration: "高频词集中度",
  verb_variety: "动词多样性",
  novel_bigram_ratio: "新搭配占比（参考值）",
  novel_char_ratio: "生僻字占比（本书没用过的字）",
  summary_per_1k: "总结回扣句密度（每千字）",
  elevation_per_1k: "升华议论密度（每千字）",
  transition_per_1k: "过渡连接词密度（每千字）",
  abstract_unsupported_ratio: "抽象替代具体比例",
  dash_ellipsis_per_1k: "破折号/省略号密度（每千字）",
  colloquial_per_1k: "口语词密度（每千字）",
  question_ratio: "疑问句占比",
  punctuation_variety: "标点种类比例",
  short_paragraph_ratio: "短段占比",
};

export function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? key;
}

/** 指标名的单位/说明后缀；出现在括号里会嵌套，展示短标签时去掉。 */
const METRIC_LABEL_SUFFIXES: string[] = [
  "（每千字）",
  "（越高越有起伏）",
  "（越低越有起伏）",
  "（本书没用过的字）",
  "（滑动窗口）",
  "（参考值）",
];

/** 括号内展示用的短指标名：去掉单位/说明后缀，避免出现 `CODE（名称（后缀））` 的嵌套括号。 */
export function metricShortLabel(key: string): string {
  const label = metricLabel(key);
  for (const suffix of METRIC_LABEL_SUFFIXES) {
    if (label.endsWith(suffix)) return label.slice(0, -suffix.length);
  }
  return label;
}

export function formatMetric(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(3);
}

export function formatScore(value: number): string {
  return Number.isFinite(value) ? value.toFixed(1) : "—";
}

/** 从基线的 metrics 里取某个指标的分布；不是分布结构就返回 null。 */
export function metricStat(value: unknown): StyleMetricStat | null {
  if (value === null || typeof value !== "object") return null;
  const candidate = value as Partial<StyleMetricStat>;
  return typeof candidate.mean === "number" ? (candidate as StyleMetricStat) : null;
}

/** 套话命中词 → 次数，按次数从多到少。 */
export function clicheHits(metrics: Record<string, any>): [string, number][] {
  const hits = metrics.cliche_hits;
  if (hits === null || typeof hits !== "object") return [];
  return Object.entries(hits as Record<string, number>)
    .filter(([, count]) => typeof count === "number")
    .sort((a, b) => b[1] - a[1]);
}

export function scoreClass(score: number): string {
  if (score >= 85) return "ok";
  if (score >= 70) return "warning";
  return "error";
}

/** 数值指标表；带上基线均值时多一列便于比较。 */
export function MetricTable({
  metrics,
  baseline,
}: {
  metrics: Record<string, any>;
  baseline?: Record<string, any>;
}) {
  const numericKeys = Object.keys(metrics).filter((key) => typeof metrics[key] === "number");
  const ordered = [
    ...KEY_METRICS.filter((key) => numericKeys.includes(key)),
    ...numericKeys.filter((key) => !KEY_METRICS.includes(key)).sort(),
  ];
  if (ordered.length === 0) return <div className="hint">本次没有可展示的数值指标。</div>;
  return (
    <table className="grid metric-table">
      <thead>
        <tr>
          <th>指标</th>
          <th>本次</th>
          {baseline && <th>基线均值</th>}
        </tr>
      </thead>
      <tbody>
        {ordered.map((key) => {
          const mean = baseline ? metricStat(baseline[key])?.mean ?? null : null;
          return (
            <tr key={key}>
              <td>{metricLabel(key)}</td>
              <td>{formatMetric(metrics[key] as number)}</td>
              {baseline && <td>{mean === null ? "—" : formatMetric(mean)}</td>}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** 基线分布表（均值 / 标准差 / 最小值 / 最大值）。 */
export function BaselineMetricTable({ metrics }: { metrics: Record<string, any> }) {
  const rows = Object.entries(metrics)
    .map(([key, value]) => ({ key, stat: metricStat(value) }))
    .filter((row): row is { key: string; stat: StyleMetricStat } => row.stat !== null);
  if (rows.length === 0) return <div className="hint">基线里没有可展示的指标。</div>;
  return (
    <table className="grid metric-table">
      <thead>
        <tr>
          <th>指标</th>
          <th>均值</th>
          <th>标准差</th>
          <th>最小</th>
          <th>最大</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ key, stat }) => (
          <tr key={key}>
            <td>{metricLabel(key)}</td>
            <td>{formatMetric(stat.mean)}</td>
            <td>{formatMetric(stat.std)}</td>
            <td>{formatMetric(stat.min)}</td>
            <td>{formatMetric(stat.max)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function StyleIssueList({ issues }: { issues: StyleIssue[] }) {
  if (issues.length === 0) {
    return (
      <div className="issue">
        <div className="issue-title">文风上没有发现明显偏离</div>
      </div>
    );
  }
  return (
    <div>
      {issues.map((issue, index) => (
        <div key={`${issue.code}-${index}`} className={`issue ${issue.level}`}>
          <div className="issue-title">
            <span className={`badge ${issue.level}`}>
              {issue.level === "warning" ? "警告" : "提示"}
            </span>{" "}
            {issue.code}
            {issue.metric && <span className="hint">（{metricShortLabel(issue.metric)}）</span>}
          </div>
          <div>{issue.message}</div>
          <div className="hint">
            本次 {formatMetric(issue.value)}
            {typeof issue.reference === "number"
              ? `｜基线均值 ${formatMetric(issue.reference)}`
              : ""}
          </div>
          {issue.excerpt && <div className="payload">原文片段：{issue.excerpt}</div>}
          {issue.suggestion && <div className="fix">建议：{issue.suggestion}</div>}
        </div>
      ))}
    </div>
  );
}

/** 一次文风评审的完整展示：分数、指标、问题、模型读感与提示。 */
export function StyleReviewView({ review }: { review: StyleReview }) {
  const hits = clicheHits(review.metrics);
  return (
    <div>
      <div className="field-row" style={{ alignItems: "center", marginBottom: 4 }}>
        <span className={`score-value ${scoreClass(review.score)}`}>{formatScore(review.score)}</span>
        <span className="hint">
          文风得分（满分 100）
          {review.baseline ? `｜对比基线「${review.baseline}」` : "｜暂无基线，用经验阈值判定"}
          {review.provider ? `｜${review.provider}${review.model ? `/${review.model}` : ""}` : ""}
        </span>
      </div>
      {review.warnings.length > 0 && (
        <div className="issue warning">
          {review.warnings.map((warning, index) => (
            <div key={index}>{warning}</div>
          ))}
        </div>
      )}
      {review.model_summary && (
        <div className="issue">
          <div className="issue-title">模型读感</div>
          <div style={{ whiteSpace: "pre-wrap" }}>{review.model_summary}</div>
        </div>
      )}
      <MetricTable metrics={review.metrics} baseline={review.baseline_metrics} />
      {hits.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div className="hint">
            套话命中（共 {hits.reduce((total, [, count]) => total + count, 0)} 次）：
          </div>
          <div className="field-row">
            {hits.map(([word, count]) => (
              <span key={word} className="badge warning">
                {word} ×{count}
              </span>
            ))}
          </div>
        </div>
      )}
      <div style={{ marginTop: 8 }}>
        <StyleIssueList issues={review.issues} />
      </div>
    </div>
  );
}
