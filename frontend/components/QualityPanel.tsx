"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  BaselineMetricTable,
  StyleReviewView,
  formatMetric,
  formatScore,
  metricShortLabel,
} from "@/components/StyleReviewView";
import type {
  ClaimEvidence,
  ClaimReport,
  ClaimVerdict,
  Commitment,
  CommitmentKind,
  CommitmentStatus,
  CraftRule,
  InvariantIssue,
  InvariantReport,
  PublishCheck,
  PublishCheckItem,
  StyleDriftReport,
  StyleProfile,
  StyleReview,
  VoiceProfile,
} from "@/lib/types";

type CommitmentFilter = CommitmentStatus | "ALL";

interface Props {
  novelId: string;
  onRefresh: () => Promise<void>;
  setStatus: (message: string) => void;
  setError: (message: string) => void;
}

const KIND_LABELS: Record<CommitmentKind, string> = {
  APPOINTMENT: "约定",
  DEADLINE: "期限",
  THREAT: "威胁",
  PROMISE: "承诺",
  QUESTION: "悬问",
};

const KIND_OPTIONS: CommitmentKind[] = ["APPOINTMENT", "DEADLINE", "THREAT", "PROMISE", "QUESTION"];

const STATUS_LABELS: Record<CommitmentFilter, string> = {
  ALL: "全部",
  OPEN: "未到期",
  OVERDUE: "已逾期",
  FULFILLED: "已兑现",
  ABANDONED: "已放弃",
};

const STATUS_FILTERS: CommitmentFilter[] = ["ALL", "OPEN", "OVERDUE", "FULFILLED", "ABANDONED"];

const STATUS_CLASS: Record<CommitmentStatus, string> = {
  OPEN: "proposed",
  OVERDUE: "error",
  FULFILLED: "ok",
  ABANDONED: "rejected",
};

const LEVEL_CLASS: Record<InvariantIssue["level"], string> = {
  error: "error",
  warning: "warning",
  info: "info",
};

const LEVEL_LABELS: Record<InvariantIssue["level"], string> = {
  error: "错误",
  warning: "警告",
  info: "提示",
};

// --------------------------------------------------------------------------- V0.5 断言核对
const VERDICT_LABELS: Record<string, string> = {
  CONFLICT: "冲突",
  UNVERIFIED: "待确认",
  SUPPORTED: "一致",
};

const VERDICT_CLASS: Record<string, string> = {
  CONFLICT: "error",
  UNVERIFIED: "warning",
  SUPPORTED: "ok",
};

const DIRECTION_LABELS: Record<string, string> = {
  locked: "按锁定窗口（容差收紧一半）",
  baseline: "按基线窗口",
  none: "还没有基线",
};

const METRIC_DIRECTION_LABELS: Record<string, string> = {
  lower_better: "越低越好",
  higher_better: "越高越好",
  range: "落在区间内即可",
};

function claimLine(claim: ClaimVerdict["claim"]): string {
  const subject = claim?.subject || "—";
  const predicate = claim?.predicate || "—";
  const object = claim?.object || "—";
  return `${subject}·${predicate} = ${object}`;
}

/** 报告来源：CHAPTER 是某一章正文，DRAFT 是贴进去的草稿，其余原样显示。 */
function reportLabel(report: ClaimReport): string {
  if (report.label === "CHAPTER") return `第${report.chapter_number ?? "?"}章正文`;
  if (!report.label || report.label === "DRAFT") return "贴的草稿";
  return report.label;
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

/** 一条断言判定：断言本体 + 结论理由 + 原文片段 + 依据。 */
function ClaimVerdictRow({ item, muted = false }: { item: ClaimVerdict; muted?: boolean }) {
  const verdict = item.verdict || "UNVERIFIED";
  const claim = item.claim;
  const evidence = item.evidence || [];
  return (
    <div className="item-row">
      <div className="row-head">
        <span className={`badge ${muted ? "" : VERDICT_CLASS[verdict] || "info"}`}>
          {VERDICT_LABELS[verdict] || verdict}
        </span>
        {muted ? (
          <span className="hint">{claimLine(claim)}</span>
        ) : (
          <b>{claimLine(claim)}</b>
        )}
        {claim?.kind ? <span className="hint">{claim.kind}</span> : null}
      </div>
      {item.reason && <div className="payload">{item.reason}</div>}
      {claim?.quote ? <div className="payload">原文：{claim.quote}</div> : null}
      {evidence.map((entry, index) => (
        <div key={`evidence-${index}`} className="payload">
          依据：{evidenceLine(entry)}
        </div>
      ))}
    </div>
  );
}

/** 一份核对报告的三组判定：冲突（警示）／待确认（提示）／一致（弱化）。 */
function ClaimVerdictGroups({ report }: { report: ClaimReport }) {
  const conflicts = report.conflicts || [];
  const unverified = report.unverified || [];
  const supported = report.supported || [];
  if (report.claim_count === 0) {
    return <div className="hint">没有抽出可核对的设定断言。</div>;
  }
  return (
    <div>
      {conflicts.length > 0 && (
        <div className="issue error" style={{ marginTop: 6 }}>
          <div className="issue-title">与 Canon／世界观规则冲突（{conflicts.length}）</div>
          {conflicts.map((item, index) => (
            <ClaimVerdictRow key={`conflict-${index}`} item={item} />
          ))}
        </div>
      )}
      {unverified.length > 0 && (
        <div className="issue warning" style={{ marginTop: 6 }}>
          <div className="issue-title">待确认（{unverified.length}）</div>
          {unverified.map((item, index) => (
            <ClaimVerdictRow key={`unverified-${index}`} item={item} />
          ))}
        </div>
      )}
      {supported.length > 0 && (
        <div className="issue" style={{ marginTop: 6 }}>
          <div className="issue-title">与现有设定一致（{supported.length}）</div>
          {supported.map((item, index) => (
            <ClaimVerdictRow key={`supported-${index}`} item={item} muted />
          ))}
        </div>
      )}
      {conflicts.length === 0 && unverified.length === 0 && supported.length === 0 && (
        <div className="hint">报告里没有任何判定（后端可能只回了统计数）。</div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- V0.7 发布前检查
const PUBLISH_LEVEL_CLASS: Record<string, string> = {
  error: "error",
  warning: "warning",
  info: "info",
  ok: "ok",
};

const PUBLISH_LEVEL_LABELS: Record<string, string> = {
  error: "阻断",
  warning: "警告",
  info: "提示",
  ok: "正常",
};

const PUBLISH_LEVEL_ORDER = ["error", "warning", "info"];

/** 开篇与章末直接关系到完读率，从清单里挑出来单独排一份。 */
const OPENING_HOOK_CODES = ["OPENING_SLOW", "OPENING_INFO_DUMP", "ENDING_NO_HOOK"];

/** 检查项的出处标记：点开看这条依据哪条规则、原文怎么说的。规则库没读到就只显示编号。 */
function CraftRuleMarker({ code, rule }: { code: string; rule?: CraftRule }) {
  return (
    <details style={{ marginTop: 2 }}>
      <summary>
        <span className="badge info">出处</span> 依据：{rule?.summary || code}
      </summary>
      <div className="payload">
        {rule ? (
          <>
            <div>
              {rule.code}｜{rule.summary}
            </div>
            <div>出处：{rule.source}</div>
            {rule.detail ? <div>{rule.detail}</div> : null}
            {rule.advice ? <div>做法：{rule.advice}</div> : null}
          </>
        ) : (
          <div>
            规则编号 {code}（规则知识库还没读到，稍后可在本块底部「这些检查的依据」里核对原文）。
          </div>
        )}
      </div>
    </details>
  );
}

/** 一条发布前检查项：结论 + 依据出处 + 原文片段 + 修法。 */
function PublishCheckRow({ item, rule }: { item: PublishCheckItem; rule?: CraftRule }) {
  const level = item.level || "info";
  return (
    <div className="item-row">
      <div className="row-head">
        <span className={`badge ${PUBLISH_LEVEL_CLASS[level] ?? "info"}`}>
          {PUBLISH_LEVEL_LABELS[level] ?? level}
        </span>
        <span className="hint">{item.code}</span>
        <b>{item.message}</b>
      </div>
      {item.rule ? <CraftRuleMarker code={item.rule} rule={rule} /> : null}
      {item.excerpt ? <div className="payload">原文：{item.excerpt}</div> : null}
      {item.category ? (
        <div className="payload">
          分类：{item.category}
          {item.word ? `｜词：${item.word}` : ""}
        </div>
      ) : null}
      {item.value !== undefined ? <div className="payload">测量值：{item.value}</div> : null}
      {item.fix ? <div className="fix">修法：{item.fix}</div> : null}
    </div>
  );
}

export function QualityPanel({ novelId, onRefresh, setStatus, setError }: Props) {
  const [baseline, setBaseline] = useState<StyleProfile | null>(null);
  const [baselineLoading, setBaselineLoading] = useState(false);
  const [baselineBuilding, setBaselineBuilding] = useState(false);
  const [draftText, setDraftText] = useState("");
  const [draftReview, setDraftReview] = useState<StyleReview | null>(null);
  const [draftReviewing, setDraftReviewing] = useState(false);

  // 作者声音画像：成文时的「声音保留分」按它算
  const [voice, setVoice] = useState<VoiceProfile | null>(null);
  const [voiceLoading, setVoiceLoading] = useState(false);
  const [voiceBuilding, setVoiceBuilding] = useState(false);
  const [voiceText, setVoiceText] = useState("");

  const [invariants, setInvariants] = useState<InvariantReport | null>(null);
  const [invariantsLoading, setInvariantsLoading] = useState(false);
  const [invariantsRunning, setInvariantsRunning] = useState(false);

  const [commitments, setCommitments] = useState<Commitment[]>([]);
  const [commitmentsLoading, setCommitmentsLoading] = useState(false);
  const [commitmentFilter, setCommitmentFilter] = useState<CommitmentFilter>("ALL");
  const [fulfilChapter, setFulfilChapter] = useState("");
  const [actingOn, setActingOn] = useState<string | null>(null);
  const [newWhat, setNewWhat] = useState("");
  const [newKind, setNewKind] = useState<CommitmentKind>("APPOINTMENT");
  const [newWho, setNewWho] = useState("");
  const [newCounterpart, setNewCounterpart] = useState("");
  const [newDeadline, setNewDeadline] = useState("");
  const [newSourceChapter, setNewSourceChapter] = useState("");
  const [creating, setCreating] = useState(false);

  // V0.5：设定断言核对
  const [claimText, setClaimText] = useState("");
  const [claimChapter, setClaimChapter] = useState("");
  const [claimUseModel, setClaimUseModel] = useState(true);
  const [claimRunning, setClaimRunning] = useState(false);
  const [claimReport, setClaimReport] = useState<ClaimReport | null>(null);
  const [claimHistory, setClaimHistory] = useState<ClaimReport[]>([]);
  const [claimHistoryLoading, setClaimHistoryLoading] = useState(false);
  const [openClaimReport, setOpenClaimReport] = useState<string | null>(null);

  // V0.5：文风锁定与漂移
  const [drift, setDrift] = useState<StyleDriftReport | null>(null);
  const [driftLoading, setDriftLoading] = useState(false);
  const [locking, setLocking] = useState(false);

  // V0.7：发布前检查（番茄免费小说）
  const [publishText, setPublishText] = useState("");
  const [publishChapter, setPublishChapter] = useState("");
  const [publishChecking, setPublishChecking] = useState(false);
  const [publishReport, setPublishReport] = useState<PublishCheck | null>(null);
  // 写作规则知识库：面板首次打开时拉一次，用来给检查项标注出处
  const [craftRules, setCraftRules] = useState<CraftRule[] | null>(null);
  const [craftRulesLoading, setCraftRulesLoading] = useState(false);
  const craftRulesRequested = useRef(false);

  const loadBaseline = useCallback(async () => {
    setBaselineLoading(true);
    try {
      setBaseline(await api.styleBaseline(novelId));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setBaseline(null);
    } finally {
      setBaselineLoading(false);
    }
  }, [novelId, setError]);

  const loadVoice = useCallback(async () => {
    setVoiceLoading(true);
    try {
      setVoice(await api.voiceProfile(novelId));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setVoice(null);
    } finally {
      setVoiceLoading(false);
    }
  }, [novelId, setError]);

  const loadInvariants = useCallback(async () => {
    setInvariantsLoading(true);
    try {
      setInvariants(await api.invariants(novelId));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setInvariants(null);
    } finally {
      setInvariantsLoading(false);
    }
  }, [novelId, setError]);

  const loadClaimHistory = useCallback(async () => {
    setClaimHistoryLoading(true);
    try {
      setClaimHistory(await api.listClaimReports(novelId, { limit: 10 }));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setClaimHistory([]);
    } finally {
      setClaimHistoryLoading(false);
    }
  }, [novelId, setError]);

  const loadDrift = useCallback(async () => {
    setDriftLoading(true);
    try {
      setDrift(await api.styleDrift(novelId));
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
      setDrift(null);
    } finally {
      setDriftLoading(false);
    }
  }, [novelId, setError]);

  /**
   * 写作规则知识库：只在面板首次打开时拉一次并缓存。拉不到就退回只显示规则编号，
   * 不弹错误、也不影响发布检查本身。
   */
  const loadCraftRules = useCallback(async () => {
    if (craftRulesRequested.current) return;
    craftRulesRequested.current = true;
    setCraftRulesLoading(true);
    try {
      setCraftRules(await api.craftRules());
    } catch {
      setCraftRules(null);
    } finally {
      setCraftRulesLoading(false);
    }
  }, []);

  const loadCommitments = useCallback(
    async (filter: CommitmentFilter) => {
      setCommitmentsLoading(true);
      try {
        setCommitments(await api.commitments(novelId, filter === "ALL" ? undefined : filter));
      } catch (error) {
        setError(error instanceof Error ? error.message : String(error));
        setCommitments([]);
      } finally {
        setCommitmentsLoading(false);
      }
    },
    [novelId, setError],
  );

  useEffect(() => {
    void loadBaseline();
    void loadVoice();
    void loadInvariants();
    void loadClaimHistory();
    void loadDrift();
    void loadCraftRules();
  }, [loadBaseline, loadVoice, loadInvariants, loadClaimHistory, loadDrift, loadCraftRules]);

  useEffect(() => {
    void loadCommitments(commitmentFilter);
  }, [loadCommitments, commitmentFilter]);

  async function buildBaseline() {
    setBaselineBuilding(true);
    setError("");
    setStatus("正在用本书已完成章节统计文风基线…");
    try {
      const profile = await api.buildStyleBaseline(novelId, { make_default: true });
      setBaseline(profile);
      setStatus(
        `文风基线已建立：${profile.name}（${profile.sample_count} 个样本 / ${profile.total_chars} 字）`,
      );
      await loadDrift();
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBaselineBuilding(false);
    }
  }

  async function buildVoice(payload: { chapter_numbers?: number[]; texts?: string[] }) {
    setVoiceBuilding(true);
    setError("");
    setStatus("正在统计你的用词、标点与断句习惯…");
    try {
      const profile = await api.buildVoiceProfile(novelId, payload);
      setVoice(profile);
      setVoiceText("");
      setStatus(
        `作者声音画像已建立：${profile.name}（${profile.sample_count} 个样本 / ${profile.total_chars} 字）`,
      );
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setVoiceBuilding(false);
    }
  }

  async function reviewDraft() {
    if (!draftText.trim()) {
      setError("请先粘贴一段草稿");
      return;
    }
    setDraftReviewing(true);
    setError("");
    setStatus("正在评审这段草稿（规则指标 + 模型读感）…");
    try {
      const review = await api.reviewTextStyle(novelId, { text: draftText, use_model: true });
      setDraftReview(review);
      setStatus(`草稿评审完成：score=${formatScore(review.score)}，${review.issues.length} 条提示`);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setDraftReviewing(false);
    }
  }

  async function rerunInvariants() {
    setInvariantsRunning(true);
    setError("");
    setStatus("正在重跑全局不变量检查…");
    try {
      const report = await api.runInvariants(novelId);
      setInvariants(report);
      setStatus(`全局检查完成：${report.errors} 个错误、${report.warnings} 个警告`);
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setInvariantsRunning(false);
    }
  }

  /** 核对草稿或某一章的设定断言：只出报告，不写 Canon。 */
  async function verifyClaims() {
    const chapter = Number(claimChapter);
    const byChapter = Number.isFinite(chapter) && chapter >= 1;
    if (!byChapter && !claimText.trim()) {
      setError("请先粘贴一段草稿，或填写要核对的章节号");
      return;
    }
    setClaimRunning(true);
    setError("");
    setStatus(
      claimUseModel
        ? "正在核对设定断言（确定性规则 + 模型）…"
        : "正在核对设定断言（只用确定性规则）…",
    );
    try {
      const report = await api.verifyClaims(novelId, {
        ...(byChapter ? { chapter_number: Math.floor(chapter) } : { text: claimText }),
        use_model: claimUseModel,
        persist: true,
      });
      setClaimReport(report);
      setStatus(
        `核对完成：${report.claim_count} 条断言，${report.conflict_count} 条冲突、` +
          `${report.unverified_count} 条待确认、${report.supported_count} 条一致`,
      );
      await loadClaimHistory();
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setClaimRunning(false);
    }
  }

  /** 锁定／解除锁定文风基线：锁定后新章按收紧一半的窗口比对。 */
  async function toggleStyleLock(locked: boolean) {
    if (!baseline) return;
    setLocking(true);
    setError("");
    try {
      const profile = await api.lockStyleBaseline(novelId, baseline.id, locked);
      setBaseline(profile);
      setStatus(
        locked
          ? `已锁定文风基线：${profile.name}（新章按收紧一半的窗口比对）`
          : `已解除锁定：${profile.name}（回到基线窗口比对）`,
      );
      await loadDrift();
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setLocking(false);
    }
  }

  /** 发布前检查：贴一段草稿，或只填章节号让后端查已入库的那一章；只出清单，不改正文、不落库。 */
  async function runPublishCheck() {
    const chapter = Number(publishChapter);
    const byChapter = Number.isFinite(chapter) && chapter >= 1;
    if (!byChapter && !publishText.trim()) {
      setError("请先粘贴一段草稿，或填写要检查的章节号");
      return;
    }
    setPublishChecking(true);
    setError("");
    setStatus("正在按番茄的发布口径检查（字数、风险词、格式、开篇与章末）…");
    try {
      const report = await api.publishCheckText(novelId, {
        ...(byChapter ? { chapter_number: Math.floor(chapter) } : { text: publishText }),
      });
      setPublishReport(report);
      setStatus(
        `发布前检查完成：${report.word_count} 字，阻断 ${report.blocking} 项、警告 ${report.warnings} 项`,
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setError(byChapter ? `第${Math.floor(chapter)}章：${message}` : message);
    } finally {
      setPublishChecking(false);
    }
  }

  async function decideCommitment(commitment: Commitment, action: "fulfill" | "abandon") {
    setActingOn(commitment.id);
    setError("");
    try {
      const chapter = Number(fulfilChapter);
      const payload =
        action === "fulfill" && Number.isFinite(chapter) && chapter >= 1
          ? { chapter_number: Math.floor(chapter) }
          : {};
      const updated =
        action === "fulfill"
          ? await api.fulfillCommitment(commitment.id, payload)
          : await api.abandonCommitment(commitment.id, payload);
      setCommitments((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setStatus(
        `${action === "fulfill" ? "已标记兑现" : "已放弃"}：${updated.what}` +
          (updated.fulfilled_chapter ? `（第${updated.fulfilled_chapter}章）` : ""),
      );
      await loadCommitments(commitmentFilter);
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setActingOn(null);
    }
  }

  async function addCommitment() {
    if (!newWhat.trim()) {
      setError("承诺内容（what）必填");
      return;
    }
    setCreating(true);
    setError("");
    try {
      const sourceChapter = Number(newSourceChapter);
      const created = await api.createCommitment(novelId, {
        what: newWhat.trim(),
        kind: newKind,
        who: newWho.trim(),
        counterpart: newCounterpart.trim(),
        deadline_text: newDeadline.trim(),
        ...(Number.isFinite(sourceChapter) && sourceChapter >= 1
          ? { source_chapter: Math.floor(sourceChapter) }
          : {}),
      });
      setNewWhat("");
      setNewWho("");
      setNewCounterpart("");
      setNewDeadline("");
      setNewSourceChapter("");
      setStatus(`已新增承诺：${created.what}`);
      await loadCommitments(commitmentFilter);
      await onRefresh();
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setCreating(false);
    }
  }

  const overdueCount = commitments.filter((item) => item.status === "OVERDUE").length;
  const issueCodes = Object.entries(invariants?.codes ?? {}).sort((a, b) => b[1] - a[1]);
  const checked = invariants?.checked ?? {};

  const publishChecks = publishReport?.checks || [];
  const publishRisks = publishReport?.risks || [];
  const publishRules = publishReport?.platform_rules || [];
  const publishOpening = publishChecks.filter((item) => OPENING_HOOK_CODES.includes(item.code));
  const publishRest = publishChecks.filter((item) => !OPENING_HOOK_CODES.includes(item.code));
  const publishOk = publishRest.filter((item) => (item.level || "info") === "ok");
  const publishGroups = PUBLISH_LEVEL_ORDER.map((level) => ({
    level,
    items: publishRest.filter((item) => (item.level || "info") === level),
  })).filter((group) => group.items.length > 0);
  const publishWords = publishReport?.words_per_chapter || [];
  const publishDaily = publishReport?.daily_words_targets || [];
  // 规则编号 → 规则；知识库还没读到或拉取失败时为空表，此时检查项只显示编号
  const ruleByCode = new Map<string, CraftRule>();
  (craftRules ?? []).forEach((rule) => ruleByCode.set(rule.code, rule));
  const ruleFor = (item: PublishCheckItem) =>
    item.rule ? ruleByCode.get(item.rule) : undefined;

  return (
    <div>
      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          文风基线
          <span className="spacer" />
          {baseline && <span className="badge">{baseline.source}</span>}
        </div>
        <div className="field-row">
          <button className="primary" onClick={() => void buildBaseline()} disabled={baselineBuilding}>
            {baselineBuilding ? "统计中…" : "用本书已完成章节建立基线"}
          </button>
          <button onClick={() => void loadBaseline()} disabled={baselineLoading}>
            {baselineLoading ? "刷新中…" : "刷新"}
          </button>
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          基线用作者认可的章节统计每个指标的分布，之后的评审以这本书自己的分布为准；
          少于 3 章或样本不足时后端会返回 409。
        </div>
        {baseline ? (
          <>
            <div style={{ marginTop: 6 }}>
              <b>{baseline.name}</b>｜样本 {baseline.sample_count} 个｜{baseline.total_chars} 字｜
              {baseline.samples.length > 0 ? `样本：${baseline.samples.join("、")}` : "未记录样本"}
              {baseline.is_default && <span className="badge ok">默认</span>}
            </div>
            <div className="hint">
              建立于 {baseline.created_at.slice(0, 19).replace("T", " ")}
            </div>
            <div style={{ marginTop: 6 }}>
              <BaselineMetricTable metrics={baseline.metrics} />
            </div>
          </>
        ) : (
          <div className="hint" style={{ marginTop: 6 }}>
            {baselineLoading ? "正在读取基线…" : "本书还没有文风基线。"}
          </div>
        )}
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          作者声音画像
          <span className="spacer" />
          {voice?.available && <span className="badge ok">已建立</span>}
        </div>
        <div className="field-row">
          <button
            className="primary"
            onClick={() => void buildVoice({})}
            disabled={voiceBuilding}
          >
            {voiceBuilding ? "统计中…" : "用本书已完成章节建立（至少 3 章）"}
          </button>
          <button onClick={() => void loadVoice()} disabled={voiceLoading}>
            {voiceLoading ? "读取中…" : "刷新"}
          </button>
        </div>
        <textarea
          placeholder="也可以直接贴你自己写的文本（碎片、日记、随手写的段落都算），每段之间空一行；至少 2 段、每段 20 字以上"
          value={voiceText}
          onChange={(event) => setVoiceText(event.target.value)}
          rows={3}
          style={{ width: "100%", marginTop: 6 }}
        />
        <div className="field-row" style={{ marginTop: 6 }}>
          <button
            onClick={() =>
              void buildVoice({
                texts: voiceText
                  .split(/\n\s*\n/)
                  .map((part) => part.trim())
                  .filter(Boolean),
              })
            }
            disabled={voiceBuilding || !voiceText.trim()}
          >
            用这些文本建立画像
          </button>
        </div>
        {voice?.available ? (
          <>
            <div style={{ marginTop: 6 }}>
              <b>{voice.name}</b>｜样本 {voice.sample_count} 个｜{voice.total_chars} 字
              {voice.created_at ? `｜建立于 ${voice.created_at.slice(0, 19).replace("T", " ")}` : ""}
            </div>
            <div className="field-row" style={{ marginTop: 4 }}>
              {voice.signature_terms.slice(0, 20).map((term) => (
                <span key={term} className="badge ok">
                  {term}
                </span>
              ))}
              {voice.signature_terms.length === 0 && <span className="hint">没有提取到特征词。</span>}
            </div>
          </>
        ) : (
          <div className="hint" style={{ marginTop: 6 }}>
            {voiceLoading ? "正在读取画像…" : "还没有声音画像。"}
          </div>
        )}
        <div className="hint" style={{ marginTop: 4 }}>
          画像决定成文时的「声音保留分」：它衡量写出来的东西有多像你（越高越好），与
          「有没有 AI 味」的文风得分是两项不同的指标，成文面板会同时给出。
        </div>
      </div>

      <div className="issue">
        <div className="issue-title">评一段草稿</div>
        <textarea
          placeholder="粘贴一段待评审的草稿（不落库，只做指标与读感评审）"
          value={draftText}
          onChange={(event) => setDraftText(event.target.value)}
          rows={4}
          style={{ width: "100%", marginTop: 4 }}
        />
        <div className="field-row" style={{ marginTop: 6 }}>
          <button
            className="primary"
            onClick={() => void reviewDraft()}
            disabled={draftReviewing || !draftText.trim()}
          >
            {draftReviewing ? "评审中…" : "评审"}
          </button>
          {draftReview && <button onClick={() => setDraftReview(null)}>清空结果</button>}
          <span className="hint">{draftText.length} 字</span>
        </div>
        {draftReview && (
          <div style={{ marginTop: 8 }}>
            <StyleReviewView review={draftReview} />
          </div>
        )}
      </div>

      <div className="issue">
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          全局不变量
          <span className="spacer" />
          {invariants && invariants.errors > 0 ? (
            <span className="badge error">{invariants.errors} 错误</span>
          ) : (
            <span className="badge ok">无错误</span>
          )}
          {invariants && <span className={`badge ${invariants.warnings > 0 ? "warning" : ""}`}>
            {invariants.warnings} 警告
          </span>}
        </div>
        <div className="field-row">
          <button className="primary" onClick={() => void rerunInvariants()} disabled={invariantsRunning}>
            {invariantsRunning ? "检查中…" : "重跑全局检查"}
          </button>
          <button onClick={() => void loadInvariants()} disabled={invariantsLoading}>
            {invariantsLoading ? "读取中…" : "读最近一次报告"}
          </button>
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          检查时间倒置、境界回退、独占冲突、认知回缩、地点跳跃、承诺越界、人物长期未登场等跨章不变量；
          重跑会留存一份报告。
        </div>
        {invariants && (
          <>
            <div className="hint" style={{ marginTop: 6 }}>
              检查范围：章节 {checked.chapters ?? "—"}｜Canon 事实 {checked.facts ?? "—"}｜人物{" "}
              {checked.characters ?? "—"}
              {invariants.created_at
                ? `｜报告时间 ${invariants.created_at.slice(0, 19).replace("T", " ")}`
                : "｜此前没有留存报告"}
              {invariants.report_id ? `｜报告 ${invariants.report_id}` : ""}
            </div>
            <div className="field-row" style={{ marginTop: 4 }}>
              {issueCodes.length === 0 && <span className="hint">没有命中的检查项。</span>}
              {issueCodes.map(([code, count]) => (
                <span key={code} className="badge warning">
                  {code} ×{count}
                </span>
              ))}
            </div>
          </>
        )}
        {!invariants && <div className="hint">尚未读取检查结果。</div>}
      </div>

      {invariants && invariants.issues.length > 0 && (
        <div>
          {invariants.issues.map((issue, index) => (
            <div key={`${issue.code}-${index}`} className={`issue ${LEVEL_CLASS[issue.level] ?? "info"}`}>
              <div className="issue-title">
                <span className={`badge ${LEVEL_CLASS[issue.level] ?? "info"}`}>
                  {LEVEL_LABELS[issue.level] ?? issue.level}
                </span>{" "}
                {issue.code}
                {issue.subject && <span className="hint">（{issue.subject}）</span>}
              </div>
              <div>{issue.message}</div>
              {issue.evidence.length > 0 && (
                <ul>
                  {issue.evidence.map((evidence, position) => (
                    <li key={position}>
                      [{evidence.source_chapter}] {evidence.quote || evidence.detail || "—"}
                      {evidence.quote && evidence.detail ? `（${evidence.detail}）` : ""}
                    </li>
                  ))}
                </ul>
              )}
              {issue.suggestion && <div className="fix">建议：{issue.suggestion}</div>}
            </div>
          ))}
        </div>
      )}

      <div className="issue" style={{ marginTop: 10 }}>
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          承诺账本
          <span className="spacer" />
          {overdueCount > 0 ? (
            <span className="badge error">{overdueCount} 条已逾期</span>
          ) : (
            <span className="badge ok">没有逾期</span>
          )}
        </div>
        <div className="field-row">
          {STATUS_FILTERS.map((value) => (
            <button
              key={value}
              className={commitmentFilter === value ? "primary" : ""}
              onClick={() => setCommitmentFilter(value)}
            >
              {STATUS_LABELS[value]}
            </button>
          ))}
          <span className="hint">当前列出 {commitments.length} 条</span>
        </div>
        <div className="field-row" style={{ marginTop: 6 }}>
          <label style={{ flex: "0 0 auto" }}>
            兑现章号（可留空）
            <input
              type="number"
              min={1}
              style={{ width: 90 }}
              value={fulfilChapter}
              placeholder="可留空"
              onChange={(event) => setFulfilChapter(event.target.value)}
            />
          </label>
          <button onClick={() => void loadCommitments(commitmentFilter)} disabled={commitmentsLoading}>
            {commitmentsLoading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      <table className="grid">
        <thead>
          <tr>
            <th>类型</th>
            <th>内容</th>
            <th>来源章</th>
            <th>期限</th>
            <th>到期</th>
            <th>状态</th>
            <th>越界章</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {commitments.map((item) => (
            <tr key={item.id}>
              <td>
                <span className="badge">{KIND_LABELS[item.kind] ?? item.kind}</span>
              </td>
              <td>
                {item.what}
                {item.who || item.counterpart ? (
                  <div className="hint">
                    {item.who || "—"}
                    {item.counterpart ? ` → ${item.counterpart}` : ""}
                  </div>
                ) : null}
                {item.quote && <div className="hint">原文：{item.quote}</div>}
              </td>
              <td>{item.source_chapter ? `第${item.source_chapter}章` : "—"}</td>
              <td>{item.deadline_text || "—"}</td>
              <td>
                {item.due_story_time || "—"}
                {item.days_remaining !== null && (
                  <div className="hint">还剩 {item.days_remaining} 天</div>
                )}
              </td>
              <td>
                <span className={`badge ${STATUS_CLASS[item.status] ?? ""}`}>
                  {STATUS_LABELS[item.status]}
                </span>
                {item.stored_status !== item.status && (
                  <div className="hint">库内：{item.stored_status}</div>
                )}
                {item.fulfilled_chapter && (
                  <div className="hint">第{item.fulfilled_chapter}章兑现</div>
                )}
              </td>
              <td>{item.breach_chapter ? `第${item.breach_chapter}章` : "—"}</td>
              <td>
                <button
                  className="primary"
                  onClick={() => void decideCommitment(item, "fulfill")}
                  disabled={actingOn !== null || item.status === "FULFILLED"}
                >
                  已兑现
                </button>{" "}
                <button
                  className="danger"
                  onClick={() => void decideCommitment(item, "abandon")}
                  disabled={actingOn !== null || item.status === "ABANDONED"}
                >
                  放弃
                </button>
              </td>
            </tr>
          ))}
          {commitments.length === 0 && (
            <tr>
              <td colSpan={8}>
                {commitmentsLoading ? "正在加载承诺…" : "这个筛选下没有承诺。"}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="issue" style={{ marginTop: 10 }}>
        <div className="issue-title">新增承诺</div>
        <div className="field-row">
          <label style={{ flex: "2 1 240px" }}>
            内容（必填）
            <input
              value={newWhat}
              placeholder="例如：三日后在听雨楼交还玄铁令"
              onChange={(event) => setNewWhat(event.target.value)}
            />
          </label>
          <label style={{ flex: "0 0 auto" }}>
            类型
            <select value={newKind} onChange={(event) => setNewKind(event.target.value as CommitmentKind)}>
              {KIND_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {KIND_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <label>
            来源章
            <input
              type="number"
              min={1}
              value={newSourceChapter}
              placeholder="可留空"
              onChange={(event) => setNewSourceChapter(event.target.value)}
            />
          </label>
        </div>
        <div className="field-row" style={{ marginTop: 6 }}>
          <label>
            当事人
            <input value={newWho} onChange={(event) => setNewWho(event.target.value)} />
          </label>
          <label>
            相对方
            <input
              value={newCounterpart}
              onChange={(event) => setNewCounterpart(event.target.value)}
            />
          </label>
          <label>
            期限（原文表述）
            <input
              value={newDeadline}
              placeholder="例如：三日后"
              onChange={(event) => setNewDeadline(event.target.value)}
            />
          </label>
        </div>
        <div className="field-row" style={{ marginTop: 8 }}>
          <button className="primary" onClick={() => void addCommitment()} disabled={creating}>
            {creating ? "写入中…" : "新增承诺"}
          </button>
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          只有写明期限的承诺才会被算作逾期；越界章由后端按各章故事时间推断。
        </div>
      </div>

      <div className="issue" style={{ marginTop: 10 }}>
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          设定断言核对
          <span className="spacer" />
          {claimReport && (
            <>
              {claimReport.conflict_count > 0 ? (
                <span className="badge error">{claimReport.conflict_count} 条冲突</span>
              ) : (
                <span className="badge ok">无冲突</span>
              )}
              <span className={`badge ${claimReport.unverified_count > 0 ? "warning" : ""}`}>
                {claimReport.unverified_count} 条待确认
              </span>
            </>
          )}
        </div>
        <textarea
          placeholder="粘贴一段待核对的草稿（例如：林默的佩剑是青云剑。）"
          value={claimText}
          onChange={(event) => setClaimText(event.target.value)}
          rows={3}
          style={{ width: "100%", marginTop: 4 }}
        />
        <div className="field-row" style={{ marginTop: 6 }}>
          <label style={{ flex: "0 0 auto" }}>
            按章节号核对（填了就核对这一章正文）
            <input
              type="number"
              min={1}
              style={{ width: 100 }}
              value={claimChapter}
              placeholder="可留空"
              onChange={(event) => setClaimChapter(event.target.value)}
            />
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", flex: "0 0 auto" }}>
            <input
              type="checkbox"
              checked={claimUseModel}
              style={{ width: "auto" }}
              onChange={(event) => setClaimUseModel(event.target.checked)}
            />
            用模型拆断言
          </label>
          <button className="primary" onClick={() => void verifyClaims()} disabled={claimRunning}>
            {claimRunning ? "核对中…" : "核对设定"}
          </button>
          {claimReport && <button onClick={() => setClaimReport(null)}>清空结果</button>}
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          把正文里的设定断言与 Canon／世界观规则逐条比对，只出报告、绝不写 Canon：
          查无记录的一律是「待确认」，确认与否由你决定。去掉「用模型拆断言」只跑确定性规则，
          秒级完成且不产生模型费用。
        </div>
        {claimReport && (
          <>
            <div className="hint" style={{ marginTop: 6 }}>
              共 {claimReport.claim_count} 条断言｜冲突 {claimReport.conflict_count}｜待确认{" "}
              {claimReport.unverified_count}｜一致 {claimReport.supported_count}｜
              {reportLabel(claimReport)}｜{claimReport.provider}/{claimReport.model}
              {claimReport.as_of_chapter ? `｜按第${claimReport.as_of_chapter}章时点比对` : ""}
              {claimReport.created_at
                ? `｜${claimReport.created_at.slice(0, 19).replace("T", " ")}`
                : ""}
            </div>
            {claimReport.summary && <div className="hint">模型小结：{claimReport.summary}</div>}
            {(claimReport.warnings || []).map((warning, index) => (
              <div key={`claim-warning-${index}`} className="hint">
                · {warning}
              </div>
            ))}
            <ClaimVerdictGroups report={claimReport} />
          </>
        )}
      </div>

      <div className="issue" style={{ marginTop: 10 }}>
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          历史核对报告
          <span className="spacer" />
          <button onClick={() => void loadClaimHistory()} disabled={claimHistoryLoading}>
            {claimHistoryLoading ? "读取中…" : "刷新"}
          </button>
        </div>
        <div className="hint">
          每次核对都会落库（正文核对存 CHAPTER，贴草稿存 DRAFT），这里只列最近 10 份。
        </div>
        {claimHistory.length === 0 && (
          <div className="hint">{claimHistoryLoading ? "正在读取…" : "还没有核对记录。"}</div>
        )}
        {claimHistory.map((item) => {
          const key = item.id || `${item.label}-${item.created_at || ""}-${item.claim_count}`;
          const open = openClaimReport === key;
          return (
            <Fragment key={key}>
              <div className="item-row">
                <div className="row-head">
                  <b>{reportLabel(item)}</b>
                  {item.conflict_count > 0 ? (
                    <span className="badge error">{item.conflict_count} 冲突</span>
                  ) : (
                    <span className="badge ok">无冲突</span>
                  )}
                  <span className={`badge ${item.unverified_count > 0 ? "warning" : ""}`}>
                    {item.unverified_count} 待确认
                  </span>
                  <span className="hint">
                    一致 {item.supported_count}／共 {item.claim_count}
                  </span>
                  <span className="spacer" />
                  <button onClick={() => setOpenClaimReport(open ? null : key)}>
                    {open ? "收起" : "详情"}
                  </button>
                </div>
                <div className="payload">
                  {item.created_at ? item.created_at.slice(0, 19).replace("T", " ") : "时间未知"}｜
                  {item.provider}/{item.model}
                  {item.as_of_chapter ? `｜按第${item.as_of_chapter}章时点比对` : ""}
                </div>
                {item.summary && <div className="payload">小结：{item.summary}</div>}
              </div>
              {open && <ClaimVerdictGroups report={item} />}
            </Fragment>
          );
        })}
      </div>

      <div className="issue" style={{ marginTop: 10 }}>
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          文风锁定与漂移
          <span className="spacer" />
          {baseline &&
            (baseline.locked ? (
              <span className="badge ok">已锁定</span>
            ) : (
              <span className="badge">未锁定</span>
            ))}
          {drift && (
            <span className={`badge ${drift.drifted_count > 0 ? "warning" : ""}`}>
              漂移 {drift.drifted_count} 章
            </span>
          )}
        </div>
        <div className="field-row">
          <button
            className="primary"
            onClick={() => void toggleStyleLock(true)}
            disabled={!baseline || locking || baseline.locked}
          >
            {locking ? "处理中…" : "锁定文风"}
          </button>
          <button
            onClick={() => void toggleStyleLock(false)}
            disabled={!baseline || locking || !baseline.locked}
          >
            解除锁定
          </button>
          <button onClick={() => void loadDrift()} disabled={driftLoading}>
            {driftLoading ? "读取中…" : "刷新漂移"}
          </button>
        </div>
        <div style={{ marginTop: 6 }}>
          基线：<b>{drift?.profile_name || baseline?.name || "未建立"}</b>
          {baseline ? `｜样本 ${baseline.sample_count} 个｜${baseline.total_chars} 字` : ""}
          {drift ? `｜比对方式：${DIRECTION_LABELS[drift.direction] || drift.direction}` : ""}
        </div>
        <div className="hint">
          {baseline?.locked
            ? "已锁定：新章按收紧一半的窗口比对（容差 ×0.5），慢慢漂走的章会被点名；同一本书同时只有一个锁定的基线。"
            : "未锁定：只按基线窗口比对。锁定文风后新章会按收紧一半的窗口比对，漂移更容易被抓出来。"}
          {drift ? `本次比对覆盖 ${drift.chapters.length} 章（不足 400 字的章跳过）。` : ""}
        </div>
        {!drift && (
          <div className="hint" style={{ marginTop: 4 }}>
            {driftLoading ? "正在读取漂移报告…" : "还没有读取漂移报告。"}
          </div>
        )}
        {drift && (drift.chapters || []).length === 0 && (
          <div className="hint" style={{ marginTop: 4 }}>
            没有足够长的章节可以比对。
          </div>
        )}
        {(drift?.chapters || []).map((chapter) => {
          const drifted = chapter.drifted || [];
          return (
            <div key={chapter.chapter_number} className="item-row">
              <div className="row-head">
                <span className="badge">第{chapter.chapter_number}章</span>
                <b>{chapter.title || "（无标题）"}</b>
                <span className="spacer" />
                <span className="hint">评分 {formatScore(chapter.score)}</span>
                {drifted.length > 0 ? (
                  <span className="badge warning">{drifted.length} 项漂移</span>
                ) : (
                  <span className="badge ok">在窗口内</span>
                )}
              </div>
              {drifted.length > 0 && (
                <details style={{ marginTop: 4 }}>
                  <summary>漂移指标（{drifted.length}）</summary>
                  <table className="grid metric-table">
                    <thead>
                      <tr>
                        <th>指标</th>
                        <th>本章</th>
                        <th>基线均值</th>
                        <th>阈值区间</th>
                        <th>越界方向</th>
                      </tr>
                    </thead>
                    <tbody>
                      {drifted.map((item) => (
                        <tr key={item.metric}>
                          <td>
                            {metricShortLabel(item.metric)}
                            <div className="hint">
                              {METRIC_DIRECTION_LABELS[item.direction] || item.direction}
                            </div>
                          </td>
                          <td>{formatMetric(item.value)}</td>
                          <td>{formatMetric(item.mean)}</td>
                          <td>
                            [{formatMetric(item.low)}, {formatMetric(item.high)}]
                          </td>
                          <td>
                            {item.side === "low" ? "低于下界" : "高于上界"}
                            <div className="hint">
                              {item.side === "low"
                                ? `≤ ${formatMetric(item.low)}`
                                : `≥ ${formatMetric(item.high)}`}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}
            </div>
          );
        })}
      </div>

      <div className="issue" style={{ marginTop: 10 }}>
        <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          发布前检查（番茄免费小说）
          <span className="spacer" />
          {publishReport &&
            (publishReport.ready ? (
              <span className="badge ok">可以发</span>
            ) : (
              <span className="badge error">先处理阻断项</span>
            ))}
        </div>
        <textarea
          placeholder="粘贴待发布的这一章（首行写成「第N章 标题」；只做检查，不落库）"
          value={publishText}
          onChange={(event) => setPublishText(event.target.value)}
          rows={4}
          style={{ width: "100%", marginTop: 4 }}
        />
        <div className="field-row" style={{ marginTop: 6 }}>
          <label style={{ flex: "0 0 auto" }}>
            按章节号核对已入库的正文（填了就按那一章查，忽略上面的草稿）
            <input
              type="number"
              min={1}
              style={{ width: 100 }}
              value={publishChapter}
              placeholder="可留空"
              onChange={(event) => setPublishChapter(event.target.value)}
            />
          </label>
          <button
            className="primary"
            onClick={() => void runPublishCheck()}
            disabled={publishChecking || (!publishText.trim() && !publishChapter.trim())}
          >
            {publishChecking ? "检查中…" : "检查"}
          </button>
          {publishReport && <button onClick={() => setPublishReport(null)}>清空结果</button>}
          <span className="hint">{publishText.length} 字</span>
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          口径来自番茄的签约标准与低质治理公告：不接受「AI 粗制滥造、格式混乱、结构失常、空洞水文」，
          优质内容看重「开篇快速进入主线」，福利按每日 4000 或 6000 字与完读率算。
          阻断项（level=error，目前只有审核风险词）不处理完不该发。
        </div>
        {publishReport && (
          <>
            <div className={publishReport.ready ? "" : "error-text"} style={{ marginTop: 6 }}>
              <b>{publishReport.word_count} 字</b>｜阻断 <b>{publishReport.blocking}</b>｜警告{" "}
              <b>{publishReport.warnings}</b>｜
              {publishReport.ready ? "可以发" : "先处理阻断项"}
            </div>
            <div className="hint">
              {publishReport.title
                ? publishReport.title
                : (publishReport.format_issues || []).some(
                      (item) => item.code === "FORMAT_TITLE_MISSING",
                    )
                  ? "（首行未识别到章节标题）"
                  : "（贴草稿时未带标题；首行格式已在检查项里核对）"}
              {publishReport.chapter_number ? `｜第${publishReport.chapter_number}章` : ""}
              ｜平台：{publishReport.platform || "—"}
              {publishWords.length === 2 ? `｜按章长度 ${publishWords[0]}–${publishWords[1]} 字` : ""}
              {publishDaily.length > 0 ? `｜每日更新目标 ${publishDaily.join(" / ")} 字` : ""}
              ｜格式问题 {(publishReport.format_issues || []).length} 项（已并入下面的清单）
            </div>

            {publishOpening.length > 0 && (
              <div className="issue warning" style={{ marginTop: 6 }}>
                <div className="issue-title">
                  开篇与章末（{publishOpening.length} 项，直接影响完读率）
                </div>
                {publishOpening.map((item, index) => (
                  <PublishCheckRow
                    key={`opening-${item.code}-${index}`}
                    item={item}
                    rule={ruleFor(item)}
                  />
                ))}
              </div>
            )}

            <div className="issue" style={{ marginTop: 6 }}>
              <div className="issue-title">平台四条低质规则</div>
              {publishRules.length === 0 && (
                <div className="hint">这次响应里没有规则映射（后端可能没算文风指标）。</div>
              )}
              {publishRules.map((rule) => (
                <div key={rule.rule} className="item-row">
                  <div className="row-head">
                    <span className={`badge ${rule.hit ? "error" : "ok"}`}>
                      {rule.hit ? "命中" : "未命中"}
                    </span>
                    <b>{rule.rule}</b>
                  </div>
                  <div className="payload">
                    {rule.evidence ? `命中指标：${rule.evidence}` : "没有命中这一类的指标"}
                  </div>
                  {rule.advice ? <div className="payload">建议：{rule.advice}</div> : null}
                </div>
              ))}
            </div>

            <div className="issue" style={{ marginTop: 6 }}>
              <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                检查项清单
                <span className="spacer" />
                <span className="hint" style={{ color: "inherit" }}>
                  共 {publishRest.length} 项{publishOpening.length > 0 ? "（开篇与章末另列）" : ""}
                </span>
              </div>
              {publishGroups.length === 0 && publishOk.length === 0 && (
                <div className="hint">这次没有任何检查项。</div>
              )}
              {publishGroups.map((group) => (
                <div
                  key={group.level}
                  className={`issue ${PUBLISH_LEVEL_CLASS[group.level] ?? "info"}`}
                  style={{ marginTop: 6 }}
                >
                  <div className="issue-title">
                    {PUBLISH_LEVEL_LABELS[group.level] ?? group.level}（{group.items.length}）
                  </div>
                  {group.items.map((item, index) => (
                    <PublishCheckRow
                      key={`${group.level}-${item.code}-${index}`}
                      item={item}
                      rule={ruleFor(item)}
                    />
                  ))}
                </div>
              ))}
              {publishOk.length > 0 && (
                <details style={{ marginTop: 6 }}>
                  <summary>已通过的检查（{publishOk.length}）</summary>
                  {publishOk.map((item, index) => (
                    <PublishCheckRow key={`ok-${item.code}-${index}`} item={item} rule={ruleFor(item)} />
                  ))}
                </details>
              )}
            </div>

            <div className="issue" style={{ marginTop: 6 }}>
              <div className="issue-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                审核风险词
                <span className="spacer" />
                {publishRisks.length > 0 ? (
                  <span className="badge error">{publishRisks.length} 处</span>
                ) : (
                  <span className="badge ok">没扫到风险词</span>
                )}
              </div>
              {publishRisks.length === 0 && <div className="hint">没扫到风险词。</div>}
              {publishRisks.map((risk, index) => (
                <div key={`${risk.category}-${risk.word}-${index}`} className="item-row">
                  <div className="row-head">
                    <span className="badge error">{risk.category || "未分类"}</span>
                    <b>{risk.word}</b>
                  </div>
                  {risk.quote ? <div className="payload">上下文：{risk.quote}</div> : null}
                  {risk.advice ? <div className="payload">建议：{risk.advice}</div> : null}
                </div>
              ))}
            </div>
          </>
        )}

        <details className="issue info" style={{ marginTop: 8, marginBottom: 0 }}>
          <summary>
            这些检查的依据（写作规则知识库 · {craftRules?.length ?? 0} 条）
          </summary>
          <div className="hint" style={{ marginTop: 4 }}>
            每条规则都标了出处：可以回去核对平台课程的原文，而不是只听一句经验阈值。
          </div>
          {craftRulesLoading && <div className="hint">正在读取规则知识库…</div>}
          {!craftRulesLoading && (craftRules?.length ?? 0) === 0 && (
            <div className="hint">这次没读到规则知识库（不影响上面的检查结果）。</div>
          )}
          {(craftRules ?? []).map((rule) => (
            <div key={rule.code} className="item-row">
              <div className="row-head">
                <span className="badge">{rule.code}</span>
                <b>{rule.summary}</b>
              </div>
              <div className="payload">出处：{rule.source}</div>
              {rule.advice ? <div className="payload">做法：{rule.advice}</div> : null}
            </div>
          ))}
        </details>
      </div>
    </div>
  );
}
