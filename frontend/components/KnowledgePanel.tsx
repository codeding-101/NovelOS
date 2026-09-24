"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { DashboardPanel } from "@/components/DashboardPanel";
import { FragmentBoard } from "@/components/FragmentBoard";
import { PlanPanel } from "@/components/PlanPanel";
import { QualityPanel } from "@/components/QualityPanel";
import type { AsOfState, AskResponse, Bible, CharacterState, Dashboard } from "@/lib/types";

type TabKey =
  | "characters"
  | "timeline"
  | "worldview"
  | "events"
  | "foreshadowing"
  | "canon"
  | "dashboard"
  | "plan"
  | "fragments"
  | "quality"
  | "qa";

const TABS: { key: TabKey; label: string }[] = [
  { key: "characters", label: "人物" },
  { key: "timeline", label: "时间线" },
  { key: "worldview", label: "世界观" },
  { key: "events", label: "事件" },
  { key: "foreshadowing", label: "伏笔" },
  { key: "canon", label: "Canon" },
  { key: "dashboard", label: "总览" },
  { key: "plan", label: "规划" },
  { key: "fragments", label: "碎片" },
  { key: "quality", label: "质量" },
  { key: "qa", label: "QA" },
];

interface Props {
  novelId: string;
  bible: Bible | null;
  dashboard: Dashboard | null;
  provider: string;
  chaptersVersion: number;
  /** 下一章章号：碎片成文表单的默认章号。 */
  nextChapterNumber: number;
  /** 碎片被改动过的次数，变化时碎片面板要重新拉取。 */
  fragmentsVersion: number;
  onFragmentsChanged: () => void;
  onRefresh: () => Promise<void>;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

export function KnowledgePanel({
  novelId,
  bible,
  dashboard,
  provider,
  chaptersVersion,
  nextChapterNumber,
  fragmentsVersion,
  onFragmentsChanged,
  onRefresh,
  setStatus,
  setError,
}: Props) {
  const [tab, setTab] = useState<TabKey>("characters");
  const [openCharacter, setOpenCharacter] = useState<string | null>(null);
  const [states, setStates] = useState<CharacterState[]>([]);
  const [canonFilter, setCanonFilter] = useState<string>("ALL");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AskResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [agentMode, setAgentMode] = useState(false);
  const [asOfInput, setAsOfInput] = useState("1");
  const [asOf, setAsOf] = useState<AsOfState | null>(null);
  const [asOfBusy, setAsOfBusy] = useState(false);

  async function toggleCharacter(characterId: string) {
    if (openCharacter === characterId) {
      setOpenCharacter(null);
      return;
    }
    setOpenCharacter(characterId);
    try {
      setStates(await api.characterStates(characterId));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setStates([]);
    }
  }

  async function actOnFact(factId: string, action: "confirm" | "reject") {
    setBusy(true);
    try {
      const result = action === "confirm" ? await api.confirmFact(factId) : await api.rejectFact(factId);
      setStatus(`${action === "confirm" ? "已确认" : "已驳回"}：${result.message ?? ""}`);
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function ask() {
    if (!question.trim()) return;
    setBusy(true);
    setError("");
    try {
      setStatus("正在检索章节、事件、Canon 与人物库…");
      setAnswer(await api.ask(novelId, question.trim(), provider, agentMode ? "agent" : "simple"));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function viewAsOf() {
    const chapter = Number(asOfInput);
    if (!Number.isFinite(chapter) || chapter < 1) {
      setError("请输入有效的章号");
      return;
    }
    setAsOfBusy(true);
    setError("");
    try {
      const state = await api.asOfState(novelId, Math.floor(chapter));
      setAsOf(state);
      setStatus(`已切换到第${state.chapter_number}章时点视图`);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setAsOfBusy(false);
    }
  }

  const facts = (bible?.canon_facts ?? []).filter(
    (fact) => canonFilter === "ALL" || fact.status === canonFilter,
  );
  const character = bible?.characters.find((item) => item.id === openCharacter) ?? null;
  const needsBible =
    tab !== "dashboard" && tab !== "plan" && tab !== "fragments" && tab !== "quality";

  return (
    <div className="bottom">
      <div className="tabs" style={{ padding: "6px 12px 0" }}>
        {TABS.map((item) => (
          <div
            key={item.key}
            className={`tab ${tab === item.key ? "active" : ""}`}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </div>
        ))}
      </div>
      <div className="bottom-body">
        {!bible && needsBible && <div className="hint">正在加载设定库…</div>}

        {tab === "dashboard" && (
          <DashboardPanel
            novelId={novelId}
            provider={provider}
            dashboard={dashboard}
            onRefresh={onRefresh}
            setStatus={setStatus}
            setError={setError}
          />
        )}

        {tab === "plan" && (
          <PlanPanel
            novelId={novelId}
            provider={provider}
            chaptersVersion={chaptersVersion}
            onRefresh={onRefresh}
            setStatus={setStatus}
            setError={setError}
          />
        )}

        {tab === "fragments" && (
          <FragmentBoard
            novelId={novelId}
            provider={provider}
            characterNames={(bible?.characters ?? []).map((item) => item.name)}
            nextChapterNumber={nextChapterNumber}
            onFragmentsChanged={onFragmentsChanged}
            refreshToken={fragmentsVersion}
            onRefresh={onRefresh}
            setStatus={setStatus}
            setError={setError}
          />
        )}

        {tab === "quality" && (
          <QualityPanel
            novelId={novelId}
            onRefresh={onRefresh}
            setStatus={setStatus}
            setError={setError}
          />
        )}

        {bible && tab === "characters" && (
          <>
            <table className="grid">
              <thead>
                <tr>
                  <th>姓名</th>
                  <th>当前状态</th>
                  <th>当前位置</th>
                  <th>首次出场</th>
                  <th>目标</th>
                </tr>
              </thead>
              <tbody>
                {bible.characters.map((item) => (
                  <tr
                    key={item.id}
                    onClick={() => void toggleCharacter(item.id)}
                    style={{ cursor: "pointer" }}
                  >
                    <td>{item.name}</td>
                    <td>
                      <span className="badge">{item.current_status}</span>
                    </td>
                    <td>{item.current_location}</td>
                    <td>{item.first_appearance ? `第${item.first_appearance}章` : "—"}</td>
                    <td>{item.goals.slice(0, 2).join("；")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {character && (
              <div className="issue" style={{ marginTop: 10 }}>
                <div className="issue-title">{character.name} 档案与状态历史</div>
                <div>简介：{character.description || "—"}</div>
                <div>性格：{character.personality || "—"}</div>
                <div>背景：{character.background || "—"}</div>
                <div>目标：{character.goals.join("；") || "—"}</div>
                <div>恐惧：{character.fears.join("；") || "—"}</div>
                <div>已知：{character.known_facts.join("；") || "—"}</div>
                <div>未知：{character.unknown_facts.join("；") || "—"}</div>
                <div style={{ marginTop: 6 }}>
                  状态历史：
                  {states.length === 0 && <span className="hint">暂无记录</span>}
                  <ul>
                    {states.map((state) => (
                      <li key={state.id}>
                        第{state.chapter_number ?? "?"}章：状态={state.status || "—"}；位置=
                        {state.location || "—"}；来源={state.source}
                        {state.note ? `；依据：${state.note}` : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
          </>
        )}

        {bible && tab === "timeline" && (
          <table className="grid">
            <thead>
              <tr>
                <th>故事时间</th>
                <th>章节</th>
                <th>事件</th>
                <th>地点</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {bible.timeline.map((entry) => (
                <tr key={entry.id}>
                  <td>{entry.story_time}</td>
                  <td>{entry.chapter_number ? `第${entry.chapter_number}章` : "—"}</td>
                  <td>{entry.event}</td>
                  <td>{entry.location || "—"}</td>
                  <td>
                    <span className={`badge ${entry.status === "CANON" ? "canon" : "proposed"}`}>
                      {entry.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {bible && tab === "worldview" && (
          <>
            <div className="issue">
              <div className="issue-title">世界观</div>
              <div className="hint" style={{ color: "inherit" }}>
                {bible.world_rules.length} 条规则
              </div>
            </div>
            <table className="grid">
              <thead>
                <tr>
                  <th>规则</th>
                  <th>类型</th>
                  <th>主语</th>
                  <th>参数</th>
                  <th>说明</th>
                  <th>来源</th>
                </tr>
              </thead>
              <tbody>
                {bible.world_rules.map((rule) => (
                  <tr key={rule.id}>
                    <td>{rule.name}</td>
                    <td>{rule.rule_type}</td>
                    <td>{rule.subject}</td>
                    <td>{JSON.stringify(rule.params)}</td>
                    <td>{rule.description}</td>
                    <td>{rule.source_chapter ? `第${rule.source_chapter}章` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {bible && tab === "events" && (
          <table className="grid">
            <thead>
              <tr>
                <th>章节</th>
                <th>故事时间</th>
                <th>地点</th>
                <th>人物</th>
                <th>事件</th>
                <th>后果</th>
              </tr>
            </thead>
            <tbody>
              {bible.events.map((event) => (
                <tr key={event.id}>
                  <td>{event.chapter_number ? `第${event.chapter_number}章` : "—"}</td>
                  <td>{event.time || "—"}</td>
                  <td>{event.location || "—"}</td>
                  <td>{event.characters.join("、")}</td>
                  <td>{event.description}</td>
                  <td>{event.consequences || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {bible && tab === "foreshadowing" && (
          <table className="grid">
            <thead>
              <tr>
                <th>伏笔</th>
                <th>状态</th>
                <th>首现</th>
                <th>最近强化</th>
                <th>相关人物</th>
                <th>期望回收</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {bible.foreshadowings.map((item) => (
                <tr key={item.id}>
                  <td>{item.name}</td>
                  <td>
                    <span className="badge">{item.status}</span>
                  </td>
                  <td>{item.first_chapter ? `第${item.first_chapter}章` : "—"}</td>
                  <td>
                    {item.last_reinforced_chapter ? `第${item.last_reinforced_chapter}章` : "—"}
                  </td>
                  <td>{item.related_characters.join("、")}</td>
                  <td>{item.expected_payoff || "—"}</td>
                  <td>{item.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {bible && tab === "canon" && (
          <>
            <div className="field-row" style={{ marginBottom: 8 }}>
              <label style={{ flex: "0 0 auto", flexDirection: "row", alignItems: "center" }}>
                时点视图：第
                <input
                  type="number"
                  min={1}
                  style={{ width: 70 }}
                  value={asOfInput}
                  onChange={(event) => setAsOfInput(event.target.value)}
                />
                章
              </label>
              <button onClick={() => void viewAsOf()} disabled={asOfBusy}>
                {asOfBusy ? "读取中…" : `查看第 ${asOfInput || "N"} 章时的设定`}
              </button>
              {asOf && <button onClick={() => setAsOf(null)}>回到当前</button>}
              {asOf && <span className="badge">第{asOf.chapter_number}章快照</span>}
            </div>

            {asOf ? (
              <>
                {asOf.notes.map((note, index) => (
                  <div key={index} className="hint">
                    · {note}
                  </div>
                ))}
                <div className="hint" style={{ marginBottom: 6 }}>
                  该时点：Canon 事实 {asOf.canon_facts.length} 条、人物 {asOf.characters.length} 位、
                  时间线 {asOf.timeline.length} 条、事件 {asOf.events.length} 条、伏笔{" "}
                  {asOf.foreshadowings.length} 条、世界观规则 {asOf.world_rules.length} 条
                </div>
                <table className="grid">
                  <thead>
                    <tr>
                      <th>状态</th>
                      <th>主语</th>
                      <th>谓语</th>
                      <th>宾语</th>
                      <th>来源章节</th>
                      <th>生效章</th>
                      <th>失效章</th>
                    </tr>
                  </thead>
                  <tbody>
                    {asOf.canon_facts.map((fact, index) => (
                      <tr key={fact.id ?? index}>
                        <td>
                          <span className="badge canon">{fact.status}</span>
                        </td>
                        <td>{fact.subject}</td>
                        <td>{fact.predicate}</td>
                        <td>{fact.object}</td>
                        <td>{fact.source_chapter ? `第${fact.source_chapter}章` : "—"}</td>
                        <td>{fact.valid_from_chapter ? `第${fact.valid_from_chapter}章` : "—"}</td>
                        <td>{fact.valid_until_chapter ? `第${fact.valid_until_chapter}章` : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <div className="issue" style={{ marginTop: 10 }}>
                  <div className="issue-title">人物状态（第{asOf.chapter_number}章时）</div>
                  <table className="grid">
                    <thead>
                      <tr>
                        <th>姓名</th>
                        <th>该章状态</th>
                        <th>该章位置</th>
                        <th>当前状态</th>
                        <th>首次出场</th>
                        <th>该章前有变更</th>
                      </tr>
                    </thead>
                    <tbody>
                      {asOf.characters.map((item) => (
                        <tr key={item.name}>
                          <td>{item.name}</td>
                          <td>
                            <span className="badge">{item.status_at_chapter}</span>
                          </td>
                          <td>{item.location_at_chapter || "—"}</td>
                          <td>{item.current_status}</td>
                          <td>
                            {item.first_appearance ? `第${item.first_appearance}章` : "—"}
                          </td>
                          <td>
                            {item.changed_since ? (
                              <span className="badge canon">是</span>
                            ) : (
                              <span className="badge warning">取当前值</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <>
                <div className="field-row" style={{ marginBottom: 8 }}>
                  {["ALL", "CANON", "PROPOSED", "REJECTED", "SUPERSEDED"].map((value) => (
                    <button
                      key={value}
                      className={canonFilter === value ? "primary" : ""}
                      onClick={() => setCanonFilter(value)}
                    >
                      {value === "ALL" ? "全部" : value}
                    </button>
                  ))}
                  <span className="hint">共 {facts.length} 条</span>
                </div>
                <table className="grid">
                  <thead>
                    <tr>
                      <th>状态</th>
                      <th>主语</th>
                      <th>谓语</th>
                      <th>宾语</th>
                      <th>来源章节</th>
                      <th>生效章</th>
                      <th>失效章</th>
                      <th>可见性</th>
                      <th>置信度</th>
                      <th>来源</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {facts.map((fact) => (
                      <tr key={fact.id}>
                        <td>
                          <span
                            className={`badge ${
                              fact.status === "CANON"
                                ? "canon"
                                : fact.status === "PROPOSED"
                                  ? "proposed"
                                  : "rejected"
                            }`}
                          >
                            {fact.status}
                          </span>
                        </td>
                        <td>{fact.subject}</td>
                        <td>{fact.predicate}</td>
                        <td>{fact.object}</td>
                        <td>{fact.source_chapter ? `第${fact.source_chapter}章` : "—"}</td>
                        <td>{fact.valid_from_chapter ? `第${fact.valid_from_chapter}章` : "—"}</td>
                        <td>
                          {fact.valid_until_chapter ? `第${fact.valid_until_chapter}章` : "—"}
                        </td>
                        <td>{fact.visibility}</td>
                        <td>{fact.confidence.toFixed(2)}</td>
                        <td>{fact.origin}</td>
                        <td>
                          {fact.status === "PROPOSED" && (
                            <>
                              <button
                                className="primary"
                                onClick={() => void actOnFact(fact.id, "confirm")}
                                disabled={busy}
                              >
                                确认
                              </button>{" "}
                              <button
                                className="danger"
                                onClick={() => void actOnFact(fact.id, "reject")}
                                disabled={busy}
                              >
                                驳回
                              </button>
                            </>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </>
        )}

        {bible && tab === "qa" && (
          <>
            <div className="field-row" style={{ marginBottom: 8 }}>
              <input
                style={{ flex: 1, minWidth: 220 }}
                placeholder="用自然语言问前文，例如：林默什么时候第一次见到王烈？"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void ask();
                }}
              />
              <button className="primary" onClick={() => void ask()} disabled={busy}>
                检索前文
              </button>
              <label style={{ flex: "0 0 auto", flexDirection: "row", alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={agentMode}
                  onChange={(event) => setAgentMode(event.target.checked)}
                  style={{ width: "auto" }}
                />
                Agent 模式
              </label>
            </div>
            <div className="hint" style={{ marginBottom: 8 }}>
              简单模式：后台先检索再由模型组织答案；Agent 模式：模型自己调用工具取数（需要
              provider 支持工具调用）。
            </div>
            {answer && (
              <div className={`issue ${answer.confidence === "UNKNOWN" ? "warning" : ""}`}>
                <div className="issue-title">
                  <span className="badge">{answer.confidence}</span>
                  {answer.mode === "agent" && <span className="badge proposed">agent</span>} 回答
                </div>
                <div style={{ whiteSpace: "pre-wrap" }}>{answer.answer}</div>
                {answer.mode === "agent" && (
                  <div style={{ marginTop: 6 }}>
                    <div className="hint">
                      Agent 工具调用（{answer.tool_calls.length} 次，provider={answer.provider}/
                      {answer.model}）
                    </div>
                    {answer.tool_calls.length === 0 && <div className="hint">本次没有调用工具。</div>}
                    {answer.tool_calls.map((call, index) => (
                      <div key={index} className="item-row">
                        <div className="row-head">
                          <b>{call.name}</b>
                          <span
                            className={`badge ${
                              call.status === "ok" || call.status === "done" ? "ok" : "proposed"
                            }`}
                          >
                            {call.status}
                          </span>
                        </div>
                        <div className="payload">{JSON.stringify(call.arguments)}</div>
                      </div>
                    ))}
                  </div>
                )}
                {answer.warnings.length > 0 && (
                  <ul>
                    {answer.warnings.map((warning, index) => (
                      <li key={index}>{warning}</li>
                    ))}
                  </ul>
                )}
                <div className="hint" style={{ marginTop: 6 }}>
                  证据：
                  {answer.evidence.length === 0 && "无"}
                </div>
                <ul>
                  {answer.evidence.map((item, index) => (
                    <li key={index}>
                      [{item.ref_type}] {item.title}
                      {item.chapter_number ? `（第${item.chapter_number}章）` : ""}：{item.excerpt}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
