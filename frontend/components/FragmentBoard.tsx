"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { RealizePanel } from "@/components/RealizePanel";
import type {
  ChapterIntent,
  Fragment,
  FragmentKind,
  FragmentStats,
  FragmentStatus,
} from "@/lib/types";

interface Props {
  novelId: string;
  provider: string;
  characterNames: string[];
  /** 下一章章号，仅用于成文表单的默认值。 */
  nextChapterNumber: number;
  /** 碎片发生变化时通知外层（用于刷新总览与其它面板）。 */
  onFragmentsChanged: () => void;
  /** 外层计数器变化时重新拉取（别的面板也可能改了碎片）。 */
  refreshToken: number;
  onRefresh: () => Promise<void>;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

type Filter = "ALL" | "UNPLACED" | FragmentStatus;

const KIND_LABELS: Record<FragmentKind, string> = {
  WHIM: "奇思妙想",
  IMAGE: "画面",
  LINE: "想写的句子",
  SCENE: "场景",
  THEME: "主题看法",
  CHARACTER: "人物瞬间",
  MECHANIC: "桥段机制",
  OTHER: "其他",
};

const KIND_OPTIONS: FragmentKind[] = [
  "WHIM",
  "IMAGE",
  "LINE",
  "SCENE",
  "THEME",
  "CHARACTER",
  "MECHANIC",
  "OTHER",
];

const STATUS_LABELS: Record<FragmentStatus, string> = {
  INBOX: "刚记下",
  PLACED: "已安排",
  REALIZED: "已成文",
  ARCHIVED: "归档",
};

const STATUS_CLASS: Record<FragmentStatus, string> = {
  INBOX: "",
  PLACED: "info",
  REALIZED: "ok",
  ARCHIVED: "rejected",
};

const FILTERS: { key: Filter; label: string }[] = [
  { key: "ALL", label: "全部" },
  { key: "UNPLACED", label: "未安排" },
  { key: "PLACED", label: "已安排" },
  { key: "REALIZED", label: "已成文" },
  { key: "ARCHIVED", label: "已归档" },
];

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

export function FragmentBoard({
  novelId,
  provider,
  characterNames,
  nextChapterNumber,
  onFragmentsChanged,
  refreshToken,
  onRefresh,
  setStatus,
  setError,
}: Props) {
  const [fragments, setFragments] = useState<Fragment[]>([]);
  const [stats, setStats] = useState<FragmentStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [filter, setFilter] = useState<Filter>("ALL");
  const [busy, setBusy] = useState(false);
  const [actingOn, setActingOn] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  // 随手记
  const [text, setText] = useState("");
  const [kind, setKind] = useState<FragmentKind>("WHIM");
  const [intent, setIntent] = useState("");
  const [tags, setTags] = useState("");
  const [characters, setCharacters] = useState("");
  const [priority, setPriority] = useState(3);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkText, setBulkText] = useState("");

  // 情感引导
  const [promptGoals, setPromptGoals] = useState("");
  const [prompting, setPrompting] = useState(false);
  const [questions, setQuestions] = useState<string[]>([]);
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const [promptWarnings, setPromptWarnings] = useState<string[]>([]);
  const [savingAnswer, setSavingAnswer] = useState<number | null>(null);

  // 就地编辑
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editIntent, setEditIntent] = useState("");
  const [editTags, setEditTags] = useState("");
  const [editPriority, setEditPriority] = useState(3);

  // 安排到第 N 章
  const [placeInputs, setPlaceInputs] = useState<Record<string, string>>({});

  // 章节意图
  const [intentChapter, setIntentChapter] = useState("");
  const [intentView, setIntentView] = useState<ChapterIntent | null>(null);
  const [intentLoading, setIntentLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextStats, nextFragments] = await Promise.all([
        api.fragmentStats(novelId),
        api.fragments(novelId, { limit: 300 }),
      ]);
      setStats(nextStats);
      setFragments(nextFragments);
      setLoadError("");
    } catch (error) {
      // 读不到就如实说读不到，不能让界面看起来像「一条碎片都没有」
      setLoadError(error instanceof Error ? error.message : String(error));
      setStats(null);
      setFragments([]);
    } finally {
      setLoading(false);
    }
  }, [novelId]);

  useEffect(() => {
    void load();
    // refreshToken 变化说明碎片在别处被改过（例如章节写作里「安排到本章」）
  }, [load, refreshToken]);

  const visible = useMemo(() => {
    if (filter === "ALL") return fragments;
    if (filter === "UNPLACED") {
      return fragments.filter((item) => item.target_chapter === null && item.status === "INBOX");
    }
    return fragments.filter((item) => item.status === filter);
  }, [fragments, filter]);

  const kindCounts = useMemo(
    () =>
      Object.entries(stats?.by_kind ?? {}).sort((a, b) => b[1] - a[1]) as [FragmentKind, number][],
    [stats],
  );

  /** 记下之后统一走这条路径：通知外层重新计数 + 刷新总览。 */
  async function afterChange() {
    onFragmentsChanged();
    await onRefresh();
  }

  async function record() {
    const body = text.trim();
    if (!body) {
      setError("先写点什么，一句不完整的话也可以");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await api.createFragment(novelId, {
        text: body,
        kind,
        intent: intent.trim(),
        tags: splitList(tags),
        related_characters: splitList(characters),
        priority,
      });
      setText("");
      setIntent("");
      setTags("");
      setCharacters("");
      setStatus(`已记下：${created.title || body.slice(0, 20)}`);
      await afterChange();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function recordBulk() {
    const items = bulkText
      .split(/\n\s*\n/)
      .map((part) => part.trim())
      .filter(Boolean);
    if (items.length === 0) {
      setError("按空行分隔多条碎片，现在没有可提交的内容");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await api.createFragmentsBulk(
        novelId,
        items.map((item) => ({ text: item, kind, priority })),
      );
      setBulkText("");
      setBulkOpen(false);
      setStatus(`已一次记下 ${created.length} 条碎片`);
      await afterChange();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function askPrompts() {
    setPrompting(true);
    setError("");
    setStatus("正在按你的目标提问（只提问，不代写）…");
    try {
      const chapterNumber = Number(intentChapter);
      const response = await api.emotionPrompts(
        novelId,
        {
          goals: promptGoals.trim(),
          characters: splitList(characters),
          ...(Number.isFinite(chapterNumber) && chapterNumber >= 1
            ? { chapter_number: Math.floor(chapterNumber) }
            : {}),
        },
        provider,
      );
      setQuestions(response.questions);
      setAnswers({});
      setPromptWarnings(response.warnings);
      setStatus(
        response.questions.length > 0
          ? `系统提了 ${response.questions.length} 个问题（provider=${response.provider}）`
          : "这次没有拿到引导问题，可以自己写下当时的细节",
      );
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setPrompting(false);
    }
  }

  async function saveAnswer(index: number, question: string) {
    const answer = (answers[index] ?? "").trim();
    if (!answer) {
      setError("先写下你的回答，再存成碎片");
      return;
    }
    setSavingAnswer(index);
    setError("");
    try {
      await api.createFragment(novelId, {
        text: answer,
        kind,
        origin: "PROMPT",
        prompted_by: question,
        intent: promptGoals.trim(),
        priority,
      });
      setAnswers((current) => ({ ...current, [index]: "" }));
      setStatus("回答已存成碎片");
      await afterChange();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setSavingAnswer(null);
    }
  }

  function startEdit(fragment: Fragment) {
    setEditingId(fragment.id);
    setEditText(fragment.text);
    setEditIntent(fragment.intent);
    setEditTags(fragment.tags.join("、"));
    setEditPriority(fragment.priority);
  }

  async function saveEdit(fragment: Fragment) {
    const body = editText.trim();
    if (!body) {
      setError("碎片正文不能为空");
      return;
    }
    setActingOn(fragment.id);
    setError("");
    try {
      await api.updateFragment(fragment.id, {
        text: body,
        intent: editIntent.trim(),
        tags: splitList(editTags),
        priority: editPriority,
      });
      setEditingId(null);
      setStatus("碎片已更新");
      await afterChange();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setActingOn(null);
    }
  }

  async function removeFragment(fragment: Fragment) {
    if (!window.confirm(`删除这条碎片？原文会一并删掉：\n${fragment.text.slice(0, 60)}`)) return;
    setActingOn(fragment.id);
    setError("");
    try {
      await api.deleteFragment(fragment.id);
      setStatus("碎片已删除");
      await afterChange();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setActingOn(null);
    }
  }

  async function placeFragment(fragment: Fragment, raw: string) {
    const chapterNumber = Math.floor(Number(raw));
    if (!Number.isFinite(chapterNumber) || chapterNumber < 1) {
      setError("请输入要安排到的章号");
      return;
    }
    setActingOn(fragment.id);
    setError("");
    try {
      await api.placeFragment(fragment.id, chapterNumber);
      setStatus(`已安排到第${chapterNumber}章：${fragment.title || fragment.text.slice(0, 20)}`);
      await afterChange();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setActingOn(null);
    }
  }

  async function loadIntent() {
    const chapterNumber = Math.floor(Number(intentChapter));
    if (!Number.isFinite(chapterNumber) || chapterNumber < 1) {
      setError("请输入要查看的章号");
      return;
    }
    setIntentLoading(true);
    setError("");
    try {
      const intent = await api.chapterIntent(novelId, chapterNumber);
      setIntentView(intent);
      setStatus(`第${intent.chapter_number}章安排了 ${intent.count} 条碎片`);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setIntentLoading(false);
    }
  }

  return (
    <div>
      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          想法碎片
          <span className="spacer" />
          <button onClick={() => void load()} disabled={loading}>
            {loading ? "读取中…" : "刷新"}
          </button>
        </div>
        <div className="field-row" style={{ alignItems: "center", marginTop: 4 }}>
          <span>
            总数 <b>{stats?.total ?? 0}</b>
          </span>
          <span>
            未安排 <b>{stats?.unplaced ?? 0}</b>
          </span>
          <span>
            已成文 <b>{stats?.realized ?? 0}</b>
          </span>
          <span>
            成文率 <b>{((stats?.realization_rate ?? 0) * 100).toFixed(0)}%</b>
          </span>
        </div>
        <div className="field-row" style={{ marginTop: 6 }}>
          {kindCounts.length === 0 && !loadError && <span className="hint">还没有碎片。</span>}
          {kindCounts.map(([value, count]) => (
            <span key={value} className="badge">
              {KIND_LABELS[value] ?? value} ×{count}
            </span>
          ))}
        </div>
        {loadError && (
          <div className="hint">碎片读取失败，下面的列表可能不是最新的：{loadError}</div>
        )}
        <div className="hint" style={{ marginTop: 4 }}>
          这里放的是你自己的素材：写得碎、写一半、只有一句话都没关系。系统不改你的说法，
          只负责存好、能被检索到，以及在写成正文后如实回填「哪句话变成了哪段正文」。
        </div>
      </div>

      <div className="split">
        <div className="issue">
          <div className="issue-title">情感引导：让系统问你</div>
          <div className="hint">
            系统只提问，答案是你的。答不上来可以空着；想写的细节往往在这种追问里才会浮出来。
          </div>
          <div className="field-row" style={{ marginTop: 6 }}>
            <label style={{ flex: "2 1 200px" }}>
              这段想写什么（可留空）
              <input
                value={promptGoals}
                placeholder="例如：师父死那天，我没哭"
                onChange={(event) => setPromptGoals(event.target.value)}
              />
            </label>
            <button
              className="primary"
              style={{ flex: "0 0 auto", alignSelf: "flex-end" }}
              onClick={() => void askPrompts()}
              disabled={prompting}
            >
              {prompting ? "提问中…" : "让系统提问"}
            </button>
          </div>
          {promptWarnings.length > 0 && (
            <ul>
              {promptWarnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          )}
          {questions.length === 0 && (
            <div className="hint" style={{ marginTop: 6 }}>
              还没有问题。
            </div>
          )}
          {questions.map((question, index) => (
            <div key={index} className="item-row">
              <div className="row-head">
                <b>{question}</b>
              </div>
              <div className="field-row" style={{ marginTop: 4 }}>
                <input
                  style={{ flex: "1 1 200px" }}
                  placeholder="你的回答（原话就行）"
                  value={answers[index] ?? ""}
                  onChange={(event) =>
                    setAnswers((current) => ({ ...current, [index]: event.target.value }))
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                      void saveAnswer(index, question);
                    }
                  }}
                />
                <button
                  style={{ flex: "0 0 auto" }}
                  onClick={() => void saveAnswer(index, question)}
                  disabled={savingAnswer !== null}
                >
                  {savingAnswer === index ? "存入中…" : "存成碎片"}
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="issue">
          <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            随手记
            <span className="spacer" />
            <button onClick={() => setBulkOpen((current) => !current)}>
              {bulkOpen ? "回到单条" : "批量粘贴"}
            </button>
          </div>
          {bulkOpen ? (
            <>
              <textarea
                placeholder="一次贴一堆碎片，每条之间空一行（攒着记的那些）"
                value={bulkText}
                onChange={(event) => setBulkText(event.target.value)}
                rows={6}
                style={{ width: "100%", marginTop: 6 }}
              />
              <div className="field-row" style={{ marginTop: 6, alignItems: "center" }}>
                <span className="hint">
                  当前 {bulkText.split(/\n\s*\n/).filter((part) => part.trim()).length} 条
                </span>
                <span className="spacer" />
                <button className="primary" onClick={() => void recordBulk()} disabled={busy}>
                  {busy ? "写入中…" : "全部记下"}
                </button>
              </div>
            </>
          ) : (
            <>
              <textarea
                placeholder="想到什么就写什么，不用完整"
                value={text}
                onChange={(event) => setText(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                    event.preventDefault();
                    void record();
                  }
                }}
                rows={4}
                style={{ width: "100%", marginTop: 6 }}
              />
              <div className="field-row" style={{ marginTop: 6 }}>
                <label style={{ flex: "0 0 auto" }}>
                  类型
                  <select
                    value={kind}
                    onChange={(event) => setKind(event.target.value as FragmentKind)}
                  >
                    {KIND_OPTIONS.map((value) => (
                      <option key={value} value={value}>
                        {KIND_LABELS[value]}
                      </option>
                    ))}
                  </select>
                </label>
                <label style={{ flex: "2 1 160px" }}>
                  意图（这条想表达什么）
                  <input value={intent} onChange={(event) => setIntent(event.target.value)} />
                </label>
                <label style={{ flex: "0 0 auto" }}>
                  优先级
                  <select
                    value={priority}
                    onChange={(event) => setPriority(Number(event.target.value))}
                  >
                    {[1, 2, 3, 4, 5].map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="field-row" style={{ marginTop: 6 }}>
                <label>
                  标签（逗号分隔）
                  <input
                    value={tags}
                    placeholder="例如：雨、葬礼"
                    onChange={(event) => setTags(event.target.value)}
                  />
                </label>
                <label>
                  相关人物（逗号分隔）
                  <input
                    value={characters}
                    placeholder="例如：林默"
                    onChange={(event) => setCharacters(event.target.value)}
                  />
                </label>
              </div>
              <div className="field-row" style={{ marginTop: 6, alignItems: "center" }}>
                <button className="primary" onClick={() => void record()} disabled={busy}>
                  {busy ? "记录中…" : "记下"}
                </button>
                <span className="hint">Ctrl+Enter 也能记下</span>
              </div>
            </>
          )}
        </div>
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          碎片列表
          <span className="spacer" />
          <span className="hint">当前列出 {visible.length} 条</span>
        </div>
        <div className="field-row" style={{ alignItems: "center" }}>
          {FILTERS.map((item) => (
            <button
              key={item.key}
              className={filter === item.key ? "primary" : ""}
              onClick={() => setFilter(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="field-row" style={{ marginTop: 6, alignItems: "center" }}>
          <label style={{ flex: "0 0 auto", flexDirection: "row", alignItems: "center" }}>
            看这一章的意图：第
            <input
              type="number"
              min={1}
              style={{ width: 70 }}
              value={intentChapter}
              placeholder="章号"
              onChange={(event) => setIntentChapter(event.target.value)}
            />
            章
          </label>
          <button onClick={() => void loadIntent()} disabled={intentLoading}>
            {intentLoading ? "读取中…" : "查看"}
          </button>
          {intentView && <button onClick={() => setIntentView(null)}>收起</button>}
        </div>
        {intentView && (
          <div className="issue info" style={{ marginTop: 8 }}>
            <div className="issue-title">
              第{intentView.chapter_number}章：安排给它的碎片 {intentView.count} 条
            </div>
            {intentView.fragments.length === 0 && (
              <div className="hint">这一章还没有安排任何碎片。</div>
            )}
            {intentView.fragments.map((item) => (
              <div key={item.id} className="item-row">
                <div className="row-head">
                  <span className="badge">{KIND_LABELS[item.kind] ?? item.kind}</span>
                  <span className={`badge ${STATUS_CLASS[item.status] ?? ""}`}>
                    {STATUS_LABELS[item.status] ?? item.status}
                  </span>
                  <b>{item.title || "（无标题）"}</b>
                </div>
                <div className="payload fragment-text">{item.text}</div>
                {item.intent && <div className="payload">意图：{item.intent}</div>}
              </div>
            ))}
            {intentView.intents.length > 0 && (
              <div className="hint">作者写的意图：{intentView.intents.join("；")}</div>
            )}
          </div>
        )}
      </div>

      {visible.map((fragment) => {
        const isEditing = editingId === fragment.id;
        const long = fragment.text.length > 120 || fragment.text.split("\n").length > 4;
        const open = expanded[fragment.id] ?? false;
        return (
          <div key={fragment.id} className="item-row">
            <div className="row-head">
              <span className="badge">{KIND_LABELS[fragment.kind] ?? fragment.kind}</span>
              <span className={`badge ${STATUS_CLASS[fragment.status] ?? ""}`}>
                {STATUS_LABELS[fragment.status] ?? fragment.status}
              </span>
              <span className="badge">P{fragment.priority}</span>
              {fragment.origin === "PROMPT" && <span className="badge info">引导</span>}
              <b>{fragment.title || "（无标题）"}</b>
              <span className="spacer" />
              <span className="hint">{formatTime(fragment.created_at)}</span>
            </div>

            {isEditing ? (
              <div style={{ marginTop: 4 }}>
                <textarea
                  value={editText}
                  onChange={(event) => setEditText(event.target.value)}
                  rows={3}
                  style={{ width: "100%" }}
                />
                <div className="field-row" style={{ marginTop: 4 }}>
                  <label style={{ flex: "2 1 160px" }}>
                    意图
                    <input
                      value={editIntent}
                      onChange={(event) => setEditIntent(event.target.value)}
                    />
                  </label>
                  <label>
                    标签（逗号分隔）
                    <input value={editTags} onChange={(event) => setEditTags(event.target.value)} />
                  </label>
                  <label style={{ flex: "0 0 auto" }}>
                    优先级
                    <select
                      value={editPriority}
                      onChange={(event) => setEditPriority(Number(event.target.value))}
                    >
                      {[1, 2, 3, 4, 5].map((value) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <div className="field-row" style={{ marginTop: 6 }}>
                  <button
                    className="primary"
                    onClick={() => void saveEdit(fragment)}
                    disabled={actingOn !== null}
                  >
                    保存
                  </button>
                  <button onClick={() => setEditingId(null)}>取消</button>
                </div>
              </div>
            ) : (
              <>
                <div
                  className={open ? "fragment-text" : "fragment-text clamped"}
                  onClick={() => long && setExpanded((current) => ({ ...current, [fragment.id]: !open }))}
                  style={long ? { cursor: "pointer" } : undefined}
                >
                  {fragment.text}
                </div>
                {long && (
                  <div className="field-row">
                    <button
                      className="fragment-toggle"
                      onClick={() =>
                        setExpanded((current) => ({ ...current, [fragment.id]: !open }))
                      }
                    >
                      {open ? "收起" : "展开全文"}
                    </button>
                  </div>
                )}
              </>
            )}

            <div className="hint" style={{ marginTop: 3 }}>
              意图：{fragment.intent || "—"}｜标签：{fragment.tags.join("、") || "—"}｜相关人物：
              {fragment.related_characters.join("、") || "—"}｜目标章：
              {fragment.target_chapter ? `第${fragment.target_chapter}章` : "未安排"}
            </div>
            {fragment.prompted_by && (
              <div className="hint">来自引导问题：{fragment.prompted_by}</div>
            )}
            {fragment.realized_excerpt && (
              <div className="realized-row">
                <span className="badge">已成文 →</span>{" "}
                <span className="hint">{fragment.realized_excerpt}</span>{" "}
                <span
                  className={`badge ${
                    fragment.realized_treatment === "QUOTED"
                      ? "ok"
                      : TREATMENT_CLASS[fragment.realized_treatment] ?? ""
                  }`}
                >
                  {fragment.realized_treatment === "QUOTED"
                    ? "原话保留"
                    : TREATMENT_LABELS[fragment.realized_treatment] ?? "已写进正文"}
                </span>
                {fragment.realized_chapter_id === null && (
                  <span className="hint">（还没存成章节，只写了正文）</span>
                )}
              </div>
            )}

            <div className="field-row" style={{ marginTop: 6, alignItems: "center" }}>
              <label style={{ flex: "0 0 auto", flexDirection: "row", alignItems: "center" }}>
                安排到第
                <input
                  type="number"
                  min={1}
                  style={{ width: 70 }}
                  placeholder={fragment.target_chapter ? String(fragment.target_chapter) : "章号"}
                  value={placeInputs[fragment.id] ?? ""}
                  onChange={(event) =>
                    setPlaceInputs((current) => ({ ...current, [fragment.id]: event.target.value }))
                  }
                />
                章
              </label>
              <button
                onClick={() =>
                  void placeFragment(
                    fragment,
                    placeInputs[fragment.id] ?? (fragment.target_chapter ? String(fragment.target_chapter) : ""),
                  )
                }
                disabled={actingOn !== null}
              >
                安排
              </button>
              <button onClick={() => startEdit(fragment)} disabled={actingOn !== null}>
                编辑
              </button>
              <button
                className="danger"
                onClick={() => void removeFragment(fragment)}
                disabled={actingOn !== null}
              >
                删除
              </button>
            </div>
          </div>
        );
      })}
      {visible.length === 0 && (
        <div className="hint" style={{ marginBottom: 8 }}>
          {loading
            ? "正在读取碎片…"
            : loadError
              ? "碎片读取失败，列表是空的。"
              : filter === "ALL"
                ? "还没有碎片：在上面随手记一条，一句话就行。"
                : "这个筛选下没有碎片。"}
        </div>
      )}

      <RealizePanel
        novelId={novelId}
        provider={provider}
        fragments={fragments}
        characterNames={characterNames}
        defaultChapter={nextChapterNumber}
        onFragmentsChanged={onFragmentsChanged}
        onRefresh={onRefresh}
        setStatus={setStatus}
        setError={setError}
      />
    </div>
  );
}

function splitList(text: string): string[] {
  return text
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatTime(value: string): string {
  return value ? value.slice(0, 16).replace("T", " ") : "—";
}
