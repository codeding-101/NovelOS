"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { ReaderAnalysis, ReaderMetricRaw } from "@/lib/types";

interface Props {
  novelId: string;
  /** 章节增删会改变逐章判定与对照结果，需要重新拉取。 */
  chaptersVersion: number;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

/** 导入框的示例：从平台后台复制出来的样子（制表符分列）。 */
const IMPORT_PLACEHOLDER = "第1章\t1200\t23.5%\n第2章\t1050\t21.8%\n第3章,980,19.4%";

/** 后端给的是 0~1 的小数，展示成百分数统一保留一位小数。 */
function percent(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toFixed(1)}%`
    : "—";
}

function fixed(value: number | null | undefined, digits: number): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

/** 结论条的颜色：相符算好消息，过严/不明显/数据不足要留意。 */
function verdictTone(verdict: string): string {
  if (/相符/.test(verdict)) return "info";
  if (/过严|不明显|不足/.test(verdict)) return "warning";
  return "";
}

function chapterLabel(chapter: { chapter_number: number; title: string }): string {
  return `第${chapter.chapter_number}章 ${chapter.title || "（无标题）"}`;
}

export function ReaderPanel({ novelId, chaptersVersion, setStatus, setError }: Props) {
  const [analysis, setAnalysis] = useState<ReaderAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [text, setText] = useState("");
  const [importing, setImporting] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [message, setMessage] = useState("");
  const [raw, setRaw] = useState<ReaderMetricRaw[] | null>(null);
  const [rawBusy, setRawBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setAnalysis(await api.readerAnalysis(novelId));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setAnalysis(null);
    } finally {
      setLoading(false);
    }
  }, [novelId, setError]);

  useEffect(() => {
    void load();
  }, [load, chaptersVersion]);

  /** 数据变了以后原始行就作废了，顺手折叠回去。 */
  async function reload() {
    setRaw(null);
    await load();
  }

  async function importRows() {
    const body = text.trim();
    if (!body) return;
    setImporting(true);
    setError("");
    setMessage("");
    try {
      const result = await api.importReaderMetrics(novelId, { text: body });
      setMessage(result.message || `已导入 ${result.imported} 章的数据`);
      setStatus(result.message || `已导入 ${result.imported} 章的数据`);
      setText("");
      await reload();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setImporting(false);
    }
  }

  async function clearAll() {
    if (!window.confirm("清空整本已导入的读者数据？之后要重新从平台后台粘贴。")) return;
    setClearing(true);
    setError("");
    try {
      const result = await api.clearReaderMetrics(novelId);
      setMessage("");
      setStatus(`已清空 ${result.deleted} 章的读者数据`);
      await reload();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setClearing(false);
    }
  }

  async function toggleRaw() {
    if (raw !== null) {
      setRaw(null);
      return;
    }
    setRawBusy(true);
    setError("");
    try {
      setRaw(await api.readerRaw(novelId));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setRaw(null);
    } finally {
      setRawBusy(false);
    }
  }

  const chapters = analysis?.chapters ?? [];
  const coverage = analysis?.coverage ?? { with_data: 0, total: 0 };
  const hasData = coverage.with_data > 0;
  const bookMean = analysis?.book_completion_rate ?? null;

  return (
    <div>
      <div className="issue info">
        <div className="issue-title">读者数据回环</div>
        <div className="hint">
          这里的数据来自平台后台（阅读人数、完读率、追读率、收益、评论数），用来验证我们的判定对不对：
          我们报出来的弱章，读者是不是真的掉队了；我们没报警但读者明显掉队的，是哪一章。
          从后台复制表格直接粘贴即可，制表符、逗号或多空格都能分列。
        </div>
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          导入章节数据
          <span className="spacer" />
          {loading && <span className="hint" style={{ color: "inherit" }}>读取中…</span>}
        </div>
        <label className="block">
          粘贴平台后台的表格（每行至少「章号 + 阅读人数」，后面依次是完读率、追读率、收益、评论数，可省略）
          <textarea
            rows={5}
            value={text}
            placeholder={IMPORT_PLACEHOLDER}
            onChange={(event) => setText(event.target.value)}
          />
        </label>
        <div className="field-row" style={{ marginTop: 6 }}>
          <button className="primary" onClick={() => void importRows()} disabled={importing || !text.trim()}>
            {importing ? "导入中…" : "导入"}
          </button>
          <button className="danger" onClick={() => void clearAll()} disabled={clearing || !hasData}>
            {clearing ? "清空中…" : "清空整本"}
          </button>
          <button onClick={() => void toggleRaw()} disabled={rawBusy || !hasData}>
            {rawBusy ? "读取中…" : raw === null ? "查看原始行" : "收起原始行"}
          </button>
          <span className="hint">同一章号会覆盖；清空整本要二次确认。</span>
        </div>
        {message && <div className="hint" style={{ marginTop: 4 }}>{message}</div>}
        {raw !== null && (
          <div style={{ overflowX: "auto", marginTop: 6 }}>
            <table className="grid">
              <thead>
                <tr>
                  <th>章</th>
                  <th>阅读人数</th>
                  <th>完读率</th>
                  <th>追读率</th>
                  <th>收益</th>
                  <th>评论数</th>
                  <th>粘贴的原始行</th>
                </tr>
              </thead>
              <tbody>
                {raw.map((row) => (
                  <tr key={row.chapter_number}>
                    <td>{row.chapter_number}</td>
                    <td>{row.reads}</td>
                    <td>{percent(row.completion_rate)}</td>
                    <td>{percent(row.retention_rate)}</td>
                    <td>{row.revenue === null ? "—" : fixed(row.revenue, 2)}</td>
                    <td>{row.comments}</td>
                    <td>{row.raw?.line || "—"}</td>
                  </tr>
                ))}
                {raw.length === 0 && (
                  <tr>
                    <td colSpan={7}>还没有导入任何行。</td>
                  </tr>
                )}
              </tbody>
            </table>
            <div className="hint">对着「粘贴的原始行」核对一遍，确认没有抄错列。</div>
          </div>
        )}
      </div>

      {!hasData && (
        <div className="hint">{loading ? "正在读取已导入的数据…" : "导入之后才能验证判定是否有效。"}</div>
      )}

      {analysis && hasData && (
        <>
          <div className={`issue ${verdictTone(analysis.verdict)}`}>
            <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
              判定 vs 读者实际
              <span className="spacer" />
              <span className="badge">
                有数据的章 {coverage.with_data} / 共 {coverage.total}
              </span>
            </div>
            <div className="field-row">
              <span className="badge">全书平均完读率 {percent(bookMean)}</span>
              <span className="badge warning">弱章平均 {percent(analysis.weak_mean_completion)}</span>
              <span className="badge ok">其余章平均 {percent(analysis.ok_mean_completion)}</span>
            </div>
            <div style={{ fontWeight: 600, marginTop: 4 }}>{analysis.verdict}</div>
          </div>

          <div className="issue error">
            <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
              我们没报警但读者掉队了（漏报 {analysis.missed.length} 章）
              <span className="spacer" />
              <span className="hint" style={{ color: "inherit" }}>
                最重要的一组：我们的指标没抓住的东西
              </span>
            </div>
            {analysis.missed.length === 0 && <div className="hint">没有漏报的章。</div>}
            {analysis.missed.map((item) => (
              <div
                key={item.chapter_number}
                className="item-row"
                style={{ borderLeft: "3px solid var(--danger)", background: "var(--danger-soft)" }}
              >
                <div className="row-head">
                  <b>{chapterLabel(item)}</b>
                  <span className="badge error">完读率 {percent(item.completion_rate)}</span>
                  <span className="badge error">
                    比全书平均低 {fixed(Math.abs(item.gap_vs_book), 1)} 个百分点
                  </span>
                </div>
              </div>
            ))}
          </div>

          <div className="issue warning">
            <div className="issue-title">
              我们报警了但读者没跑（误报 {analysis.false_alarms.length} 章）
            </div>
            {analysis.false_alarms.length === 0 && <div className="hint">没有误报的章。</div>}
            {analysis.false_alarms.map((item) => (
              <div key={item.chapter_number} className="item-row">
                <div className="row-head">
                  <b>{chapterLabel(item)}</b>
                  <span className="badge warning">完读率 {percent(item.completion_rate)}</span>
                </div>
                <div className="payload">报出的原因：{item.reasons.join("；") || "—"}</div>
              </div>
            ))}
          </div>

          <div className="issue">
            <div className="issue-title">
              读者在哪里掉的（阅读人数比上一章下降 {analysis.drop_chapters.length} 章）
            </div>
            {analysis.drop_chapters.length === 0 && (
              <div className="hint">没有明显掉人的章（降幅 8% 以上才会列出来）。</div>
            )}
            {analysis.drop_chapters.map((item) => (
              <div key={item.chapter_number} className="item-row">
                <div className="row-head">
                  <b>{chapterLabel(item)}</b>
                  <span className="badge">阅读人数 {item.reads}</span>
                  <span className="badge error">
                    比第{item.from_chapter}章少 {fixed(Math.abs(item.change * 100), 1)}%
                  </span>
                </div>
              </div>
            ))}
          </div>

          <div className="issue">
            <div className="issue-title">校准建议</div>
            {analysis.suggestions.length === 0 && <div className="hint">暂时没有建议。</div>}
            <ul>
              {analysis.suggestions.map((suggestion, index) => (
                <li key={index}>{suggestion}</li>
              ))}
            </ul>
          </div>

          <div style={{ overflowX: "auto" }}>
            <table className="grid">
              <thead>
                <tr>
                  <th>章</th>
                  <th>标题</th>
                  <th>阅读人数</th>
                  <th>完读率</th>
                  <th>钩子分</th>
                  <th>推进密度</th>
                  <th>注水比例</th>
                  <th>判定</th>
                </tr>
              </thead>
              <tbody>
                {chapters.map((chapter) => {
                  // 与后端口径一致：判弱但完读率比全书平均高出 3 个百分点以上，才算误报
                  const overBook =
                    chapter.weak &&
                    chapter.completion_rate !== null &&
                    bookMean !== null &&
                    chapter.completion_rate > bookMean + 0.03;
                  return (
                    <tr
                      key={chapter.chapter_number}
                      style={overBook ? { backgroundColor: "var(--danger-soft)" } : undefined}
                    >
                      <td>{chapter.chapter_number}</td>
                      <td>{chapter.title || "—"}</td>
                      <td>{chapter.reads === null ? "—" : chapter.reads}</td>
                      <td>{percent(chapter.completion_rate)}</td>
                      <td>{fixed(chapter.hook_score, 2)}</td>
                      <td>{fixed(chapter.advancement_per_1k, 1)}</td>
                      <td>{percent(chapter.filler_paragraph_ratio)}</td>
                      <td>
                        <span className={`badge ${chapter.weak ? "warning" : "ok"}`}>
                          {chapter.weak ? "弱" : "正常"}
                        </span>
                        {overBook && <span className="badge error">误报</span>}
                        {chapter.weak_reasons.length > 0 && (
                          <div className="hint" style={{ color: "inherit" }}>
                            {chapter.weak_reasons.join("；")}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {chapters.length === 0 && (
                  <tr>
                    <td colSpan={8}>本书还没有章节。</td>
                  </tr>
                )}
              </tbody>
            </table>
            <div className="hint">
              完读率是平台给的实际数据，钩子分 / 推进密度 / 注水比例是我们的判定信号；标出的「误报」是
              「我们判弱、但完读率比全书平均高出 3 个百分点以上」的章（与上面误报清单同一口径）。
              没导入数据的章只有阅读人数与完读率为「—」。
            </div>
          </div>
        </>
      )}
    </div>
  );
}
