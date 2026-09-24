"use client";

import { useCallback, useEffect, useState } from "react";
import { AIPanel } from "@/components/AIPanel";
import { ChapterPanel } from "@/components/ChapterPanel";
import { EditorPanel, type DraftState } from "@/components/EditorPanel";
import { KnowledgePanel } from "@/components/KnowledgePanel";
import { api } from "@/lib/api";
import type {
  Bible,
  Chapter,
  ChapterSummary,
  CompletionReport,
  ContinuityReport,
  Dashboard,
  Novel,
  NovelStats,
  ProviderInfo,
} from "@/lib/types";

const PROVIDER_LABELS: Record<string, string> = {
  deepseek: "DeepSeek Flash（真实模型）",
  offline: "离线规则提供者（无需密钥）",
};

/** 章节列表每页条数。 */
const CHAPTER_PAGE_SIZE = 100;

export default function Page() {
  const [novels, setNovels] = useState<Novel[]>([]);
  const [novel, setNovel] = useState<Novel | null>(null);
  const [stats, setStats] = useState<NovelStats | null>(null);
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [chapterTotal, setChapterTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [chapter, setChapter] = useState<Chapter | null>(null);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [dirty, setDirty] = useState(false);
  const [bible, setBible] = useState<Bible | null>(null);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [provider, setProvider] = useState<string>("offline");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [report, setReport] = useState<CompletionReport | null>(null);
  const [storedReport, setStoredReport] = useState<ContinuityReport | null>(null);
  const [newTitle, setNewTitle] = useState("");
  /** 碎片被改动过的次数：变化时碎片面板与章节写作里的碎片列表重新拉取。 */
  const [fragmentsVersion, setFragmentsVersion] = useState(0);

  const noteFragmentsChanged = useCallback(() => {
    setFragmentsVersion((current) => current + 1);
  }, []);

  const refreshDashboard = useCallback(async (novelId: string) => {
    try {
      setDashboard(await api.dashboard(novelId));
    } catch {
      setDashboard(null);
    }
  }, []);

  const refreshNovelData = useCallback(
    async (target: Novel | null) => {
      if (!target) return;
      const [nextStats, nextChapters, nextBible, freshNovel] = await Promise.all([
        api.stats(target.id),
        api.listChapters(target.id, 0, CHAPTER_PAGE_SIZE),
        api.bible(target.id),
        api.getNovel(target.id),
      ]);
      setStats(nextStats);
      setChapters(nextChapters.items);
      setChapterTotal(nextChapters.total);
      setBible(nextBible);
      setNovel(freshNovel);
      await refreshDashboard(target.id);
    },
    [refreshDashboard],
  );

  async function loadMoreChapters() {
    if (!novel || loadingMore) return;
    setLoadingMore(true);
    try {
      const next = await api.listChapters(novel.id, chapters.length, CHAPTER_PAGE_SIZE);
      setChapters((current) => {
        const known = new Set(current.map((item) => item.chapter_id));
        return [...current, ...next.items.filter((item) => !known.has(item.chapter_id))];
      });
      setChapterTotal(next.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingMore(false);
    }
  }

  // 初始化：读取提供者列表与已有小说
  useEffect(() => {
    (async () => {
      try {
        const info = await api.health();
        setProviders(info.providers);
        const available = info.providers.find((item) => item.name === "deepseek" && item.available);
        setProvider(available ? "deepseek" : "offline");
      } catch {
        setProvider("offline");
      }
      try {
        const list = await api.listNovels();
        setNovels(list);
        if (list.length > 0) {
          setNovel(list[0]);
          await refreshNovelData(list[0]);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
  }, [refreshNovelData]);

  async function createNovel() {
    if (!newTitle.trim()) {
      setError("请输入书名");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await api.createNovel({ title: newTitle.trim(), target_word_count: 1000000 });
      setNewTitle("");
      setNovels((current) => [created, ...current]);
      setNovel(created);
      await refreshNovelData(created);
      setStatus(`已创建小说《${created.title}》，可点击「装载测试小说」导入示例内容`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function seedTestNovel(reset: boolean) {
    if (!novel) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.seed(novel.id, reset);
      setStatus(
        `已装载测试小说：${result.chapters} 章 / ${result.characters} 人物 / ${result.events} 事件 / ` +
          `${result.canon_facts} 条 Canon 事实 / ${result.foreshadowings} 个伏笔`,
      );
      await refreshNovelData(novel);
      const list = await api.listChapters(novel.id);
      if (list.items.length > 0) await selectChapter(list.items[0].chapter_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function selectChapter(chapterId: string) {
    setBusy(true);
    setError("");
    try {
      const full = await api.getChapter(chapterId);
      setChapter(full);
      setDraft({
        title: full.title,
        content: full.content,
        summary: full.summary,
        story_time: full.story_time ?? "",
        location: full.location ?? "",
      });
      setDirty(false);
      setReport(null);
      try {
        setStoredReport(await api.latestContinuity(full.chapter_id));
      } catch {
        setStoredReport(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const ensureSaved = useCallback(async (): Promise<boolean> => {
    if (!chapter || !draft) return false;
    if (!dirty) return true;
    try {
      await api.saveChapter(chapter.chapter_id, {
        title: draft.title,
        content: draft.content,
        summary: draft.summary,
        story_time: draft.story_time || null,
        location: draft.location || null,
      });
      setDirty(false);
      setStatus("已保存");
      if (novel) await refreshNovelData(novel);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return false;
    }
  }, [chapter, dirty, draft, novel, refreshNovelData]);

  async function newChapter() {
    if (!novel) return;
    setBusy(true);
    try {
      const nextNumber = chapters.length > 0 ? Math.max(...chapters.map((item) => item.chapter_number)) + 1 : 1;
      const created = await api.createChapter(novel.id, { chapter_number: nextNumber, title: "" });
      await refreshNovelData(novel);
      await selectChapter(created.chapter_id);
      setStatus(`已新建第${created.chapter_number}章`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteChapter(chapterId: string) {
    if (!novel) return;
    setBusy(true);
    try {
      await api.deleteChapter(chapterId);
      if (chapter?.chapter_id === chapterId) {
        setChapter(null);
        setDraft(null);
      }
      await refreshNovelData(novel);
      setStatus("章节已删除");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function completeChapter() {
    if (!chapter) return;
    if (!(await ensureSaved())) return;
    setBusy(true);
    try {
      setStatus("正在执行章节完成工作流（第 1—10 步）…");
      const result = await api.complete(chapter.chapter_id, provider);
      setReport(result);
      setStatus(
        `工作流完成：${result.errors} 个错误、${result.warnings} 个警告、${result.pending_items} 条待确认`,
      );
      const full = await api.getChapter(chapter.chapter_id);
      setChapter(full);
      if (novel) await refreshNovelData(novel);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const nextChapterNumber =
    chapters.length > 0 ? Math.max(...chapters.map((item) => item.chapter_number)) + 1 : 1;

  return (
    <div className="app">
      <div className="topbar">
        <h1>NovelOS V0.6</h1>
        <span className="hint">中文长篇小说 AI 辅助创作系统 · 长期记忆与 Canon 一致性</span>
        <span className="spacer" />
        <select value={novel?.id ?? ""} onChange={(event) => {
          const target = novels.find((item) => item.id === event.target.value) ?? null;
          setNovel(target);
          setChapter(null);
          setDraft(null);
          void refreshNovelData(target);
        }}>
          {novels.length === 0 && <option value="">（暂无小说）</option>}
          {novels.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title}
            </option>
          ))}
        </select>
        <select value={provider} onChange={(event) => setProvider(event.target.value)}>
          {(providers.length > 0
            ? providers
            : [{ name: "offline", model: "", kind: "", available: true, supports_tools: false, detail: "" }]
          ).map(
            (item) => (
              <option key={item.name} value={item.name} disabled={!item.available}>
                {PROVIDER_LABELS[item.name] ?? item.name}
                {item.available ? "" : "（不可用）"}
              </option>
            ),
          )}
        </select>
      </div>

      {novel && stats && (
        <div className="topbar" style={{ borderTop: "none" }}>
          <div className="stat-badges">
            <span>
              字数 <b>{stats.word_count}</b> / 目标 <b>{stats.target_word_count}</b>（
              {(stats.progress * 100).toFixed(2)}%）
            </span>
            <span>
              章节 <b>{stats.chapter_count}</b>
            </span>
            <span>
              人物 <b>{stats.character_count}</b>
            </span>
            <span>
              Canon <b>{stats.canon_fact_count}</b>
            </span>
            <span>
              PROPOSED <b>{stats.proposed_fact_count}</b>
            </span>
            <span>
              未回收伏笔 <b>{stats.foreshadowing_open_count}</b>
            </span>
            <span>
              待审条目 <b>{stats.pending_review_items}</b>
            </span>
            <span>
              欠账伏笔{" "}
              <b className={(dashboard?.overdue_foreshadowing ?? 0) > 0 ? "error-text" : ""}>
                {dashboard?.overdue_foreshadowing ?? 0}
              </b>
            </span>
            <span>
              未检查章节{" "}
              <b className={(dashboard?.unchecked_chapters.length ?? 0) > 0 ? "error-text" : ""}>
                {dashboard?.unchecked_chapters.length ?? 0}
              </b>
            </span>
            <span>
              最近审校 <b className={stats.latest_continuity_errors > 0 ? "error-text" : ""}>
                {stats.latest_continuity_errors} 错 / {stats.latest_continuity_warnings} 警
              </b>
            </span>
          </div>
          <span className="spacer" />
          <button onClick={() => void seedTestNovel(false)} disabled={busy}>
            装载测试小说
          </button>
          <button onClick={() => void seedTestNovel(true)} disabled={busy}>
            重置并装载
          </button>
        </div>
      )}

      {!novel ? (
        <div className="empty">
          <div className="card">
            <h2 style={{ margin: 0, fontSize: 16 }}>创建你的第一部小说</h2>
            <p className="hint">
              NovelOS 会为它维护人物状态、事件、时间线、伏笔与 Canon 事实；
              AI 只能提出 PROPOSED 事实，经你确认后才升级为 CANON。
            </p>
            <div className="field-row">
              <input
                style={{ flex: 1 }}
                placeholder="书名，例如：剑起青云"
                value={newTitle}
                onChange={(event) => setNewTitle(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void createNovel();
                }}
              />
              <button className="primary" onClick={() => void createNovel()} disabled={busy}>
                创建
              </button>
            </div>
            <div className="hint">
              创建后可点击「装载测试小说」，导入内置的 20 章示例（含 2 处故意设置的前后矛盾）用于验证。
            </div>
          </div>
        </div>
      ) : (
        <div className="workspace">
          <ChapterPanel
            chapters={chapters}
            total={chapterTotal}
            loadingMore={loadingMore}
            onLoadMore={() => void loadMoreChapters()}
            currentId={chapter?.chapter_id ?? null}
            onSelect={(id) => void selectChapter(id)}
            onNewChapter={() => void newChapter()}
            onDeleteChapter={(id) => void deleteChapter(id)}
            onSearch={async (query) => (await api.search(novel.id, query)).hits}
            busy={busy}
          />
          <EditorPanel
            chapter={chapter}
            draft={draft}
            dirty={dirty}
            busy={busy}
            report={report}
            onChange={(patch) => {
              setDraft((current) => (current ? { ...current, ...patch } : current));
              setDirty(true);
            }}
            onSave={() => void ensureSaved()}
            onCompleteChapter={() => void completeChapter()}
            status={report ? report.message : status}
          />
          <AIPanel
            novelId={novel.id}
            chapterId={chapter?.chapter_id ?? null}
            chapterNumber={chapter?.chapter_number ?? null}
            nextChapterNumber={nextChapterNumber}
            provider={provider}
            bible={bible}
            storedReport={storedReport}
            fragmentsVersion={fragmentsVersion}
            onFragmentsChanged={noteFragmentsChanged}
            ensureSaved={ensureSaved}
            onRefresh={() => refreshNovelData(novel)}
            setStatus={setStatus}
            setError={setError}
          />
        </div>
      )}

      {novel && (
        <KnowledgePanel
          novelId={novel.id}
          bible={bible}
          dashboard={dashboard}
          provider={provider}
          chaptersVersion={chapters.length}
          nextChapterNumber={nextChapterNumber}
          fragmentsVersion={fragmentsVersion}
          onFragmentsChanged={noteFragmentsChanged}
          onRefresh={() => refreshNovelData(novel)}
          setStatus={setStatus}
          setError={setError}
        />
      )}

      <div className="statusbar">
        {busy && <span>处理中…</span>}
        {error ? <span className="error-text">{error}</span> : <span>{status}</span>}
        <span className="spacer" />
        {novel && <span>正文文件：data/novels/{novel.slug}/chNNN.md</span>}
      </div>
    </div>
  );
}
