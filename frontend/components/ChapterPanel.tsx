"use client";

import { useState } from "react";
import type { ChapterSummary, SearchHit } from "@/lib/types";

interface Props {
  chapters: ChapterSummary[];
  total: number;
  loadingMore: boolean;
  onLoadMore: () => void;
  currentId: string | null;
  onSelect: (id: string) => void;
  onNewChapter: () => void;
  onDeleteChapter: (id: string) => void;
  onSearch: (query: string) => Promise<SearchHit[]>;
  busy: boolean;
}

export function ChapterPanel({
  chapters,
  total,
  loadingMore,
  onLoadMore,
  currentId,
  onSelect,
  onNewChapter,
  onDeleteChapter,
  onSearch,
  busy,
}: Props) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  async function runSearch() {
    if (!query.trim()) {
      setHits(null);
      return;
    }
    setSearching(true);
    try {
      setHits(await onSearch(query.trim()));
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="column">
      <div className="column-header">
        章节
        <span className="spacer" />
        <button onClick={onNewChapter} disabled={busy}>
          新建
        </button>
      </div>
      <div className="column-body">
        <div className="field-row" style={{ marginBottom: 8 }}>
          <input
            placeholder="全文搜索章节"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void runSearch();
            }}
            style={{ flex: 1, minWidth: 0 }}
          />
          <button onClick={() => void runSearch()} disabled={searching}>
            搜索
          </button>
        </div>
        {hits && (
          <div style={{ marginBottom: 10 }}>
            <div className="hint">
              命中 {hits.length} 章（点击跳转）
              <button
                style={{ marginLeft: 6, padding: "1px 6px" }}
                onClick={() => setHits(null)}
              >
                清除
              </button>
            </div>
            {hits.map((hit) => (
              <div
                key={hit.chapter_id}
                className="item-row"
                style={{ cursor: "pointer" }}
                onClick={() => onSelect(hit.chapter_id)}
              >
                <div className="row-head">
                  <b>
                    第{hit.chapter_number}章 {hit.title}
                  </b>
                  <span className="badge">{hit.match_source}</span>
                </div>
                <div className="payload">{hit.snippet}</div>
              </div>
            ))}
          </div>
        )}

        {chapters.length === 0 && <div className="hint">还没有章节。点击「新建」开始，或在下方装载测试小说。</div>}
        {chapters.map((chapter) => (
          <div
            key={chapter.chapter_id}
            className={`chapter-item ${chapter.chapter_id === currentId ? "active" : ""}`}
            onClick={() => onSelect(chapter.chapter_id)}
          >
            <span>
              {chapter.chapter_number}. {chapter.title || "（无标题）"}
            </span>
            <span className="meta">
              {chapter.word_count}字
              {chapter.status === "DRAFT" ? " · 草稿" : ""}
              <button
                style={{ marginLeft: 6, padding: "0 5px" }}
                onClick={(event) => {
                  event.stopPropagation();
                  onDeleteChapter(chapter.chapter_id);
                }}
                title="删除该章"
              >
                ×
              </button>
            </span>
          </div>
        ))}

        {chapters.length > 0 && (
          <div className="hint" style={{ marginTop: 8 }}>
            共 {total} 章，已加载 {chapters.length} 章
            {chapters.length < total && (
              <>
                {" "}
                <button onClick={onLoadMore} disabled={loadingMore}>
                  {loadingMore ? "加载中…" : "加载更多"}
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
