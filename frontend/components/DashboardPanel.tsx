"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { Dashboard, Novel, RetrievalResult, SweepMode, SweepRun } from "@/lib/types";

interface Props {
  novelId: string;
  provider: string;
  dashboard: Dashboard | null;
  novel: Novel | null;
  onNovelSaved: (novel: Novel) => void;
  onRefresh: () => Promise<void>;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

const LEVEL_CLASS: Record<string, string> = {
  error: "error",
  warning: "warning",
  info: "info",
};

const LEVEL_LABELS: Record<string, string> = {
  error: "错误",
  warning: "警告",
  info: "提示",
};

const FRAGMENT_KIND_LABELS: Record<string, string> = {
  WHIM: "奇思妙想",
  IMAGE: "画面",
  LINE: "想写的句子",
  SCENE: "场景",
  THEME: "主题看法",
  CHARACTER: "人物瞬间",
  MECHANIC: "桥段机制",
  OTHER: "其他",
};

/** 「本书设定与大纲」的草稿：数字字段用字符串存，空串表示不改这一项。 */
interface SettingDraft {
  genre: string;
  synopsis: string;
  worldview: string;
  outline: string;
  chapterWordsMin: string;
  chapterWordsMax: string;
  dailyWordsTarget: string;
}

/** 每章字数区间的上下限与每日目标的下限都由后端限制，这里先挡一次。 */
const WORDS_MIN_LIMIT = 200;
const WORDS_MAX_LIMIT = 20000;
const DAILY_MAX_LIMIT = 100000;

function numberField(value: string, label: string, max: number): number | undefined {
  const trimmed = value.trim();
  if (trimmed === "") return undefined;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < WORDS_MIN_LIMIT || parsed > max) {
    throw new Error(`${label}要填 ${WORDS_MIN_LIMIT}~${max} 之间的整数`);
  }
  return Math.floor(parsed);
}

export function DashboardPanel({
  novelId,
  provider,
  dashboard,
  novel,
  onNovelSaved,
  onRefresh,
  setStatus,
  setError,
}: Props) {
  const [running, setRunning] = useState<SweepMode | null>(null);
  const [run, setRun] = useState<SweepRun | null>(null);
  const [reindexLog, setReindexLog] = useState("");
  const [reindexing, setReindexing] = useState(false);
  const [searchText, setSearchText] = useState("");
  const [useVector, setUseVector] = useState(true);
  const [searching, setSearching] = useState(false);
  const [retrieval, setRetrieval] = useState<RetrievalResult | null>(null);
  const [setting, setSetting] = useState<SettingDraft | null>(null);
  const [savingSetting, setSavingSetting] = useState(false);
  const [exportFrom, setExportFrom] = useState("");
  const [exportTo, setExportTo] = useState("");
  const [exporting, setExporting] = useState<"txt" | "md" | null>(null);

  const currentSetting: SettingDraft = {
    genre: novel?.genre ?? "",
    synopsis: novel?.synopsis ?? "",
    worldview: novel?.worldview ?? "",
    outline: novel?.outline ?? "",
    chapterWordsMin: String(novel?.chapter_words_min ?? 2000),
    chapterWordsMax: String(novel?.chapter_words_max ?? 3000),
    dailyWordsTarget: String(novel?.daily_words_target ?? 4000),
  };
  const settingDraft = setting ?? currentSetting;
  // 数字字段也按文本比对：「2000」与 2000 视为没有改动
  const settingDirty =
    setting !== null &&
    (Object.keys(currentSetting) as (keyof SettingDraft)[]).some(
      (key) => setting[key] !== currentSetting[key],
    );

  function editSetting(patch: Partial<SettingDraft>) {
    setSetting({ ...settingDraft, ...patch });
  }

  async function saveSetting() {
    if (!setting) return;
    let numbers: {
      chapter_words_min?: number;
      chapter_words_max?: number;
      daily_words_target?: number;
    };
    try {
      numbers = {
        chapter_words_min: numberField(setting.chapterWordsMin, "每章字数下限", WORDS_MAX_LIMIT),
        chapter_words_max: numberField(setting.chapterWordsMax, "每章字数上限", WORDS_MAX_LIMIT),
        daily_words_target: numberField(setting.dailyWordsTarget, "每日更新目标", DAILY_MAX_LIMIT),
      };
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      return;
    }
    setSavingSetting(true);
    setError("");
    try {
      const updated = await api.updateNovel(novelId, {
        genre: setting.genre,
        synopsis: setting.synopsis,
        worldview: setting.worldview,
        outline: setting.outline,
        ...numbers,
      });
      onNovelSaved(updated);
      setSetting(null);
      setStatus(
        "本书设定已保存：之后的规划、写作、碎片成文都会带上它（之前写过的章节不受影响）；" +
          "每章字数区间与每日更新目标用于发布前检查。",
      );
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setSavingSetting(false);
    }
  }

  /** 导出整本或一段；txt 由后端去掉 Markdown 标记，用于粘贴/上传到平台后台。 */
  async function exportText(fmt: "txt" | "md") {
    setExporting(fmt);
    setError("");
    setStatus(`正在导出 ${fmt}…`);
    try {
      const from = Number(exportFrom);
      const to = Number(exportTo);
      const { text, filename } = await api.exportNovelText(novelId, {
        fmt,
        fromChapter: Number.isFinite(from) && from >= 1 ? Math.floor(from) : undefined,
        toChapter: Number.isFinite(to) && to >= 1 ? Math.floor(to) : undefined,
      });
      downloadText(filename, text);
      setStatus(
        `已导出 ${filename}（${text.length} 字符）` +
          (fmt === "txt"
            ? "：txt 已去掉 Markdown 标记、章节之间留了空行，可以直接粘贴或上传到番茄后台"
            : "：md 保留原始 Markdown 标记"),
      );
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setExporting(null);
    }
  }

  async function runSweep(mode: SweepMode) {
    setRunning(mode);
    setError("");
    setStatus(
      mode === "rules"
        ? "正在跑规则扫描（确定性规则，不需要模型）…"
        : "正在深度扫描：会逐章调用模型抽取与审校，章节多时耗时较长…",
    );
    try {
      const result = await api.sweep(novelId, { mode, provider });
      setRun(result);
      setStatus(
        `${mode === "rules" ? "规则扫描" : "深度扫描"}完成：检查 ${result.chapters_checked}/${result.chapters_total} 章，` +
          `${result.errors} 个错误、${result.warnings} 个警告`,
      );
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setRunning(null);
    }
  }

  async function reindex() {
    setReindexing(true);
    setError("");
    try {
      setStatus("正在重建向量索引…");
      const result = await api.reindexVectors(novelId, "local");
      const entities = Object.entries(result.entities)
        .map(([key, value]) => `${key} ${value}`)
        .join("、");
      setReindexLog(
        `已重建：provider=${result.provider}，维度 ${result.dim}，章节 ${result.chapters} 章 / ` +
          `${result.chapter_chunks} 个片段；实体：${entities || "无"}` +
          (result.warnings.length > 0 ? `；提示：${result.warnings.join("；")}` : ""),
      );
      setStatus("向量索引已重建");
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setReindexing(false);
    }
  }

  async function runRetrieval() {
    if (!searchText.trim()) return;
    setSearching(true);
    setError("");
    try {
      setRetrieval(
        await api.retrieval(novelId, { q: searchText.trim(), limit: 8, useVector }),
      );
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setSearching(false);
    }
  }

  const summary = run ?? dashboard?.latest_sweep ?? null;
  const chapterHealth = [...(dashboard?.chapters ?? [])].sort(
    (a, b) => a.chapter_number - b.chapter_number,
  );
  const errorTotals = Object.entries(dashboard?.error_totals ?? {}).sort((a, b) => b[1] - a[1]);
  const invariantCodes = Object.entries(dashboard?.invariant_codes ?? {}).sort(
    (a, b) => b[1] - a[1],
  );
  // 总览只摘要前几条不变量问题，全文在「质量」标签里
  const invariantIssues = (dashboard?.invariant_issues ?? []).slice(0, 5);
  const styleAverage = dashboard?.style_review_average ?? null;
  const fragmentKinds = Object.entries(dashboard?.fragment_kinds ?? {}).sort((a, b) => b[1] - a[1]);

  return (
    <div>
      <div className="issue" style={{ marginBottom: 10 }}>
        <div className="issue-title">本书设定与大纲</div>
        <div className="hint" style={{ marginBottom: 6 }}>
          写在这里的东西会进规划、写作与碎片成文的提示词。简介写这本书是什么，
          世界观写规则与势力，全书大纲写主线、分卷与结局走向。
        </div>
        <div className="field-row" style={{ marginBottom: 6 }}>
          <label>
            类型
            <input
              value={settingDraft.genre}
              placeholder="东方玄幻 / 都市异能 / 悬疑…"
              onChange={(event) => editSetting({ genre: event.target.value })}
            />
          </label>
        </div>
        <label className="block">
          简介
          <textarea
            rows={2}
            value={settingDraft.synopsis}
            placeholder="一两句话：谁、要什么、拦着他的是什么"
            onChange={(event) => editSetting({ synopsis: event.target.value })}
          />
        </label>
        <label className="block">
          世界观
          <textarea
            rows={4}
            value={settingDraft.worldview}
            placeholder="力量体系、地理、势力、时代背景"
            onChange={(event) => editSetting({ worldview: event.target.value })}
          />
        </label>
        <label className="block">
          全书大纲
          <textarea
            rows={6}
            value={settingDraft.outline}
            placeholder="主线走向、分卷安排、人物弧线、结局；也可以只写你目前想到的"
            onChange={(event) => editSetting({ outline: event.target.value })}
          />
        </label>
        <div className="field-row" style={{ marginTop: 6 }}>
          <label>
            每章字数下限
            <input
              type="number"
              min={WORDS_MIN_LIMIT}
              style={{ width: 110 }}
              value={settingDraft.chapterWordsMin}
              onChange={(event) => editSetting({ chapterWordsMin: event.target.value })}
            />
          </label>
          <label>
            每章字数上限
            <input
              type="number"
              min={WORDS_MIN_LIMIT}
              style={{ width: 110 }}
              value={settingDraft.chapterWordsMax}
              onChange={(event) => editSetting({ chapterWordsMax: event.target.value })}
            />
          </label>
          <label>
            每日更新目标（字）
            <input
              type="number"
              min={WORDS_MIN_LIMIT}
              style={{ width: 110 }}
              value={settingDraft.dailyWordsTarget}
              onChange={(event) => editSetting({ dailyWordsTarget: event.target.value })}
            />
          </label>
        </div>
        <div className="hint">
          每章字数区间用于发布前检查；平台福利按每日有效字数算，默认 4000。留空表示不改这一项。
        </div>
        <div className="field-row" style={{ marginTop: 6 }}>
          <button className="primary" onClick={() => void saveSetting()} disabled={!settingDirty || savingSetting}>
            {savingSetting ? "保存中…" : "保存本书设定"}
          </button>
          <button onClick={() => setSetting(null)} disabled={!settingDirty}>
            撤销改动
          </button>
          {settingDirty && <span className="hint">有未保存的改动</span>}
        </div>
      </div>

      <div className="issue" style={{ marginBottom: 10 }}>
        <div className="issue-title">导出为发布稿</div>
        <div className="field-row" style={{ marginBottom: 6 }}>
          <label style={{ flex: "0 0 auto" }}>
            起始章号（留空 = 从第 1 章）
            <input
              type="number"
              min={1}
              style={{ width: 110 }}
              value={exportFrom}
              placeholder="可留空"
              onChange={(event) => setExportFrom(event.target.value)}
            />
          </label>
          <label style={{ flex: "0 0 auto" }}>
            结束章号（留空 = 到最后一章）
            <input
              type="number"
              min={1}
              style={{ width: 110 }}
              value={exportTo}
              placeholder="可留空"
              onChange={(event) => setExportTo(event.target.value)}
            />
          </label>
        </div>
        <div className="field-row">
          <button className="primary" onClick={() => void exportText("txt")} disabled={exporting !== null}>
            {exporting === "txt" ? "导出中…" : "导出为 txt（可粘贴/上传到番茄后台）"}
          </button>
          <button onClick={() => void exportText("md")} disabled={exporting !== null}>
            {exporting === "md" ? "导出中…" : "导出 md"}
          </button>
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          txt 是去掉 Markdown 标记的纯文本（#/**/列表符号都清掉），章节之间留了空行，按「第N章 标题」
          单独成行，可以直接粘到平台后台或整本上传；md 保留原始标记，用于自己留档或另投。
        </div>
      </div>

      <div className="field-row">
        <button onClick={() => void runSweep("rules")} disabled={running !== null}>
          {running === "rules" ? "规则扫描中…" : "跑一遍规则扫描（快）"}
        </button>
        <button
          className="primary"
          onClick={() => void runSweep("full")}
          disabled={running !== null}
        >
          {running === "full" ? "深度扫描中…" : "深度扫描（full，会调用模型）"}
        </button>
      </div>
      <div className="hint" style={{ marginTop: 4 }}>
        规则扫描只做确定性检查，秒级完成；深度扫描会逐章调用模型抽取与审校，章节多时耗时长，
        也会产生模型费用。
      </div>

      {summary && (
        <div className="issue" style={{ marginTop: 8 }}>
          <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {run ? "本次扫描" : "最近一次扫描"}
            <span className="spacer" />
            <span className={`badge ${summary.mode === "full" ? "proposed" : ""}`}>
              {summary.mode}
            </span>
          </div>
          <div>
            检查 {summary.chapters_checked}/{summary.chapters_total} 章｜错误 {summary.errors}｜警告{" "}
            {summary.warnings}｜耗时 {formatDuration(summary.started_at, summary.finished_at)}
            {summary.provider ? `｜provider=${summary.provider}` : ""}
            {summary.model ? `/${summary.model}` : ""}
          </div>
          <div className="hint">
            {formatTime(summary.started_at)} 开始
            {summary.finished_at ? `，${formatTime(summary.finished_at)} 结束` : "（尚未结束）"}
          </div>
        </div>
      )}

      {dashboard?.notes.map((note, index) => (
        <div key={index} className="hint">
          · {note}
        </div>
      ))}

      <div className="issue" style={{ marginTop: 8 }}>
        <div className="issue-title">每章健康</div>
        <div className="hint">
          共 {dashboard?.chapter_count ?? 0} 章｜{dashboard?.word_count ?? 0} 字 / 目标{" "}
          {dashboard?.target_word_count ?? 0}｜有错误 {dashboard?.error_chapters.length ?? 0} 章｜未检查{" "}
          {dashboard?.unchecked_chapters.length ?? 0} 章
        </div>
      </div>
      <table className="grid">
        <thead>
          <tr>
            <th>章号</th>
            <th>标题</th>
            <th>字数</th>
            <th>错误</th>
            <th>警告</th>
            <th>问题类型</th>
            <th>最近检查</th>
          </tr>
        </thead>
        <tbody>
          {chapterHealth.map((item) => (
            <tr key={item.chapter_number}>
              <td>第{item.chapter_number}章</td>
              <td>{item.title || "（无标题）"}</td>
              <td>{item.word_count}</td>
              <td>
                {item.errors > 0 ? (
                  <span className="badge error">{item.errors}</span>
                ) : (
                  item.errors
                )}
              </td>
              <td>{item.warnings}</td>
              <td>{item.top_codes.length > 0 ? item.top_codes.join("、") : "—"}</td>
              <td>
                {item.checked_at ? (
                  formatTime(item.checked_at)
                ) : (
                  <span className="badge warning">未检查</span>
                )}
              </td>
            </tr>
          ))}
          {chapterHealth.length === 0 && (
            <tr>
              <td colSpan={7}>还没有章节。</td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="issue" style={{ marginTop: 10 }}>
        <div className="issue-title">错误类型统计</div>
        {errorTotals.length === 0 && <div className="hint">暂无错误记录。</div>}
        <ul>
          {errorTotals.map(([code, count]) => (
            <li key={code}>
              <span className="badge error">{count}</span> {code}
            </li>
          ))}
        </ul>
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          全局不变量
          <span className="spacer" />
          {(dashboard?.invariant_errors ?? 0) > 0 ? (
            <span className="badge error">{dashboard?.invariant_errors} 错误</span>
          ) : (
            <span className="badge ok">无错误</span>
          )}
          <span className={`badge ${(dashboard?.invariant_warnings ?? 0) > 0 ? "warning" : ""}`}>
            {dashboard?.invariant_warnings ?? 0} 警告
          </span>
        </div>
        {invariantCodes.length === 0 && <div className="hint">没有命中的不变量检查项。</div>}
        {invariantCodes.length > 0 && (
          <div className="field-row">
            {invariantCodes.map(([code, count]) => (
              <span key={code} className="badge warning">
                {code} ×{count}
              </span>
            ))}
          </div>
        )}
        {invariantIssues.map((issue, index) => (
          <div key={`${issue.code}-${index}`} className="item-row">
            <div className="row-head">
              <span className={`badge ${LEVEL_CLASS[issue.level] ?? ""}`}>
                {LEVEL_LABELS[issue.level] ?? issue.level}
              </span>
              <b>{issue.code}</b>
              {issue.subject && <span className="hint">{issue.subject}</span>}
            </div>
            <div className="payload">{issue.message}</div>
          </div>
        ))}
        {(dashboard?.invariant_issues.length ?? 0) > invariantIssues.length && (
          <div className="hint">
            共 {dashboard?.invariant_issues.length} 条，其余请在「质量」标签里查看。
          </div>
        )}
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          承诺账本
          <span className="spacer" />
          <span className="hint" style={{ color: "inherit" }}>
            未到期 {dashboard?.commitments_open ?? 0}｜逾期{" "}
            <b className={(dashboard?.commitments_overdue ?? 0) > 0 ? "error-text" : ""}>
              {dashboard?.commitments_overdue ?? 0}
            </b>
          </span>
        </div>
        {(dashboard?.commitments_overdue_items.length ?? 0) === 0 && (
          <div className="hint">没有逾期的承诺。</div>
        )}
        {dashboard?.commitments_overdue_items.map((item) => (
          <div key={item.id} className="item-row">
            <div className="row-head">
              <span className="badge error">OVERDUE</span>
              <span className="badge">{item.kind}</span>
              <b>{item.what}</b>
              <span className="spacer" />
              <span className="hint">
                {item.source_chapter ? `第${item.source_chapter}章` : "来源未知"}
              </span>
            </div>
            <div className="payload">
              期限：{item.deadline_text || "—"}
              {item.due_story_time ? `（到期 ${item.due_story_time}）` : ""}
              {item.breach_chapter ? `｜第${item.breach_chapter}章越界` : ""}
              {item.who ? `｜当事人 ${item.who}` : ""}
              {item.counterpart ? ` → ${item.counterpart}` : ""}
            </div>
          </div>
        ))}
        {(dashboard?.commitments_overdue ?? 0) >
          (dashboard?.commitments_overdue_items.length ?? 0) && (
          <div className="hint">只列出前 {dashboard?.commitments_overdue_items.length} 条。</div>
        )}
      </div>

      <div className="issue">
        <div className="issue-title">文风</div>
        <div>
          基线：<b>{dashboard?.style_baseline || "未建立"}</b>｜历史评审平均分：
          <b className={styleAverage !== null && styleAverage < 70 ? "error-text" : ""}>
            {styleAverage === null ? "—" : styleAverage.toFixed(1)}
          </b>
        </div>
        <div className="hint">
          基线与评审明细在「质量」标签：可建立基线、评草稿、重跑不变量并维护承诺账本。
        </div>
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          文风与断言
          <span className="spacer" />
          {dashboard?.style_locked ? (
            <span className="badge ok">文风已锁定</span>
          ) : (
            <span className="badge">文风未锁定</span>
          )}
          {(dashboard?.claim_conflicts ?? 0) > 0 ? (
            <span className="badge error">{dashboard?.claim_conflicts} 条断言冲突</span>
          ) : (
            <span className="badge ok">无断言冲突</span>
          )}
        </div>
        <div>
          漂移章节{" "}
          <b className={(dashboard?.style_drift_chapters ?? 0) > 0 ? "error-text" : ""}>
            {dashboard?.style_drift_chapters ?? 0}
          </b>
          ｜断言核对报告 <b>{dashboard?.claim_reports ?? 0}</b> 份｜冲突{" "}
          <b className={(dashboard?.claim_conflicts ?? 0) > 0 ? "error-text" : ""}>
            {dashboard?.claim_conflicts ?? 0}
          </b>
          ｜待确认{" "}
          <b className={(dashboard?.claim_unverified ?? 0) > 0 ? "error-text" : ""}>
            {dashboard?.claim_unverified ?? 0}
          </b>
        </div>
        <div className="hint">
          {dashboard?.style_locked
            ? "文风已锁定：新章按收紧一半的窗口比对，漂出窗口的章会被点名。"
            : "文风未锁定：只按基线窗口比对；锁定后新章会按收紧一半的窗口比对。"}
          漂移章数只在锁定文风后才统计。「质量」标签里可以贴草稿核对设定断言、锁定文风基线并逐章看漂移；
          断言核对只出报告，不会自动写 Canon。
        </div>
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          想法碎片
          <span className="spacer" />
          <span className="hint" style={{ color: "inherit" }}>
            成文率 {((dashboard?.fragment_realization_rate ?? 0) * 100).toFixed(0)}%
          </span>
        </div>
        <div>
          总数 <b>{dashboard?.fragments_total ?? 0}</b>｜未安排{" "}
          <b>{dashboard?.fragments_unplaced ?? 0}</b>｜已成文{" "}
          <b>{dashboard?.fragments_realized ?? 0}</b>
        </div>
        <div className="field-row" style={{ marginTop: 4 }}>
          {fragmentKinds.length === 0 && <span className="hint">还没有碎片。</span>}
          {fragmentKinds.map(([kind, count]) => (
            <span key={kind} className="badge">
              {FRAGMENT_KIND_LABELS[kind] ?? kind} ×{count}
            </span>
          ))}
        </div>
        <div className="hint">
          「碎片」标签里可以随手记、让系统提问、按章安排，并把碎片写成正文。
        </div>
      </div>

      <div className="issue">
        <div className="issue-title">作者声音</div>
        {dashboard?.voice_profile ? (
          <>
            <div>
              画像：<b>{dashboard.voice_profile}</b>｜特征词 {dashboard.voice_terms.length} 个
            </div>
            <div className="field-row" style={{ marginTop: 4 }}>
              {dashboard.voice_terms.slice(0, 20).map((term) => (
                <span key={term} className="badge ok">
                  {term}
                </span>
              ))}
            </div>
            <div className="hint">
              声音保留分衡量「像不像你」（越高越好），与「有没有 AI 味」是两项不同的指标：
              成文结果里会同时给出。
            </div>
          </>
        ) : (
          <div className="hint">
            还没有作者声音画像：可在「质量」面板用你自己的章节或文本建立，之后成文会附带
            「声音保留分」。
          </div>
        )}
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          伏笔欠账
          <span className="spacer" />
          <span className="hint" style={{ color: "inherit" }}>
            共 {dashboard?.foreshadowing_debt.length ?? 0} 条，overdue{" "}
            {dashboard?.overdue_foreshadowing ?? 0} 条
          </span>
        </div>
        {(dashboard?.foreshadowing_debt.length ?? 0) === 0 && (
          <div className="hint">当前没有欠账伏笔。</div>
        )}
        {dashboard?.foreshadowing_debt.map((item) => (
          <div key={item.id} className="item-row">
            <div className="row-head">
              <b>{item.name}</b>
              <span className="badge">{item.status}</span>
              {item.overdue && <span className="badge error">已欠账</span>}
              <span className="spacer" />
              <span className="hint">已 {item.age} 章未推进</span>
            </div>
            <div className="payload">
              首次出现：{item.first_chapter ? `第${item.first_chapter}章` : "—"}｜最近强化：
              {item.last_reinforced_chapter ? `第${item.last_reinforced_chapter}章` : "—"}｜相关人物：
              {item.related_characters.join("、") || "—"}
            </div>
            {item.description && <div className="payload">说明：{item.description}</div>}
            {item.expected_payoff && <div className="payload">期望回收：{item.expected_payoff}</div>}
            {item.evidence && <div className="payload">证据：{item.evidence}</div>}
          </div>
        ))}
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          向量索引
          <span className="spacer" />
          <button onClick={() => void reindex()} disabled={reindexing}>
            {reindexing ? "重建中…" : "重建索引"}
          </button>
        </div>
        <div>
          记录数 <b>{dashboard?.vector_index.records ?? 0}</b>｜维度 <b>{dashboard?.vector_index.dim ?? 0}</b>
          ｜provider：{dashboard?.vector_index.providers.join("、") || "—"}
        </div>
        <div className="hint">
          {Object.entries(dashboard?.vector_index.by_ref_type ?? {})
            .map(([key, value]) => `${key} ${value}`)
            .join("｜") || "暂无向量记录，可点击「重建索引」生成"}
        </div>
        {reindexLog && <div className="hint">{reindexLog}</div>}
      </div>

      <div className="issue">
        <div className="issue-title">混合检索试跑</div>
        <div className="field-row">
          <input
            style={{ flex: 1, minWidth: 200 }}
            placeholder="检索词，例如：玄铁令"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void runRetrieval();
            }}
          />
          <label style={{ flexDirection: "row", alignItems: "center", flex: "0 0 auto" }}>
            <input
              type="checkbox"
              checked={useVector}
              onChange={(event) => setUseVector(event.target.checked)}
              style={{ width: "auto" }}
            />
            使用向量
          </label>
          <button className="primary" onClick={() => void runRetrieval()} disabled={searching}>
            {searching ? "检索中…" : "检索"}
          </button>
        </div>
        {retrieval && (
          <>
            <div className="hint" style={{ marginTop: 6 }}>
              engine={retrieval.engine}｜通道：{retrieval.channels.join("、") || "—"}｜命中{" "}
              {retrieval.hits.length} 条
            </div>
            {retrieval.hits.map((hit, index) => (
              <div key={`${hit.ref_id}-${index}`} className="item-row">
                <div className="row-head">
                  <span className="badge">{hit.ref_type}</span>
                  <b>
                    {hit.chapter_number ? `第${hit.chapter_number}章 ` : ""}
                    {hit.title || "（无标题）"}
                  </b>
                  <span className="spacer" />
                  {hit.channels.map((channel) => (
                    <span key={channel} className="badge">
                      {channel}
                    </span>
                  ))}
                </div>
                <div className="payload">{hit.excerpt}</div>
                <div className="payload">
                  score={hit.score.toFixed(4)}｜keyword={hit.keyword_score.toFixed(4)}｜vector=
                  {hit.vector_score.toFixed(4)}｜term_weight={hit.term_weight.toFixed(4)}
                  {hit.terms_matched.length > 0 ? `｜命中词：${hit.terms_matched.join("、")}` : ""}
                </div>
              </div>
            ))}
            {retrieval.hits.length === 0 && <div className="hint">没有命中任何片段。</div>}
          </>
        )}
      </div>
    </div>
  );
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  return value.slice(0, 19).replace("T", " ");
}

function formatDuration(startedAt: string, finishedAt: string | null): string {
  if (!finishedAt) return "进行中";
  const started = new Date(startedAt).getTime();
  const finished = new Date(finishedAt).getTime();
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return "—";
  return `${((finished - started) / 1000).toFixed(1)} 秒`;
}

/** 用 Blob + a[download] 触发下载：导出接口回的是纯文本，不能用 JSON 封装。 */
function downloadText(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 立刻撤销会让部分浏览器丢掉还在读取的 Blob，等一拍再释放
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
