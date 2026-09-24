"use client";

import type { Chapter, ClaimEvidence, ClaimReport, CompletionReport } from "@/lib/types";

export interface DraftState {
  title: string;
  content: string;
  summary: string;
  story_time: string;
  location: string;
}

interface Props {
  chapter: Chapter | null;
  draft: DraftState | null;
  dirty: boolean;
  busy: boolean;
  report: CompletionReport | null;
  onChange: (patch: Partial<DraftState>) => void;
  onSave: () => void;
  onCompleteChapter: () => void;
  status: string;
}

export function EditorPanel({
  chapter,
  draft,
  dirty,
  busy,
  report,
  onChange,
  onSave,
  onCompleteChapter,
  status,
}: Props) {
  if (!chapter || !draft) {
    return (
      <div className="column">
        <div className="column-header">正文编辑器</div>
        <div className="column-body">
          <p className="hint">
            左侧选择或新建一个章节开始写作。保存后可以调用 AI 助手做信息抽取、一致性检查、以及根据前文生成新章节。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="column">
      <div className="editor-toolbar">
        <span className="badge">第{chapter.chapter_number}章</span>
        <input
          className="editor-title"
          value={draft.title}
          onChange={(event) => onChange({ title: event.target.value })}
          placeholder="章节标题"
        />
        <input
          style={{ width: 130 }}
          value={draft.story_time}
          onChange={(event) => onChange({ story_time: event.target.value })}
          placeholder="故事时间"
        />
        <input
          style={{ width: 110 }}
          value={draft.location}
          onChange={(event) => onChange({ location: event.target.value })}
          placeholder="地点"
        />
        <button className="primary" onClick={onSave} disabled={busy || !dirty}>
          {dirty ? "保存" : "已保存"}
        </button>
        <button onClick={onCompleteChapter} disabled={busy}>
          完成本章（工作流）
        </button>
      </div>
      <textarea
        className="editor-textarea"
        value={draft.content}
        onChange={(event) => onChange({ content: event.target.value })}
        placeholder="在此写作正文…"
      />
      {report && (
        <div className="workflow-steps">
          <div className="hint">
            章节完成工作流 · 第 {report.steps[0]?.step}—{report.steps[report.steps.length - 1]?.step} 步
            （错误 {report.errors} / 警告 {report.warnings} / 待确认 {report.pending_items}）
          </div>
          <div className="steps">
            {report.steps.map((step) => (
              <span key={step.step} className="step" title={step.detail}>
                <span
                  className={`badge ${
                    step.status === "ok"
                      ? "ok"
                      : step.status === "error"
                        ? "error"
                        : step.status === "warning"
                          ? "warning"
                          : ""
                  }`}
                >
                  {step.step}
                </span>
                {step.name}
              </span>
            ))}
          </div>
          <div className="hint">{report.message}</div>
          {report.claims && <ClaimSummary report={report.claims} />}
          {(report.notes?.length ?? 0) > 0 && (
            <div>
              {(report.notes || []).map((note, index) => (
                <div key={`note-${index}`} className="hint">
                  · {note}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <div className="statusbar">
        <span>当前 {countChars(draft.content)} 字</span>
        <span>状态：{chapter.status === "DRAFT" ? "草稿" : "已完成"}</span>
        <span>{status}</span>
      </div>
    </div>
  );
}

function countChars(text: string): number {
  return (text || "").replace(/\s+/g, "").length;
}

function evidenceLine(evidence: ClaimEvidence): string {
  const text = evidence.text || "—";
  if (evidence.kind === "CANON_FACT") {
    return `Canon：${text}${evidence.source_chapter ? `（第 ${evidence.source_chapter} 章）` : ""}`;
  }
  if (evidence.kind === "WORLD_RULE") {
    return `世界观规则：${text}`;
  }
  return `${evidence.kind || "依据"}：${text}`;
}

/** 工作流跑完后的设定断言核对摘要：冲突逐条列出，待确认只给条数。 */
function ClaimSummary({ report }: { report: ClaimReport }) {
  const conflicts = report.conflicts || [];
  return (
    <div className="issue" style={{ marginTop: 4, marginBottom: 0 }}>
      <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
        设定断言核对
        <span className="spacer" />
        {report.conflict_count > 0 ? (
          <span className="badge error">{report.conflict_count} 冲突</span>
        ) : (
          <span className="badge ok">无冲突</span>
        )}
        <span className={`badge ${report.unverified_count > 0 ? "warning" : ""}`}>
          {report.unverified_count} 待确认
        </span>
        <span className="badge">{report.supported_count} 一致</span>
      </div>
      <div className="hint">
        共 {report.claim_count} 条断言｜
        {report.label === "CHAPTER"
          ? `第${report.chapter_number ?? "?"}章正文`
          : report.label || "草稿"}
        ｜{report.provider}/{report.model}｜核对只出报告，不会自动写 Canon。
      </div>
      {conflicts.length > 0 && (
        <div style={{ marginTop: 4, maxHeight: 200, overflow: "auto" }}>
          {conflicts.map((item, index) => (
            <div key={`conflict-${index}`} className="item-row">
              <div className="row-head">
                <span className="badge error">冲突</span>
                <b>
                  {`${item.claim?.subject || "—"}·${item.claim?.predicate || "—"} = ${
                    item.claim?.object || "—"
                  }`}
                </b>
              </div>
              {item.reason && <div className="payload">{item.reason}</div>}
              {item.claim?.quote && <div className="payload">原文：{item.claim.quote}</div>}
              {(item.evidence || []).map((entry, position) => (
                <div key={`evidence-${position}`} className="payload">
                  依据：{evidenceLine(entry)}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      {report.conflict_count === 0 && report.unverified_count > 0 && (
        <div className="hint">
          没有冲突；{report.unverified_count} 条查无记录的断言可在「质量 → 设定断言核对」里逐条看。
        </div>
      )}
    </div>
  );
}
