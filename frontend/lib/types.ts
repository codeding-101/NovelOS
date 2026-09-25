export interface Novel {
  id: string;
  title: string;
  slug: string;
  synopsis: string;
  genre: string;
  worldview: string;
  /** 全书大纲：主线、分卷、人物弧线、结局走向。会进规划与写作的提示词。 */
  outline: string;
  /** 每章字数区间：发布前检查按它判定是否贴合平台要求。 */
  chapter_words_min: number;
  chapter_words_max: number;
  /** 每日更新目标字数：平台福利按每日有效字数算，默认 4000。 */
  daily_words_target: number;
  author: string;
  target_word_count: number;
  word_count: number;
  chapter_count: number;
}

export interface ChapterSummary {
  chapter_id: string;
  novel_id: string;
  chapter_number: number;
  title: string;
  summary: string;
  word_count: number;
  status: string;
  story_time: string | null;
  location: string | null;
  created_at: string;
  updated_at: string;
}

export interface Chapter extends ChapterSummary {
  content: string;
  content_path: string;
}

export interface NovelStats {
  novel_id: string;
  word_count: number;
  chapter_count: number;
  target_word_count: number;
  progress: number;
  character_count: number;
  event_count: number;
  canon_fact_count: number;
  proposed_fact_count: number;
  foreshadowing_open_count: number;
  timeline_entry_count: number;
  pending_review_items: number;
  latest_continuity_errors: number;
  latest_continuity_warnings: number;
}

export interface Character {
  id: string;
  name: string;
  description: string;
  personality: string;
  background: string;
  goals: string[];
  fears: string[];
  current_location: string;
  current_status: string;
  known_facts: string[];
  unknown_facts: string[];
  first_appearance: number | null;
  last_appearance: number | null;
}

export interface CharacterState {
  id: string;
  character_id: string;
  chapter_number: number | null;
  status: string;
  location: string;
  note: string;
  source: string;
  created_at: string;
}

export interface CanonFact {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  source_chapter: number | null;
  /** 该事实从第几章起生效（null 表示不限）。 */
  valid_from_chapter: number | null;
  /** 该事实在第几章后失效（null 表示仍然有效）。 */
  valid_until_chapter: number | null;
  status: string;
  confidence: number;
  visibility: string;
  known_by: string[];
  origin: string;
  note: string;
  superseded_by: string | null;
}

export interface Foreshadowing {
  id: string;
  name: string;
  description: string;
  first_chapter: number | null;
  related_characters: string[];
  expected_payoff: string;
  status: string;
  last_reinforced_chapter: number | null;
}

export interface TimelineEntry {
  id: string;
  story_time: string;
  story_time_sort: number;
  chapter_number: number | null;
  event: string;
  location: string;
  description: string;
  status: string;
}

export interface EventRecord {
  id: string;
  chapter_number: number | null;
  time: string | null;
  location: string;
  characters: string[];
  description: string;
  consequences: string;
  status: string;
}

export interface WorldRule {
  id: string;
  name: string;
  rule_type: string;
  subject: string;
  params: Record<string, unknown>;
  description: string;
  source_chapter: number | null;
}

export interface Evidence {
  source_chapter: string;
  ref_type: string;
  ref_id: string | null;
  quote: string | null;
  detail: string | null;
}

export interface ContinuityIssue {
  level: "error" | "warning";
  code: string;
  message: string;
  evidence: Evidence[];
  suggested_fix: string | null;
}

export interface ContinuityReport {
  chapter_id: string;
  chapter_number: number | null;
  errors: ContinuityIssue[];
  warnings: ContinuityIssue[];
  dropped_issues: Record<string, unknown>[];
  provider: string;
  model: string;
  report_id: string | null;
  created_at: string | null;
}

export interface ExtractionItem {
  id: string;
  run_id: string;
  kind: string;
  payload: Record<string, any>;
  review_status: "PENDING" | "ACCEPTED" | "REJECTED";
  linked_fact_id: string | null;
  applied_ref_id: string | null;
  created_at: string;
}

export interface ExtractionRun {
  id: string;
  novel_id: string;
  chapter_id: string;
  chapter_number: number | null;
  status: string;
  provider: string;
  model: string;
  payload: Record<string, any>;
  warnings: string[];
  created_at: string;
  applied_at: string | null;
  items: ExtractionItem[];
}

export interface ApplyResult {
  run_id: string;
  status: string;
  applied: Record<string, any>[];
  skipped: Record<string, any>[];
  message: string;
}

export interface WorkflowStep {
  step: number;
  name: string;
  status: "ok" | "warning" | "error" | "skipped";
  detail: string;
}

export interface CompletionReport {
  chapter_id: string;
  chapter_number: number | null;
  steps: WorkflowStep[];
  extraction_run: ExtractionRun | null;
  continuity: ContinuityReport | null;
  /** V0.5：工作流里的设定断言核对结果（未跑或失败时为 null）。 */
  claims?: ClaimReport | null;
  pending_items: number;
  proposed_facts: number;
  errors: number;
  warnings: number;
  message: string;
  notes?: string[];
}

export interface EvidenceItem {
  ref_type: string;
  ref_id: string | null;
  title: string;
  chapter_number: number | null;
  chapter_title: string | null;
  excerpt: string;
  score: number;
}

export interface ToolCallRecord {
  name: string;
  arguments: unknown;
  status: string;
}

export interface AskResponse {
  question: string;
  answer: string;
  confidence: string;
  evidence: EvidenceItem[];
  provider: string;
  model: string;
  warnings: string[];
  /** simple = 后端先检索再由模型组织语言；agent = 模型自己调用工具取数。 */
  mode: string;
  tool_calls: ToolCallRecord[];
}

export type AskMode = "simple" | "agent";

/** 写作前语义召回的前文片段。 */
export interface SemanticHit {
  chapter_number: number | null;
  chapter_title: string;
  excerpt: string;
  score: number;
}

export interface RetrievalBundle {
  canon_facts: Record<string, any>[];
  characters: Record<string, any>[];
  events: Record<string, any>[];
  timeline: Record<string, any>[];
  foreshadowing: Record<string, any>[];
  world_rules: Record<string, any>[];
  recent_chapter_summaries: Record<string, any>[];
  semantic_hits: SemanticHit[];
  /** V0.4：作者自己的想法碎片（写成正文时优先体现）。 */
  fragments: Record<string, any>[];
  notes: string[];
}

/** 一次生成的章节草稿（写作与修订闭环共用同一结构）。 */
export interface ChapterDraft {
  title: string;
  content: string;
  word_count: number;
  chapter_number: number | null;
  story_time: string | null;
  location: string | null;
}

export interface WriteChapterResponse {
  draft: ChapterDraft;
  retrieved: RetrievalBundle;
  provider: string;
  model: string;
  warnings: string[];
  saved_chapter_id: string | null;
  generation_id: string | null;
}

export interface ProviderInfo {
  name: string;
  model: string;
  kind: string;
  available: boolean;
  supports_tools: boolean;
  detail: string;
}

export interface SearchHit {
  chapter_id: string;
  chapter_number: number;
  title: string;
  snippet: string;
  score: number;
  match_source: string;
}

export interface Bible {
  characters: Character[];
  relationships: { id: string; character_a: string; character_b: string; relation: string; description: string }[];
  events: EventRecord[];
  canon_facts: CanonFact[];
  foreshadowings: Foreshadowing[];
  timeline: TimelineEntry[];
  world_rules: WorldRule[];
}

/** 第 N 章时的人物状态快照。 */
export interface AsOfCharacter {
  name: string;
  status_at_chapter: string;
  location_at_chapter: string;
  current_status: string;
  first_appearance: number | null;
  /** 该章之前是否有记录过状态变更（否则取当前值）。 */
  changed_since: boolean;
}

export interface AsOfState {
  novel_id: string;
  chapter_number: number;
  canon_facts: CanonFact[];
  characters: AsOfCharacter[];
  timeline: TimelineEntry[];
  events: EventRecord[];
  foreshadowings: Foreshadowing[];
  world_rules: WorldRule[];
  notes: string[];
}

export interface ForeshadowingDebtItem {
  id: string;
  name: string;
  status: string;
  description: string;
  first_chapter: number | null;
  last_reinforced_chapter: number | null;
  related_characters: string[];
  expected_payoff: string;
  /** 已多少章未推进。 */
  age: number;
  overdue: boolean;
  evidence: string;
}

export interface ForeshadowingSuggestion {
  foreshadowing_id: string;
  name: string;
  status: string;
  age: number;
  overdue: boolean;
  suggested_chapter: number;
  urgency: "HIGH" | "MEDIUM" | "LOW";
  related_characters: string[];
  reason: string;
  expected_payoff: string;
}

export interface ForeshadowingPlan {
  frontier_chapter: number;
  horizon: number;
  overdue_count: number;
  open_count: number;
  suggestions: ForeshadowingSuggestion[];
  debt: ForeshadowingDebtItem[];
}

export type SweepMode = "rules" | "full";

export interface SweepPayload {
  mode: SweepMode;
  provider?: string;
  narrative_pass?: boolean;
  chapter_numbers?: number[];
}

export interface SweepDetail {
  chapters?: unknown[];
  error_totals?: Record<string, number>;
  warning_totals?: Record<string, number>;
}

export interface SweepRun {
  id: string;
  novel_id: string;
  mode: string;
  provider: string;
  model: string;
  chapters_total: number;
  chapters_checked: number;
  errors: number;
  warnings: number;
  detail: SweepDetail;
  started_at: string;
  finished_at: string | null;
}

export interface ChapterHealth {
  chapter_number: number;
  title: string;
  word_count: number;
  checked_at: string | null;
  errors: number;
  warnings: number;
  top_codes: string[];
}

export interface EmbeddingStats {
  records: number;
  by_ref_type: Record<string, number>;
  providers: string[];
  dim: number;
}

export interface ReindexResult {
  provider: string;
  dim: number;
  chapters: number;
  chapter_chunks: number;
  entities: Record<string, number>;
  warnings: string[];
}

export interface Dashboard {
  novel_id: string;
  word_count: number;
  chapter_count: number;
  target_word_count: number;
  chapters: ChapterHealth[];
  error_totals: Record<string, number>;
  error_chapters: number[];
  unchecked_chapters: number[];
  foreshadowing_debt: ForeshadowingDebtItem[];
  overdue_foreshadowing: number;
  proposed_backlog: number;
  pending_review_items: number;
  vector_index: EmbeddingStats;
  latest_sweep: SweepRun | null;
  /** V0.3：全局不变量、承诺账本与文风。 */
  invariant_errors: number;
  invariant_warnings: number;
  invariant_codes: Record<string, number>;
  invariant_issues: InvariantIssue[];
  commitments_open: number;
  commitments_overdue: number;
  commitments_overdue_items: Commitment[];
  style_baseline: string;
  style_review_average: number | null;
  /** V0.4：作者声音画像的名称与特征词（没有画像时为空）。 */
  voice_profile: string;
  voice_terms: string[];
  fragments_total: number;
  fragments_unplaced: number;
  fragments_realized: number;
  fragment_realization_rate: number;
  /** kind → 条数。 */
  fragment_kinds: Record<string, number>;
  /** V0.5：文风是否锁定、漂移章数与最近的断言核对统计。 */
  style_locked: boolean;
  style_drift_chapters: number;
  claim_reports: number;
  claim_conflicts: number;
  claim_unverified: number;
  notes: string[];
}

export type RetrievalRefType =
  | "CHAPTER"
  | "CANON_FACT"
  | "TIMELINE"
  | "EVENT"
  | "CHARACTER"
  | "FORESHADOWING";

export interface RetrievalHit {
  ref_type: string;
  ref_id: string;
  chapter_number: number | null;
  title: string;
  excerpt: string;
  keyword_score: number;
  vector_score: number;
  term_weight: number;
  terms_matched: string[];
  channels: string[];
  score: number;
}

export interface RetrievalResult {
  query: string;
  engine: string;
  channels: string[];
  hits: RetrievalHit[];
}

export type PlanStatus = "PLANNED" | "WRITTEN" | "DISCARDED";

export interface ChapterPlan {
  id: string;
  novel_id: string;
  chapter_number: number;
  title: string;
  goals: string;
  must_include: string[];
  forbidden: string[];
  characters: string[];
  advance_foreshadowing: string[];
  rationale: string;
  steer: string;
  status: PlanStatus;
  source: string;
  provider: string;
  model: string;
  created_at: string;
  updated_at: string;
}

export interface PlanGenerateContextSummary {
  frontier_chapter: number;
  canon_facts: number;
  characters: number;
  foreshadowing_debt: number;
  recent_summaries: number;
  skipped: { chapter_number: number; reason: string }[];
}

export interface PlanGenerateResponse {
  plans: ChapterPlan[];
  provider: string;
  model: string;
  warnings: string[];
  context_summary: PlanGenerateContextSummary;
}

// --------------------------------------------------------------------------- V0.3 文风
/** 基线里单个指标的分布（由样章的测量值统计而来）。 */
export interface StyleMetricStat {
  mean: number;
  std: number;
  min: number;
  max: number;
}

export interface StyleProfile {
  id: string;
  novel_id: string;
  name: string;
  /** CHAPTERS（用本书章节建立）或 TEXTS（直接贴文本）。 */
  source: string;
  sample_count: number;
  total_chars: number;
  /**
   * 指标名 → 分布；同时混有 metrics_version / sample_count / samples 等元信息，
   * 读取时必须先判型（见 StyleReviewView 的 metricStat）。
   */
  metrics: Record<string, any>;
  samples: string[];
  is_default: boolean;
  /** V0.5：锁定后新章按收紧一半的窗口比对。 */
  locked: boolean;
  created_at: string;
}

export interface StyleBaselineRequest {
  name?: string;
  /** 指定章节号；留空且没有 texts 时用本书已完成章节。 */
  chapter_numbers?: number[];
  texts?: string[];
  make_default?: boolean;
}

export interface StyleLockRequest {
  locked: boolean;
}

export interface StyleIssue {
  code: string;
  level: "warning" | "info";
  metric: string;
  message: string;
  value: number;
  /** 指标参考值（本书基线均值），没有基线时为 null。 */
  reference: number | null;
  excerpt: string;
  suggestion: string;
}

export interface StyleReview {
  chapter_id: string | null;
  chapter_number: number | null;
  score: number;
  /** 同时含 cliche_hits / hook_signals 等非数值项，读取时需判型。 */
  metrics: Record<string, any>;
  issues: StyleIssue[];
  baseline: string;
  /** 指标名 → 分布。 */
  baseline_metrics: Record<string, any>;
  warnings: string[];
  model_summary: string;
  provider: string;
  model: string;
  review_id: string | null;
}

/** 历史文风评审条目（GET /style/reviews）。 */
export interface StyleReviewSummary {
  id: string;
  chapter_number: number | null;
  label: string;
  score: number;
  codes: string[];
  metrics: Record<string, any>;
  provider: string;
  model: string;
  created_at: string;
}

/** 逐章漂移里的一个越界指标（direction：lower_better / higher_better / range；side：low / high）。 */
export interface StyleDriftItem {
  chapter_number: number;
  title: string;
  score: number;
  drifted: {
    metric: string;
    value: number;
    mean: number;
    low: number;
    high: number;
    direction: string;
    side: string;
  }[];
}

export interface StyleDriftReport {
  locked: boolean;
  profile_id: string | null;
  profile_name: string;
  /** locked / baseline / none。 */
  direction: string;
  chapters: StyleDriftItem[];
  drifted_count: number;
}

// --------------------------------------------------------------------------- V0.3 全局不变量
export interface InvariantEvidence {
  source_chapter: string;
  quote?: string | null;
  detail?: string | null;
}

export interface InvariantIssue {
  code: string;
  level: "error" | "warning" | "info";
  subject: string;
  message: string;
  evidence: InvariantEvidence[];
  suggestion: string;
}

export interface InvariantReport {
  novel_id: string;
  report_id?: string | null;
  created_at?: string | null;
  errors: number;
  warnings: number;
  /** code → 条数。 */
  codes: Record<string, number>;
  /** 读最近一次留存报告时后端不带这项，字段会是空对象。 */
  checked: { chapters?: number; facts?: number; characters?: number };
  issues: InvariantIssue[];
}

// --------------------------------------------------------------------------- V0.3 承诺账本
export type CommitmentKind = "APPOINTMENT" | "DEADLINE" | "THREAT" | "PROMISE" | "QUESTION";

export type CommitmentStatus = "OPEN" | "OVERDUE" | "FULFILLED" | "ABANDONED";

export interface CommitmentEvidence {
  source_chapter: string;
  quote?: string | null;
  detail?: string | null;
}

export interface Commitment {
  id: string;
  kind: CommitmentKind;
  who: string;
  counterpart: string;
  what: string;
  quote: string;
  source_chapter: number | null;
  deadline_text: string;
  due_story_time: string | null;
  /** 后端按故事时间算出的实时状态：越界后为 OVERDUE。 */
  status: CommitmentStatus;
  /** 库里存的状态（可能仍是 OPEN，只有显式重跑才会写回）。 */
  stored_status: string;
  fulfilled_chapter: number | null;
  days_remaining: number | null;
  breach_chapter: number | null;
  frontier_chapter: number;
  evidence: CommitmentEvidence[];
}

export interface CommitmentCreate {
  source_chapter?: number;
  kind?: CommitmentKind;
  who?: string;
  counterpart?: string;
  what: string;
  quote?: string;
  deadline_text?: string;
  note?: string;
}

// --------------------------------------------------------------------------- V0.3 修订闭环
export interface RevisionRound {
  round: number;
  /** draft / revised / no-change。 */
  stage: string;
  score: number;
  word_count: number;
  /** 这一轮的「声音保留分」；没有作者声音画像时为 null。 */
  voice_score?: number | null;
  style_codes: string[];
  continuity_codes: string[];
}

export interface RevisionMetricDelta {
  before: number;
  after: number;
  delta: number;
  /** null 表示该指标只应落在区间内，无法判定好坏。 */
  better: boolean | null;
}

export interface RevisionMetricDeltas {
  deltas: Record<string, RevisionMetricDelta>;
  improved: number;
  worsened: number;
}

export interface RevisionRequest {
  goals: string;
  must_include?: string[];
  forbidden?: string[];
  characters?: string[];
  chapter_number?: number;
  title?: string;
  story_time?: string;
  location?: string;
  target_words?: number;
  provider?: string;
  save?: boolean;
  plan_id?: string;
  /** 0-4，默认 2。 */
  max_rounds?: number;
  /** 默认 85。 */
  target_score?: number;
  /** 默认 true。 */
  use_model_critic?: boolean;
}

export interface RevisionResponse {
  draft: ChapterDraft;
  rounds: RevisionRound[];
  accepted: boolean;
  final_score: number;
  metric_deltas: RevisionMetricDeltas;
  retrieved: RetrievalBundle;
  provider: string;
  model: string;
  warnings: string[];
  saved_chapter_id: string | null;
  generation_id: string | null;
  goal: string;
  plan_id: string | null;
}

// --------------------------------------------------------------------------- V0.4 想法碎片
/** 碎片类型：奇思妙想 / 画面 / 想写的句子 / 场景 / 主题看法 / 人物瞬间 / 桥段机制 / 其他。 */
export type FragmentKind =
  | "WHIM"
  | "IMAGE"
  | "LINE"
  | "SCENE"
  | "THEME"
  | "CHARACTER"
  | "MECHANIC"
  | "OTHER";

export type FragmentStatus = "INBOX" | "PLACED" | "REALIZED" | "ARCHIVED";

/** 成文时这条碎片被怎么用：QUOTED = 作者原话被原样保留。 */
export type FragmentTreatment = "" | "QUOTED" | "PARAPHRASED" | "EXPANDED" | "BACKGROUND";

/** 作者的一条想法碎片：允许不完整、可以很碎。 */
export interface Fragment {
  id: string;
  novel_id: string;
  kind: FragmentKind;
  title: string;
  text: string;
  /** 作者自己写的意图（这一条想表达什么）。 */
  intent: string;
  tags: string[];
  related_characters: string[];
  target_chapter: number | null;
  status: FragmentStatus;
  priority: number;
  /** USER = 作者手记；PROMPT = 系统提问后写下的回答。 */
  origin: string;
  prompted_by: string;
  realized_chapter_id: string | null;
  realized_excerpt: string;
  realized_treatment: FragmentTreatment;
  notes: string;
  created_at: string;
  updated_at: string;
}

export interface FragmentCreate {
  kind?: FragmentKind;
  title?: string;
  text: string;
  intent?: string;
  tags?: string[];
  related_characters?: string[];
  target_chapter?: number | null;
  status?: FragmentStatus;
  priority?: number;
  origin?: string;
  prompted_by?: string;
  notes?: string;
}

/** 碎片的部分更新（PATCH 只传要改的字段）。 */
export type FragmentUpdate = Partial<Omit<FragmentCreate, "text">> & { text?: string };

export interface FragmentQuery {
  status?: FragmentStatus;
  kind?: FragmentKind;
  character?: string;
  targetChapter?: number;
  unplacedOnly?: boolean;
  limit?: number;
}

export interface FragmentStats {
  total: number;
  by_status: Record<string, number>;
  by_kind: Record<string, number>;
  inbox: number;
  unplaced: number;
  realized: number;
  realization_rate: number;
}

/** 碎片 → 正文的一条对应关系（成文后给作者核对）。 */
export interface FragmentPassage {
  /** 碎片 id；`__bridge__` 表示过渡段落，不属于任何碎片。 */
  fragment_id: string;
  prose: string;
  /** 被原样沿用的那句原话；没有则为空。 */
  uses_quote: string;
  treatment: Exclude<FragmentTreatment, "">;
}

export interface UndevelopedFragment {
  fragment_id: string;
  reason: string;
}

/** 成文里出现、但 Canon 未记录的设定性陈述。 */
export interface InventedClaim {
  subject: string;
  predicate: string;
  object: string;
}

export interface FragmentCoverage {
  fragments_total: number;
  fragments_used: number;
  used_ids: string[];
  unused_ids: string[];
  ratio: number;
}

export interface FragmentRealization {
  title: string;
  content: string;
  word_count: number;
  passages: FragmentPassage[];
  undeveloped: UndevelopedFragment[];
  coverage: FragmentCoverage;
  invented_claims: InventedClaim[];
  notes: string;
  saved_chapter_id: string | null;
  chapter_number: number | null;
}

export interface FragmentRealizeRequest {
  fragment_ids?: string[];
  raw_fragments?: string[];
  goals?: string;
  tone?: string;
  must_keep?: string[];
  forbidden?: string[];
  characters?: string[];
  chapter_number?: number;
  target_words?: number;
  provider?: string;
  save?: boolean;
  revise?: boolean;
  /** 0-3，默认 1。 */
  max_rounds?: number;
  /** 默认 85。 */
  target_score?: number;
  use_model_critic?: boolean;
}

/** 成文后的文风体检；未走改稿闭环时只有 final_score / issues / metrics。 */
export interface RealizeStyleReport {
  final_score: number;
  /** null 表示没有走改稿闭环。 */
  accepted?: boolean | null;
  rounds?: RevisionRound[];
  metric_deltas?: RevisionMetricDeltas;
  issues?: StyleIssue[];
  metrics?: Record<string, any>;
}

/** 声音保留分：这段文字有多像作者本人（越高越好）。 */
export interface VoiceReport {
  score: number | null;
  available: boolean;
  signature_hits: string[];
  signature_expected?: number;
  profile_terms?: string[];
  components: Record<string, number>;
}

export interface FragmentRealizeResponse {
  realization: FragmentRealization;
  provider: string;
  model: string;
  warnings: string[];
  style: Partial<RealizeStyleReport>;
  /** 没有作者声音画像时后端返回空对象。 */
  voice: Partial<VoiceReport>;
  fragments_updated: number;
}

export interface EmotionPromptRequest {
  goals?: string;
  characters?: string[];
  chapter_number?: number;
  provider?: string;
}

/** 情感引导：系统只提问，答案由作者写。 */
export interface EmotionPromptResponse {
  questions: string[];
  provider: string;
  model: string;
  warnings: string[];
}

/** 作者声音画像：他惯用的词、标点与断句习惯。 */
export interface VoiceProfile {
  available: boolean;
  name: string;
  sample_count: number;
  total_chars: number;
  signature_terms: string[];
  /** 标点 → 占比。 */
  punctuation: Record<string, number>;
  created_at: string | null;
}

export interface VoiceProfileRequest {
  name?: string;
  chapter_numbers?: number[];
  texts?: string[];
}

export interface ChapterIntent {
  chapter_number: number;
  fragments: Fragment[];
  intents: string[];
  count: number;
}

// --------------------------------------------------------------------------- V0.5 设定断言核对
/** 一条判定的依据：Canon 事实或世界观规则。 */
export interface ClaimEvidence {
  kind: string;
  text: string;
  source_chapter: number | null;
  fact_id?: string | null;
  rule_type?: string;
}

/** 正文里的一条设定断言与它的核对结论。 */
export interface ClaimVerdict {
  claim: {
    subject: string;
    predicate: string;
    object: string;
    kind?: string;
    /** 正文里的原句（必须能逐字对上正文）。 */
    quote?: string;
    /** deterministic = 确定性规则抽出；model = 模型抽出。 */
    origin?: string;
  };
  verdict: "SUPPORTED" | "UNVERIFIED" | "CONFLICT";
  reason: string;
  evidence: ClaimEvidence[];
}

/** 一次断言核对的报告：只出结论，绝不写 Canon。 */
export interface ClaimReport {
  id?: string | null;
  novel_id: string;
  chapter_id?: string | null;
  chapter_number?: number | null;
  /** DRAFT（贴的草稿）或 CHAPTER（某一章正文）。 */
  label: string;
  as_of_chapter?: number | null;
  claim_count: number;
  supported_count: number;
  unverified_count: number;
  conflict_count: number;
  claims: ClaimVerdict[];
  conflicts: ClaimVerdict[];
  unverified: ClaimVerdict[];
  supported: ClaimVerdict[];
  provider: string;
  model: string;
  warnings: string[];
  summary: string;
  created_at?: string | null;
}

// --------------------------------------------------------------------------- 写作规则知识库
/** 一条写作规则：编号、一句话说法、出处（平台课程原文）、可执行的做法与详细复述。 */
export interface CraftRule {
  /** 规则编号，如 FIRST_PAGE。 */
  code: string;
  summary: string;
  /** 出处，如「《如何稳定剧情，让读者追更不停？》· 番茄作家课堂」。 */
  source: string;
  advice: string;
  /** 原文要点的详细复述。 */
  detail: string;
}

// --------------------------------------------------------------------------- V0.7 发布前准备
/** 一条发布前检查项；level=error 的是阻断项，归零才算可以发。 */
export interface PublishCheckItem {
  code: string;
  level: "error" | "warning" | "info" | "ok";
  message: string;
  fix: string;
  /** 这一条依据的规则编号（写作规则知识库的 code）；没有依据时后端不带这项。 */
  rule?: string;
  /** 命中的原文片段（风险词与格式问题会带）。 */
  excerpt?: string;
  /** 度量值（字数、钩子分等）。 */
  value?: number;
  /** 风险词所属分类（只有 RISK_WORD 带）。 */
  category?: string;
  /** 命中的风险词（只有 RISK_WORD 带）。 */
  word?: string;
}

/** 命中的审核风险词：分类 + 词 + 上下文 + 处置建议。 */
export interface PublishRisk {
  category: string;
  word: string;
  quote: string;
  advice: string;
}

/** 平台点名的四条低质规则的命中情况。 */
export interface PublishRule {
  rule: string;
  hit: boolean;
  /** 命中的文风指标 code（多个用「、」连接）；未命中为空串。 */
  evidence: string;
  advice: string;
}

/** 一次发布前检查的清单：贴的草稿或某一章正文。 */
export interface PublishCheck {
  novel_id: string;
  novel_title: string;
  chapter_id?: string | null;
  chapter_number?: number | null;
  title: string;
  word_count: number;
  platform: string;
  words_per_chapter: number[];
  daily_words_targets: number[];
  checks: PublishCheckItem[];
  risks: PublishRisk[];
  format_issues: PublishCheckItem[];
  platform_rules: PublishRule[];
  /** level=error 的条数。 */
  blocking: number;
  warnings: number;
  ready: boolean;
}

// --------------------------------------------------------------------------- V0.7 结构视图
/** 结构视图里的一章：正文层的节奏信号 + 跨章的结构事件（伏笔首现/推进、承诺到期）。 */
export interface StructureChapter {
  chapter_id: string;
  chapter_number: number;
  title: string;
  word_count: number;
  hook_score: number;
  advancement_per_1k: number;
  filler_paragraph_ratio: number;
  conflict_per_1k: number;
  dialogue_ratio: number;
  continuity_errors: number;
  continuity_warnings: number;
  claim_conflicts: number;
  foreshadowing_opened: string[];
  foreshadowing_advanced: string[];
  commitments_due: string[];
  /** 本章不达标的原因；空数组表示没问题。 */
  verdicts: string[];
}

/** 连续不达标的章节区间：单章弱可以忍，连着弱是读者会走的地方。 */
export interface StructureWeakRun {
  start_chapter: number;
  end_chapter: number;
  length: number;
  reasons: string[];
}

/** 每 PACE_WINDOW（5）章一个节奏窗口，看这一段整体有没有「小高潮」。 */
export interface StructurePaceWindow {
  start_chapter: number;
  end_chapter: number;
  avg_hook: number;
  avg_advancement: number;
  avg_conflict: number;
  words: number;
  peak_chapter: number;
  verdict: string;
}

export interface StructureSummary {
  weak_chapters: number;
  weakest_run: StructureWeakRun | null;
  weakest_window: StructurePaceWindow | null;
}

/** 全书结构视图：逐章信号 + 连续弱区 + 每 5 章的节奏窗口。 */
export interface StructureView {
  novel_id: string;
  title: string;
  chapter_count: number;
  word_count: number;
  target_word_count: number;
  /**
   * 阈值：hook_floor / advancement_floor / filler_ceiling / pace_window，
   * 以及冲突信号上下限 conflict_ceiling（一直紧）/ conflict_floor（一直松）。
   */
  floors: Record<string, number>;
  chapters: StructureChapter[];
  weak_runs: StructureWeakRun[];
  pace_windows: StructurePaceWindow[];
  summary: StructureSummary;
}

// --------------------------------------------------------------------------- 读者数据回环
/** 逐章对照里的一章：平台数据 + 我们的判定信号。 */
export interface ReaderAnalysisChapter {
  chapter_number: number;
  title: string;
  word_count: number;
  /** 没导入这一章的数据时为 null。 */
  reads: number | null;
  completion_rate: number | null;
  retention_rate: number | null;
  hook_score: number;
  advancement_per_1k: number;
  filler_paragraph_ratio: number;
  weak: boolean;
  /** 不达标的原因；空数组表示我们没报警。 */
  weak_reasons: string[];
}

/** 漏报：我们没报警，完读率却明显低于全书平均。 */
export interface ReaderMissedChapter {
  chapter_number: number;
  title: string;
  completion_rate: number;
  /** 与全书平均完读率的差（百分点，负值表示更低）。 */
  gap_vs_book: number;
}

/** 误报：我们报警了，但读者没跑。 */
export interface ReaderFalseAlarm {
  chapter_number: number;
  title: string;
  completion_rate: number;
  reasons: string[];
}

/** 阅读人数相对上一章下降的章。 */
export interface ReaderDropChapter {
  chapter_number: number;
  title: string;
  reads: number;
  from_chapter: number;
  /** 相对上一章的人数变化（-0.25 表示少了四分之一）。 */
  change: number;
}

/** 平台数据与规则判定的对照结果。 */
export interface ReaderAnalysis {
  novel_id: string;
  title: string;
  chapters: ReaderAnalysisChapter[];
  coverage: { with_data: number; total: number };
  /** 下面三个都是 0~1 的比例。 */
  book_completion_rate: number;
  weak_mean_completion: number;
  ok_mean_completion: number;
  /** 一句话结论：我们的判定与读者的实际表现是否相符。 */
  verdict: string;
  missed: ReaderMissedChapter[];
  false_alarms: ReaderFalseAlarm[];
  drop_chapters: ReaderDropChapter[];
  suggestions: string[];
}

/** 导入请求：text 是从平台后台复制的表格。 */
export interface ReaderImportBody {
  text: string;
  note?: string;
}

export interface ReaderImportResult {
  imported: number;
  rows: { chapter_number: number; reads: number; completion_rate: number }[];
  message: string;
}

/** 已导入的原始行，用来核对是不是抄错了列。 */
export interface ReaderMetricRaw {
  chapter_number: number;
  reads: number;
  completion_rate: number;
  retention_rate: number | null;
  revenue: number | null;
  comments: number;
  /** 粘贴时的原始文本在 raw.line。 */
  raw: { line?: string };
  recorded_at: string | null;
}

// --------------------------------------------------------------------------- V0.9 Obsidian 对接
/** 探测到的一个本地笔记库；id 用来做列表 key。 */
export interface DetectedVault {
  id: string;
  path: string;
  exists: boolean;
  /** 是否是 Obsidian 当前打开的那个库。 */
  open: boolean;
}

/** 对接状态；GET /api/obsidian 与 POST /api/obsidian 返回同一结构。 */
export interface ObsidianStatus {
  connected: boolean;
  vault: string;
  inbox: string;
  inbox_exists: boolean;
  inbox_notes: number;
  export_dir: string;
  export_exists: boolean;
  total_notes: number;
  /** 之前导出过、并在笔记库里登记的篇数。 */
  exported: number;
  /** 探测到的库列表；没装 Obsidian 时是空数组。 */
  detected: DetectedVault[];
  /** 可直接展示给作者的提示，可能是「收件箱不存在」这类可执行建议。 */
  message: string;
}

/** 改对接配置；只传要改的字段，其余保持不变。 */
export interface ObsidianConfigBody {
  vault?: string;
  inbox?: string;
  export_dir?: string;
  tags?: string[];
}

/** 从笔记库收件箱导入素材。 */
export interface ObsidianImportBody {
  vault?: string;
  inbox?: string;
  tags?: string[];
  limit?: number;
}

export interface ObsidianImportResult {
  imported: number;
  updated: number;
  skipped: number;
  /** 本次涉及的笔记文件路径。 */
  notes: string[];
  message: string;
}

/** 把设定库导出成带双链的笔记；subdir 为空时用已保存的导出目录。 */
export interface ObsidianExportBody {
  vault?: string;
  subdir?: string;
}

export interface ObsidianExportResult {
  written: number;
  conflicts: number;
  files: string[];
  /** 作者在库里改过、未被覆盖而是另存的 .conflict.md。 */
  conflict_files: string[];
  directory: string;
  message: string;
}
