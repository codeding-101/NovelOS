import type {
  ApplyResult,
  AsOfState,
  AskMode,
  AskResponse,
  Bible,
  CanonFact,
  Chapter,
  ChapterIntent,
  ChapterPlan,
  ChapterSummary,
  CharacterState,
  ClaimReport,
  Commitment,
  CommitmentCreate,
  CommitmentStatus,
  CompletionReport,
  ContinuityReport,
  CraftRule,
  Dashboard,
  EmbeddingStats,
  EmotionPromptRequest,
  EmotionPromptResponse,
  ExtractionRun,
  ForeshadowingPlan,
  Fragment,
  FragmentCreate,
  FragmentQuery,
  FragmentRealizeRequest,
  FragmentRealizeResponse,
  FragmentStats,
  FragmentUpdate,
  InvariantReport,
  Novel,
  NovelStats,
  PlanGenerateResponse,
  ProviderInfo,
  PublishCheck,
  ReaderAnalysis,
  ReaderImportBody,
  ReaderImportResult,
  ReaderMetricRaw,
  ReindexResult,
  RetrievalHit,
  RetrievalResult,
  RevisionRequest,
  RevisionResponse,
  SearchHit,
  StructureView,
  StyleBaselineRequest,
  StyleDriftReport,
  StyleProfile,
  StyleReview,
  StyleReviewSummary,
  SweepPayload,
  SweepRun,
  VoiceProfile,
  VoiceProfileRequest,
  WriteChapterResponse,
} from "./types";

const BASE = "/api";

async function requestRaw(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* 保留默认错误信息 */
    }
    throw new Error(detail);
  }
  return response;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await requestRaw(path, init);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const json = (body: unknown) => ({ body: JSON.stringify(body) });

/** 拼查询串，自动跳过空值，避免把 undefined 传给后端。 */
function query(params: Record<string, string | number | boolean | undefined | null>): string {
  return Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => `${key}=${encodeURIComponent(String(value))}`)
    .join("&");
}

/** 从 Content-Disposition（可能带 RFC 5987 的 filename*）里取附件名；取不到返回 null。 */
function attachmentFilename(header: string | null): string | null {
  if (!header) return null;
  const extended = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      return extended[1].trim();
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : null;
}

export const api = {
  health: () => request<{ status: string; search_engine: string; providers: ProviderInfo[] }>("/health"),
  providers: () => request<ProviderInfo[]>("/ai/providers"),
  tools: () => request<{ name: string; description: string }[]>("/ai/tools"),

  listNovels: () => request<Novel[]>("/novels"),
  createNovel: (payload: Partial<Novel>) =>
    request<Novel>("/novels", { method: "POST", ...json(payload) }),
  getNovel: (id: string) => request<Novel>(`/novels/${id}`),
  updateNovel: (id: string, payload: Partial<Novel>) =>
    request<Novel>(`/novels/${id}`, { method: "PATCH", ...json(payload) }),
  stats: (id: string) => request<NovelStats>(`/novels/${id}/stats`),
  bible: (id: string) => request<Bible>(`/novels/${id}/bible`),
  seed: (id: string, reset = false) =>
    request<Record<string, any>>(`/novels/${id}/seed?reset=${reset}`, { method: "POST" }),

  listChapters: async (
    novelId: string,
    offset = 0,
    limit = 100,
  ): Promise<{ items: ChapterSummary[]; total: number }> => {
    const response = await requestRaw(`/novels/${novelId}/chapters?offset=${offset}&limit=${limit}`);
    const items = (await response.json()) as ChapterSummary[];
    const header = response.headers.get("X-Total-Count");
    const parsed = header === null ? NaN : Number(header);
    return { items, total: Number.isFinite(parsed) ? parsed : items.length };
  },
  createChapter: (novelId: string, payload: Record<string, unknown>) =>
    request<Chapter>(`/novels/${novelId}/chapters`, { method: "POST", ...json(payload) }),
  getChapter: (chapterId: string) => request<Chapter>(`/chapters/${chapterId}`),
  saveChapter: (chapterId: string, payload: Record<string, unknown>) =>
    request<Chapter>(`/chapters/${chapterId}`, { method: "PUT", ...json(payload) }),
  deleteChapter: (chapterId: string) =>
    request<void>(`/chapters/${chapterId}`, { method: "DELETE" }),
  search: (novelId: string, searchQuery: string) =>
    request<{ query: string; engine: string; hits: SearchHit[] }>(
      `/novels/${novelId}/search?q=${encodeURIComponent(searchQuery)}`,
    ),

  complete: (chapterId: string, provider: string) =>
    request<CompletionReport>(`/chapters/${chapterId}/complete?provider=${provider}`, {
      method: "POST",
    }),
  extract: (chapterId: string, provider: string) =>
    request<ExtractionRun>(`/chapters/${chapterId}/extract?provider=${provider}`, {
      method: "POST",
    }),
  continuity: (chapterId: string, provider: string) =>
    request<ContinuityReport>(`/chapters/${chapterId}/continuity?provider=${provider}`, {
      method: "POST",
    }),
  latestContinuity: (chapterId: string) =>
    request<ContinuityReport | null>(`/chapters/${chapterId}/continuity`),
  runs: (novelId: string, chapterId?: string) =>
    request<ExtractionRun[]>(
      `/novels/${novelId}/extraction-runs${chapterId ? `?chapter_id=${chapterId}` : ""}`,
    ),
  reviewItem: (itemId: string, reviewStatus: "ACCEPTED" | "REJECTED") =>
    request<{ item_id: string; review_status: string }>(`/extraction-items/${itemId}/review`, {
      method: "POST",
      ...json({ review_status: reviewStatus }),
    }),
  applyRun: (runId: string, acceptPending = false) =>
    request<ApplyResult>(`/extraction-runs/${runId}/apply`, {
      method: "POST",
      ...json({ accept_pending: acceptPending }),
    }),
  confirmFact: (factId: string) =>
    request<Record<string, any>>(`/canon-facts/${factId}/confirm`, { method: "POST", ...json({}) }),
  rejectFact: (factId: string) =>
    request<Record<string, any>>(`/canon-facts/${factId}/reject`, { method: "POST", ...json({}) }),
  canonFacts: (novelId: string, status?: string) =>
    request<CanonFact[]>(`/novels/${novelId}/canon-facts${status ? `?status=${status}` : ""}`),
  characterStates: (characterId: string) =>
    request<CharacterState[]>(`/characters/${characterId}/states`),

  ask: (novelId: string, question: string, provider: string, mode: AskMode = "simple") =>
    request<AskResponse>(`/novels/${novelId}/ai/ask?provider=${provider}`, {
      method: "POST",
      ...json({ question, mode }),
    }),
  writeChapter: (novelId: string, payload: Record<string, unknown>, provider: string) =>
    request<WriteChapterResponse>(`/novels/${novelId}/ai/write-chapter?provider=${provider}`, {
      method: "POST",
      ...json(payload),
    }),

  // ------------------------------------------------------------------ 时点视图与总览
  asOfState: (novelId: string, chapter: number) =>
    request<AsOfState>(`/novels/${novelId}/state?${query({ chapter })}`),
  dashboard: (novelId: string) => request<Dashboard>(`/novels/${novelId}/dashboard`),
  sweep: (novelId: string, payload: SweepPayload) =>
    request<SweepRun>(`/novels/${novelId}/sweep`, { method: "POST", ...json(payload) }),
  sweeps: (novelId: string, limit = 10) =>
    request<SweepRun[]>(`/novels/${novelId}/sweeps?${query({ limit })}`),

  // ------------------------------------------------------------------ 混合检索与向量索引
  retrieval: (
    novelId: string,
    params: { q: string; limit?: number; useVector?: boolean; refType?: string },
  ) =>
    request<RetrievalResult>(
      `/novels/${novelId}/retrieval?${query({
        q: params.q,
        limit: params.limit ?? 8,
        use_vector: params.useVector ?? true,
        ref_type: params.refType,
      })}`,
    ),
  vectors: (novelId: string) => request<EmbeddingStats>(`/novels/${novelId}/vectors`),
  reindexVectors: (novelId: string, provider = "local") =>
    request<ReindexResult>(`/novels/${novelId}/vectors/reindex?${query({ provider })}`, {
      method: "POST",
    }),

  // ------------------------------------------------------------------ 伏笔规划
  foreshadowingPlan: (novelId: string, horizon = 5, gap = 8) =>
    request<ForeshadowingPlan>(
      `/novels/${novelId}/foreshadowing-plan?${query({ horizon, gap })}`,
    ),

  // ------------------------------------------------------------------ 章节规划
  listPlans: (novelId: string, status?: string) =>
    request<ChapterPlan[]>(`/novels/${novelId}/plans?${query({ status })}`),
  generatePlans: (
    novelId: string,
    payload: {
      from_chapter?: number;
      count: number;
      steer?: string;
      provider?: string;
      overwrite?: boolean;
    },
  ) =>
    request<PlanGenerateResponse>(`/novels/${novelId}/plans/generate`, {
      method: "POST",
      ...json(payload),
    }),
  createPlan: (novelId: string, payload: Record<string, unknown>) =>
    request<ChapterPlan>(`/novels/${novelId}/plans`, { method: "POST", ...json(payload) }),
  deletePlan: (planId: string) => request<void>(`/plans/${planId}`, { method: "DELETE" }),
  writeFromPlan: (
    planId: string,
    params: { provider?: string; targetWords?: number; save?: boolean } = {},
  ) =>
    request<WriteChapterResponse>(
      `/plans/${planId}/write?${query({
        provider: params.provider,
        target_words: params.targetWords ?? 1500,
        save: params.save ?? false,
      })}`,
      { method: "POST" },
    ),

  // ------------------------------------------------------------------ V0.3 文风
  styleBaseline: (novelId: string) =>
    request<StyleProfile | null>(`/novels/${novelId}/style/baseline`),
  /** 不传 chapter_numbers / texts 时后端用本书已完成章节；样本不足返回 409。 */
  buildStyleBaseline: (novelId: string, payload: StyleBaselineRequest = {}) =>
    request<StyleProfile>(`/novels/${novelId}/style/baseline`, {
      method: "POST",
      ...json(payload),
    }),
  reviewChapterStyle: (
    chapterId: string,
    params: { useModel?: boolean; persist?: boolean } = {},
  ) =>
    request<StyleReview>(
      `/chapters/${chapterId}/style-review?${query({
        use_model: params.useModel ?? true,
        persist: params.persist ?? true,
      })}`,
      { method: "POST" },
    ),
  reviewTextStyle: (
    novelId: string,
    payload: { text: string; use_model?: boolean; chapter_number?: number },
  ) =>
    request<StyleReview>(`/novels/${novelId}/style/review-text`, {
      method: "POST",
      ...json({ use_model: true, ...payload }),
    }),
  listStyleReviews: (
    novelId: string,
    params: { chapterNumber?: number; limit?: number } = {},
  ) =>
    request<StyleReviewSummary[]>(
      `/novels/${novelId}/style/reviews?${query({
        chapter_number: params.chapterNumber,
        limit: params.limit ?? 20,
      })}`,
    ),

  // ------------------------------------------------------------------ V0.3 全局不变量
  invariants: (novelId: string, run = false) =>
    request<InvariantReport>(`/novels/${novelId}/invariants?${query({ run })}`),
  runInvariants: (novelId: string) =>
    request<InvariantReport>(`/novels/${novelId}/invariants/run`, { method: "POST" }),

  // ------------------------------------------------------------------ V0.3 承诺账本
  commitments: (novelId: string, status?: CommitmentStatus) =>
    request<Commitment[]>(`/novels/${novelId}/commitments?${query({ status })}`),
  createCommitment: (novelId: string, payload: CommitmentCreate) =>
    request<Commitment>(`/novels/${novelId}/commitments`, { method: "POST", ...json(payload) }),
  fulfillCommitment: (
    commitmentId: string,
    payload: { note?: string; chapter_number?: number } = {},
  ) =>
    request<Commitment>(`/commitments/${commitmentId}/fulfill`, {
      method: "POST",
      ...json(payload),
    }),
  abandonCommitment: (
    commitmentId: string,
    payload: { note?: string; chapter_number?: number } = {},
  ) =>
    request<Commitment>(`/commitments/${commitmentId}/abandon`, {
      method: "POST",
      ...json(payload),
    }),

  // ------------------------------------------------------------------ V0.3 修订闭环
  reviseChapter: (novelId: string, payload: RevisionRequest, provider?: string) =>
    request<RevisionResponse>(
      `/novels/${novelId}/ai/revise-chapter?${query({ provider })}`,
      { method: "POST", ...json(payload) },
    ),

  // ------------------------------------------------------------------ V0.4 想法碎片
  fragments: (novelId: string, params: FragmentQuery = {}) =>
    request<Fragment[]>(
      `/novels/${novelId}/fragments?${query({
        status: params.status,
        kind: params.kind,
        character: params.character,
        target_chapter: params.targetChapter,
        unplaced_only: params.unplacedOnly ? true : undefined,
        limit: params.limit ?? 200,
      })}`,
    ),
  createFragment: (novelId: string, payload: FragmentCreate) =>
    request<Fragment>(`/novels/${novelId}/fragments`, { method: "POST", ...json(payload) }),
  createFragmentsBulk: (novelId: string, payload: FragmentCreate[]) =>
    request<Fragment[]>(`/novels/${novelId}/fragments/bulk`, {
      method: "POST",
      ...json(payload),
    }),
  updateFragment: (fragmentId: string, payload: FragmentUpdate) =>
    request<Fragment>(`/fragments/${fragmentId}`, { method: "PATCH", ...json(payload) }),
  deleteFragment: (fragmentId: string) =>
    request<void>(`/fragments/${fragmentId}`, { method: "DELETE" }),
  placeFragment: (fragmentId: string, chapterNumber: number) =>
    request<Fragment>(`/fragments/${fragmentId}/place`, {
      method: "POST",
      ...json({ chapter_number: chapterNumber }),
    }),
  fragmentStats: (novelId: string) =>
    request<FragmentStats>(`/novels/${novelId}/fragments/stats`),
  /** 按语义/关键词召回碎片，结构与混合检索的 hits 一致。 */
  relevantFragments: (novelId: string, q: string, limit = 5) =>
    request<RetrievalHit[]>(
      `/novels/${novelId}/fragments/relevant?${query({ q, limit })}`,
    ),
  /** 某章安排好的碎片与作者写的意图。 */
  chapterIntent: (novelId: string, chapterNumber: number) =>
    request<ChapterIntent>(`/novels/${novelId}/fragments/intent/${chapterNumber}`),
  realizeFragments: (
    novelId: string,
    payload: FragmentRealizeRequest,
    provider?: string,
  ) =>
    request<FragmentRealizeResponse>(
      `/novels/${novelId}/fragments/realize?${query({ provider })}`,
      { method: "POST", ...json(payload) },
    ),
  /** 情感引导：只提问，作者的回答再存成碎片。 */
  emotionPrompts: (novelId: string, payload: EmotionPromptRequest, provider?: string) =>
    request<EmotionPromptResponse>(
      `/novels/${novelId}/fragments/prompts?${query({ provider })}`,
      { method: "POST", ...json(payload) },
    ),
  voiceProfile: (novelId: string) =>
    request<VoiceProfile>(`/novels/${novelId}/style/voice`),
  /** 样本不足（少于 2 段 20 字以上的文本 / 3 章正文）时后端返回 409。 */
  buildVoiceProfile: (novelId: string, payload: VoiceProfileRequest = {}) =>
    request<VoiceProfile>(`/novels/${novelId}/style/voice`, {
      method: "POST",
      ...json(payload),
    }),

  // ------------------------------------------------------------------ V0.5 设定断言核对
  /** 核对一段草稿或某一章正文里的设定断言；text 与 chapter_number 至少给一个。 */
  verifyClaims: (
    novelId: string,
    body: {
      text?: string;
      chapter_number?: number;
      use_model?: boolean;
      persist?: boolean;
      as_of_chapter?: number;
    },
  ) =>
    request<ClaimReport>(`/novels/${novelId}/claims/verify`, {
      method: "POST",
      ...json({ use_model: true, persist: true, ...body }),
    }),
  listClaimReports: (novelId: string, params: { chapter_number?: number; limit?: number } = {}) =>
    request<ClaimReport[]>(
      `/novels/${novelId}/claims?${query({
        chapter_number: params.chapter_number,
        limit: params.limit ?? 20,
      })}`,
    ),

  // ------------------------------------------------------------------ V0.5 文风锁定与漂移
  lockStyleBaseline: (novelId: string, profileId: string, locked: boolean) =>
    request<StyleProfile>(`/novels/${novelId}/style/baseline/${profileId}/lock`, {
      method: "POST",
      ...json({ locked }),
    }),
  styleDrift: (novelId: string) => request<StyleDriftReport>(`/novels/${novelId}/style/drift`),

  // ------------------------------------------------------------------ V0.7 发布前检查与导出
  /** 检查已入库的某一章（用章节 id）。 */
  publishCheckChapter: (chapterId: string) =>
    request<PublishCheck>(`/chapters/${chapterId}/publish-check`, { method: "POST" }),
  /** 检查一段草稿，或只给 chapter_number 让后端去查那一章；text 与 chapter_number 至少给一个。 */
  publishCheckText: (
    novelId: string,
    body: {
      text?: string;
      chapter_number?: number;
      title?: string;
      target_words_min?: number;
      target_words_max?: number;
    },
  ) =>
    request<PublishCheck>(`/novels/${novelId}/publish-check`, {
      method: "POST",
      ...json(body),
    }),
  /**
   * 导出正文：响应体是纯文本（不是 JSON），所以只借用 requestRaw 拿响应本身，
   * 再按 Content-Disposition 解析附件名，解析不到就自己拼一个。
   */
  exportNovelText: async (
    novelId: string,
    params: { fmt?: "txt" | "md"; fromChapter?: number; toChapter?: number } = {},
  ): Promise<{ text: string; filename: string }> => {
    const fmt = params.fmt ?? "txt";
    const search = query({
      fmt,
      from_chapter: params.fromChapter,
      to_chapter: params.toChapter,
    });
    const response = await requestRaw(`/novels/${novelId}/export?${search}`);
    const text = await response.text();
    const filename =
      attachmentFilename(response.headers.get("Content-Disposition")) || `${novelId}.${fmt}`;
    return { text, filename };
  },

  // ------------------------------------------------------------------ V0.7 结构视图
  /** 全书结构视图：逐章信号 + 连续弱区 + 每 5 章的节奏窗口（只读，不落库）。 */
  structure: (novelId: string) => request<StructureView>(`/novels/${novelId}/structure`),

  // ------------------------------------------------------------------ 读者数据回环
  /** 导入平台后台粘出来的章节数据；一行都认不出来时后端返回 400。 */
  importReaderMetrics: (novelId: string, body: ReaderImportBody) =>
    request<ReaderImportResult>(`/novels/${novelId}/reader-metrics`, {
      method: "POST",
      ...json(body),
    }),
  readerAnalysis: (novelId: string) =>
    request<ReaderAnalysis>(`/novels/${novelId}/reader-metrics`),
  /** 已导入的原始行（含 raw.line），用来核对是不是抄错了列。 */
  readerRaw: (novelId: string) =>
    request<ReaderMetricRaw[]>(`/novels/${novelId}/reader-metrics/raw`),
  /** 不带 chapterNumber 时清掉整本。 */
  clearReaderMetrics: (novelId: string, chapterNumber?: number) =>
    request<{ deleted: number }>(
      `/novels/${novelId}/reader-metrics${
        chapterNumber === undefined ? "" : `?${query({ chapter_number: chapterNumber })}`
      }`,
      { method: "DELETE" },
    ),

  // ------------------------------------------------------------------ 写作规则知识库
  /** 全部写作规则与出处；路径不在 /novels/{id} 下。 */
  craftRules: () => request<CraftRule[]>("/craft-rules"),
};
