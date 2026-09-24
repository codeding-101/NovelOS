"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { formatScore, scoreClass } from "@/components/StyleReviewView";
import type { Fragment, FragmentRealizeResponse } from "@/lib/types";

interface Props {
  novelId: string;
  provider: string;
  /** 当前已加载的碎片，用于选择与「碎片 → 正文」对照。 */
  fragments: Fragment[];
  characterNames: string[];
  /** 未指定章号时的默认值（通常是下一章）。 */
  defaultChapter: number | null;
  onFragmentsChanged: () => void;
  onRefresh: () => Promise<void>;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

const KIND_LABELS: Record<string, string> = {
  WHIM: "奇思妙想",
  IMAGE: "画面",
  LINE: "想写的句子",
  SCENE: "场景",
  THEME: "主题看法",
  CHARACTER: "人物瞬间",
  MECHANIC: "桥段机制",
  OTHER: "其他",
};

const STATUS_LABELS: Record<string, string> = {
  INBOX: "刚记下",
  PLACED: "已安排",
  REALIZED: "已成文",
  ARCHIVED: "归档",
};

const TREATMENT_LABELS: Record<string, string> = {
  QUOTED: "原话保留",
  PARAPHRASED: "转述",
  EXPANDED: "展开",
  BACKGROUND: "背景",
};

const TREATMENT_CLASS: Record<string, string> = {
  QUOTED: "ok",
  PARAPHRASED: "",
  EXPANDED: "proposed",
  BACKGROUND: "rejected",
};

export function RealizePanel({
  novelId,
  provider,
  fragments,
  characterNames,
  defaultChapter,
  onFragmentsChanged,
  onRefresh,
  setStatus,
  setError,
}: Props) {
  const [mode, setMode] = useState<"picked" | "raw">("picked");
  const [selected, setSelected] = useState<string[]>([]);
  const [rawText, setRawText] = useState("");
  const [goals, setGoals] = useState("");
  const [tone, setTone] = useState("");
  const [mustKeep, setMustKeep] = useState<string[]>([]);
  const [forbidden, setForbidden] = useState("");
  const [characters, setCharacters] = useState<string[]>([]);
  const [chapter, setChapter] = useState(defaultChapter ? String(defaultChapter) : "");
  const [targetWords, setTargetWords] = useState(800);
  const [revise, setRevise] = useState(false);
  const [maxRounds, setMaxRounds] = useState(1);
  const [targetScore, setTargetScore] = useState(85);
  const [useModelCritic, setUseModelCritic] = useState(true);
  const [save, setSave] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<FragmentRealizeResponse | null>(null);
  const initialised = useRef(false);

  // 默认勾选还没成文的碎片（INBOX / PLACED）；作者此前的勾选不被覆盖
  useEffect(() => {
    const known = new Set(fragments.map((item) => item.id));
    setSelected((current) => {
      const kept = current.filter((id) => known.has(id));
      if (initialised.current || fragments.length === 0) return kept;
      initialised.current = true;
      const defaults = fragments
        .filter((item) => item.status === "INBOX" || item.status === "PLACED")
        .map((item) => item.id);
      return Array.from(new Set([...kept, ...defaults]));
    });
  }, [fragments]);

  const fragmentById = useMemo(
    () => new Map(fragments.map((item) => [item.id, item])),
    [fragments],
  );
  const pickedFragments = useMemo(
    () => fragments.filter((item) => selected.includes(item.id)),
    [fragments, selected],
  );
  const sentenceChips = useMemo(
    () =>
      pickedFragments
        .flatMap((item) => splitSentences(item.text).map((sentence) => ({ id: item.id, sentence })))
        .slice(0, 24),
    [pickedFragments],
  );

  function toggle(id: string) {
    setSelected((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    );
  }

  function toggleMustKeep(sentence: string) {
    setMustKeep((current) =>
      current.includes(sentence)
        ? current.filter((value) => value !== sentence)
        : [...current, sentence],
    );
  }

  async function realize() {
    const raw = rawText
      .split(/\n\s*\n/)
      .map((part) => part.trim())
      .filter(Boolean);
    const ids = mode === "picked" ? selected : [];
    if (ids.length === 0 && raw.length === 0) {
      setError(mode === "picked" ? "先勾选要成文的碎片" : "先按空行贴几条碎片进来");
      return;
    }
    const chapterNumber = Number(chapter);
    // 改稿闭环内部按「章节写作」的下限跑，字数太低会被后端直接拒掉
    if (revise && targetWords < 300) {
      setError("勾了「成文后自动修订」时目标字数至少要 300 字：改稿闭环按章节写作的下限来，调高或取消勾选");
      return;
    }
    // 存成章节会在该章号上新建章节，章号已存在时后端会直接报错
    if (
      save &&
      defaultChapter !== null &&
      Number.isFinite(chapterNumber) &&
      chapterNumber < defaultChapter
    ) {
      setError(
        `第${Math.floor(chapterNumber)}章已经存在了：「存为草稿章节」会新建章节，请把章号改到第${defaultChapter}章或更后，或取消勾选`,
      );
      return;
    }
    setBusy(true);
    setError("");
    setStatus("正在把你的想法写成正文（系统只做写作这件事，不替你编故事）…");
    try {
      const response = await api.realizeFragments(
        novelId,
        {
          fragment_ids: ids,
          raw_fragments: raw,
          goals: goals.trim(),
          tone: tone.trim(),
          must_keep: mustKeep,
          forbidden: splitList(forbidden),
          characters,
          ...(Number.isFinite(chapterNumber) && chapterNumber >= 1
            ? { chapter_number: Math.floor(chapterNumber) }
            : {}),
          target_words: targetWords,
          save,
          revise,
          max_rounds: maxRounds,
          target_score: targetScore,
          use_model_critic: useModelCritic,
        },
        provider,
      );
      setResult(response);
      setStatus(
        `成文完成：${response.realization.word_count} 字，用了 ${response.realization.coverage.fragments_used}/` +
          `${response.realization.coverage.fragments_total} 条碎片` +
          (response.realization.saved_chapter_id
            ? `，已在第${response.realization.chapter_number ?? "?"}章生成草稿`
            : ""),
      );
      onFragmentsChanged();
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  const voiceScore = result?.voice.score ?? null;
  const styleScore = result?.style.final_score ?? null;
  const coverage = result?.realization.coverage;

  return (
    <div className="issue" style={{ marginTop: 10 }}>
      <div className="issue-title">把碎片写成正文</div>
      <div className="hint">
        系统只做「把你的碎片写成有文学气息的文字」这件事：逐条给出「碎片 → 正文」的对应关系，
        写不到的地方如实报出来。原话想保留的，勾在下面的「必须原样保留」里。
      </div>

      <div className="field-row" style={{ marginTop: 6 }}>
        <button
          className={mode === "picked" ? "primary" : ""}
          onClick={() => setMode("picked")}
        >
          选已记下的碎片
        </button>
        <button className={mode === "raw" ? "primary" : ""} onClick={() => setMode("raw")}>
          直接贴碎片
        </button>
        {mode === "picked" && (
          <span className="hint" style={{ alignSelf: "center" }}>
            已勾选 {selected.length} 条（默认勾上还没成文的）
          </span>
        )}
      </div>

      {mode === "picked" ? (
        <div style={{ marginTop: 6 }}>
          {fragments.length === 0 && (
            <div className="hint">还没有碎片：先在上面随手记几条。</div>
          )}
          {fragments.map((fragment) => (
            <label key={fragment.id} className="fragment-pick">
              <input
                type="checkbox"
                checked={selected.includes(fragment.id)}
                onChange={() => toggle(fragment.id)}
                style={{ width: "auto", marginTop: 3 }}
              />
              <span>
                <span className="badge">{KIND_LABELS[fragment.kind] ?? fragment.kind}</span>{" "}
                <span className="badge">{STATUS_LABELS[fragment.status] ?? fragment.status}</span>{" "}
                <span className="badge">P{fragment.priority}</span>{" "}
                {fragment.title || fragment.text.slice(0, 20)}
                <span className="hint">　{fragment.text.replace(/\s+/g, " ").slice(0, 60)}</span>
              </span>
            </label>
          ))}
        </div>
      ) : (
        <textarea
          placeholder="把碎片直接贴在这里，多条之间空一行（这些碎片不会被存进碎片库）"
          value={rawText}
          onChange={(event) => setRawText(event.target.value)}
          rows={4}
          style={{ width: "100%", marginTop: 6 }}
        />
      )}

      <div className="field-row" style={{ marginTop: 8 }}>
        <label style={{ flex: "2 1 240px" }}>
          这次成文的目标（可留空）
          <input
            value={goals}
            placeholder="例如：把这三条写成开场，别解释，让读者自己看出来"
            onChange={(event) => setGoals(event.target.value)}
          />
        </label>
        <label>
          语气（可留空）
          <input
            value={tone}
            placeholder="例如：冷一点、口语"
            onChange={(event) => setTone(event.target.value)}
          />
        </label>
      </div>

      <div className="field-row" style={{ marginTop: 6 }}>
        <label>
          禁止出现（逗号分隔）
          <input value={forbidden} onChange={(event) => setForbidden(event.target.value)} />
        </label>
        <label style={{ flex: "0 0 auto", maxWidth: 220 }}>
          相关人物
          <select
            multiple
            value={characters}
            onChange={(event) =>
              setCharacters(Array.from(event.target.selectedOptions).map((option) => option.value))
            }
            style={{ minHeight: 78 }}
          >
            {characterNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label style={{ flex: "0 0 auto" }}>
          章号
          <input
            type="number"
            min={1}
            style={{ width: 90 }}
            value={chapter}
            placeholder="可留空"
            onChange={(event) => setChapter(event.target.value)}
          />
        </label>
        <label style={{ flex: "0 0 auto" }}>
          目标字数
          <input
            type="number"
            min={revise ? 300 : 100}
            max={8000}
            style={{ width: 100 }}
            value={targetWords}
            onChange={(event) => setTargetWords(Number(event.target.value) || 800)}
          />
        </label>
      </div>

      <div style={{ marginTop: 8 }}>
        <div className="hint">
          必须原样保留的原话（must_keep）：点下面碎片里的句子加入，写的时候会被要求一字不改。
        </div>
        <div className="field-row" style={{ marginTop: 4 }}>
          {sentenceChips.length === 0 && <span className="hint">勾选碎片后，这里会列出可以点选的句子。</span>}
          {sentenceChips.map((chip) => (
            <button
              key={`${chip.id}-${chip.sentence}`}
              className={mustKeep.includes(chip.sentence) ? "primary" : ""}
              onClick={() => toggleMustKeep(chip.sentence)}
            >
              {chip.sentence}
            </button>
          ))}
        </div>
        {mustKeep.length > 0 && (
          <div className="hint" style={{ marginTop: 4 }}>
            已选 {mustKeep.length} 句：{mustKeep.join(" / ")}　
            <button onClick={() => setMustKeep([])}>清空</button>
          </div>
        )}
      </div>

      <div className="field-row" style={{ marginTop: 8, alignItems: "center" }}>
        <label style={{ flexDirection: "row", alignItems: "center", flex: "0 0 auto" }}>
          <input
            type="checkbox"
            checked={revise}
            onChange={(event) => setRevise(event.target.checked)}
            style={{ width: "auto" }}
          />
          成文后自动修订
        </label>
        <label style={{ flexDirection: "row", alignItems: "center", flex: "0 0 auto" }}>
          <input
            type="checkbox"
            checked={save}
            onChange={(event) => setSave(event.target.checked)}
            style={{ width: "auto" }}
          />
          存为草稿章节
        </label>
      </div>
      {revise && (
        <div className="field-row" style={{ marginTop: 6, alignItems: "center" }}>
          <label style={{ flex: "0 0 auto" }}>
            最多改稿轮次（0-3）
            <input
              type="number"
              min={0}
              max={3}
              style={{ width: 70 }}
              value={maxRounds}
              onChange={(event) => setMaxRounds(Number(event.target.value) || 0)}
            />
          </label>
          <label style={{ flex: "0 0 auto" }}>
            目标分
            <input
              type="number"
              min={40}
              max={100}
              style={{ width: 80 }}
              value={targetScore}
              onChange={(event) => setTargetScore(Number(event.target.value) || 85)}
            />
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", flex: "0 0 auto" }}>
            <input
              type="checkbox"
              checked={useModelCritic}
              onChange={(event) => setUseModelCritic(event.target.checked)}
              style={{ width: "auto" }}
            />
            用模型读感当评审
          </label>
          <span className="hint">改稿会多跑几轮模型，费用与耗时都更高。</span>
        </div>
      )}

      <div className="field-row" style={{ marginTop: 8 }}>
        <button className="primary" onClick={() => void realize()} disabled={busy}>
          {busy ? "成文中…" : "把碎片写成正文"}
        </button>
        {result && <button onClick={() => setResult(null)}>清空结果</button>}
      </div>
      <div className="hint" style={{ marginTop: 4 }}>
        「存为草稿章节」会新建一章草稿（章号空着时后端自动取下一章），已有的章号会被后端拒绝；
        不勾选时碎片只会被标成 PLACED 而不是 REALIZED。勾了自动修订时目标字数不能低于 300。
      </div>

      {result && (
        <div style={{ marginTop: 10 }}>
          <div className="issue-title">
            {result.realization.title || "成文结果"}｜{result.realization.word_count} 字
            {result.provider ? `｜${result.provider}${result.model ? `/${result.model}` : ""}` : ""}
          </div>

          <div className="field-row" style={{ alignItems: "center", marginTop: 4 }}>
            <span className={`score-value ${voiceScore === null ? "" : scoreClass(voiceScore)}`}>
              {voiceScore === null ? "—" : formatScore(voiceScore)}
            </span>
            <span className="hint">
              <b>声音保留分：像不像你</b>
              （越高越好）。衡量成文里有多少你惯用的词、标点与断句；不是「文笔分」。
            </span>
          </div>
          <div className="field-row" style={{ alignItems: "center", marginTop: 2 }}>
            <span className={`score-value ${styleScore === null ? "" : scoreClass(styleScore)}`}>
              {styleScore === null ? "—" : formatScore(styleScore)}
            </span>
            <span className="hint">
              <b>文风得分：有没有 AI 味</b>
              （越高越好）。衡量套话、解释腔、句句工整这类「机器味」；与上面那项衡量的东西不同，
              两项都要看：声音不能丢，AI 味不能留。
            </span>
          </div>
          {!result.voice.available && (
            <div className="hint">
              还没有作者声音画像，所以算不出「像不像你」：可在「质量」标签里用你自己的文本建立画像。
            </div>
          )}
          {(result.voice.signature_hits?.length ?? 0) > 0 && (
            <div className="field-row" style={{ marginTop: 4 }}>
              <span className="hint" style={{ alignSelf: "center" }}>
                命中的作者特征词：
              </span>
              {result.voice.signature_hits?.map((term) => (
                <span key={term} className="badge ok">
                  {term}
                </span>
              ))}
            </div>
          )}
          {result.style.accepted === false && (
            <div className="hint">
              改稿没有达到目标分 {targetScore}：最终稿只是改稿里最好的一版，需要你再看一遍。
            </div>
          )}

          <div className="hint" style={{ marginTop: 8 }}>
            对应表（哪条碎片变成了哪段正文）：
          </div>
          <table className="grid">
            <thead>
              <tr>
                <th>碎片</th>
                <th>处理方式</th>
                <th>碎片原文</th>
                <th>生成的正文</th>
                <th>沿用的原话</th>
              </tr>
            </thead>
            <tbody>
              {result.realization.passages.map((passage, index) => {
                const source = fragmentById.get(passage.fragment_id) ?? null;
                const bridge = passage.fragment_id === "__bridge__";
                const inline = passage.fragment_id.startsWith("inline-");
                return (
                  <tr key={`${passage.fragment_id}-${index}`}>
                    <td>
                      {bridge ? (
                        <span className="badge">过渡</span>
                      ) : (
                        <>
                          <span className="badge">
                            {KIND_LABELS[source?.kind ?? ""] ?? source?.kind ?? "碎片"}
                          </span>
                          <div className="hint">
                            {source?.title ??
                              (inline ? "直接贴入的碎片（未入库）" : passage.fragment_id)}
                          </div>
                        </>
                      )}
                    </td>
                    <td>
                      <span
                        className={`badge ${
                          TREATMENT_CLASS[passage.treatment] ?? ""
                        }`}
                      >
                        {passage.treatment === "QUOTED"
                          ? "原话保留（QUOTED）"
                          : TREATMENT_LABELS[passage.treatment] ?? passage.treatment}
                      </span>
                    </td>
                    <td className="hint">
                      {bridge
                        ? "（过渡段落，不属于任何碎片）"
                        : source?.text ??
                          (inline ? "（直接贴入的碎片，没有存进碎片库）" : "（未加载到这条碎片）")}
                    </td>
                    <td>{passage.prose}</td>
                    <td className="hint">{passage.uses_quote || "—"}</td>
                  </tr>
                );
              })}
              {result.realization.passages.length === 0 && (
                <tr>
                  <td colSpan={5}>这次没有产出可核对的对应关系。</td>
                </tr>
              )}
            </tbody>
          </table>

          <div className="hint" style={{ marginTop: 8 }}>
            覆盖率：用了 <b>{coverage?.fragments_used ?? 0}</b> / 共{" "}
            <b>{coverage?.fragments_total ?? 0}</b> 条（
            {coverage ? `${(coverage.ratio * 100).toFixed(0)}%` : "—"}）
            {coverage && coverage.unused_ids.length > 0
              ? `｜没被写进去：${coverage.unused_ids
                  .map((id) => fragmentById.get(id)?.title ?? id)
                  .join("、")}`
              : ""}
          </div>

          {result.realization.undeveloped.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <div className="hint">系统自己说明「没写成」的碎片：</div>
              <ul>
                {result.realization.undeveloped.map((item, index) => (
                  <li key={`${item.fragment_id}-${index}`}>
                    {fragmentById.get(item.fragment_id)?.title ?? item.fragment_id}：
                    {item.reason || "未说明原因"}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.realization.invented_claims.length > 0 && (
            <div className="issue warning" style={{ marginTop: 8 }}>
              <div className="issue-title">成文里出现了 Canon 未记录的设定性陈述</div>
              <div>
                下面这些是成文里写出来、但设定库（Canon）里没有记录的：
                <ul>
                  {result.realization.invented_claims.map((claim, index) => (
                    <li key={index}>
                      {claim.subject}的{claim.predicate}是{claim.object}
                    </li>
                  ))}
                </ul>
                需要你确认：接受为新设定（补进 Canon），还是改掉这段正文。
              </div>
            </div>
          )}

          {result.warnings.length > 0 && (
            <div className="issue warning" style={{ marginTop: 8 }}>
              <div className="issue-title">
                系统如实报告（{result.warnings.length} 条）
              </div>
              <ul>
                {result.warnings.map((warning, index) => (
                  <li key={index}>{warning}</li>
                ))}
              </ul>
            </div>
          )}

          {result.realization.notes && (
            <div className="hint" style={{ marginTop: 6 }}>
              系统给作者的一句话：{result.realization.notes}
            </div>
          )}

          {result.style.rounds && result.style.rounds.length > 0 && (
            <>
              <div className="hint" style={{ marginTop: 8 }}>
                改稿轨迹（目标分 {targetScore}）：
              </div>
              <table className="grid metric-table">
                <thead>
                  <tr>
                    <th>轮次</th>
                    <th>阶段</th>
                    <th>文风得分</th>
                    <th>声音保留分</th>
                    <th>字数</th>
                    <th>文风 codes</th>
                    <th>一致性 codes</th>
                  </tr>
                </thead>
                <tbody>
                  {result.style.rounds.map((round, index) => (
                    <tr key={`${round.round}-${index}`}>
                      <td>第{round.round}轮</td>
                      <td>
                        <span className="badge">{round.stage}</span>
                      </td>
                      <td>{formatScore(round.score)}</td>
                      <td>{round.voice_score === null || round.voice_score === undefined ? "—" : formatScore(round.voice_score)}</td>
                      <td>{round.word_count}</td>
                      <td>{round.style_codes.join("、") || "—"}</td>
                      <td>{round.continuity_codes.join("、") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          <div className="hint" style={{ marginTop: 6 }}>
            这次成文更新了 {result.fragments_updated} 条碎片的状态与摘要
            {result.realization.saved_chapter_id
              ? `｜已在章节列表生成草稿：第${result.realization.chapter_number ?? "?"}章`
              : "｜没有落库，只给你看这段文字"}
            。
          </div>
          <pre className="draft">{result.realization.content}</pre>
        </div>
      )}
    </div>
  );
}

function splitList(text: string): string[] {
  return text
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** 供「必须原样保留」快速点选：按句末标点与换行切句，太短的片段不作为候选。 */
function splitSentences(text: string): string[] {
  return text
    .split(/[。！？!?；;\n]+/)
    .map((item) => item.trim())
    .filter((item) => item.length >= 4);
}
