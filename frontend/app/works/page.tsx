"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { platformRequest } from "@/lib/platform";
import shell from "../platform.module.css";
import styles from "./works.module.css";

type Work = { id: string; title: string; description: string };
type Kind = "volume" | "chapter" | "scene" | "material";
type Asset = {
  id: string;
  title: string;
  kind: Kind;
  parent_id: string | null;
  head_version: number;
};
type Revision = {
  id: string;
  asset_id: string;
  number: number;
  title: string;
  content: string;
  sha256: string;
  restored_from: string | null;
  created_at: string;
};
type Detail = {
  id: string;
  kind: Kind;
  parent_id: string | null;
  revision: Revision;
};
type Audit = {
  id: string;
  action: string;
  created_at: string;
  target_id: string;
};
const kinds: Record<Kind, string> = {
  volume: "卷",
  chapter: "章节",
  scene: "场景",
  material: "资料",
};
const actions: Record<string, string> = {
  "work.created": "创建作品",
  "asset.created": "创建资产",
  "asset.saved": "保存新版本",
  "asset.restored": "恢复历史版本",
  "asset.imported": "导入原稿",
  "work.exported": "导出草稿",
};

async function allPages<T>(path: string): Promise<T[]> {
  const items: T[] = [];
  for (let offset = 0; ; offset += 100) {
    const page = await platformRequest<T[]>(
      `${path}?offset=${offset}&limit=100`,
    );
    items.push(...page);
    if (page.length < 100) return items;
  }
}

export default function WorksPage() {
  const [works, setWorks] = useState<Work[]>([]);
  const [workId, setWorkId] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [history, setHistory] = useState<Revision[]>([]);
  const [preview, setPreview] = useState<Revision | null>(null);
  const [diff, setDiff] = useState("");
  const [audit, setAudit] = useState<Audit[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [newWork, setNewWork] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [kind, setKind] = useState<Kind>("chapter");
  const [parentId, setParentId] = useState("");
  const [pending, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const busy = pending || !ready;
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [expired, setExpired] = useState(false);
  const dirty =
    !!detail &&
    (title !== detail.revision.title || content !== detail.revision.content);
  const base = `/works/${workId}`;
  const endpoint = `${base}/assets/${detail?.id}`;

  const loadWorks = useCallback(
    async () => setWorks(await allPages<Work>("/works")),
    [],
  );
  useEffect(() => {
    loadWorks()
      .catch((err) => setError(err.message))
      .finally(() => setReady(true));
    const expiredSession = () => setExpired(true);
    window.addEventListener("novelos-session-expired", expiredSession);
    return () =>
      window.removeEventListener("novelos-session-expired", expiredSession);
  }, [loadWorks]);

  useEffect(() => {
    function protect(event: BeforeUnloadEvent) {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty]);

  function mayDiscard() {
    return !dirty || window.confirm("当前输入尚未保存。放弃这些输入并继续吗？");
  }

  async function run(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function openWork(id: string) {
    const items = await allPages<Asset>(`/works/${id}/assets`);
    setWorkId(id);
    setAssets(items);
    setDetail(null);
    setHistory([]);
    setPreview(null);
    setTitle("");
    setContent("");
    setParentId("");
    setAudit([]);
  }

  async function openAsset(id: string) {
    const [item, versions] = await Promise.all([
      platformRequest<Detail>(`${base}/assets/${id}`),
      platformRequest<Revision[]>(`${base}/assets/${id}/revisions`),
    ]);
    setDetail(item);
    setTitle(item.revision.title);
    setContent(item.revision.content);
    setHistory(versions);
    setPreview(null);
    setDiff("");
  }

  async function updateSaved(revision: Revision) {
    setDetail((old) => old && { ...old, revision });
    setTitle(revision.title);
    setContent(revision.content);
    setPreview(null);
    setDiff("");
    const [items, versions] = await Promise.all([
      allPages<Asset>(base + "/assets"),
      platformRequest<Revision[]>(endpoint + "/revisions"),
    ]);
    setAssets(items);
    setHistory(versions);
  }

  async function createWork(event: FormEvent) {
    event.preventDefault();
    if (!mayDiscard()) return;
    await run(async () => {
      const work = await platformRequest<Work>("/works", {
        method: "POST",
        body: JSON.stringify({ title: newWork }),
      });
      await loadWorks();
      await openWork(work.id);
      setNewWork("");
      setNotice("作品已建立，可以添加章节或导入旧稿。");
    });
  }

  async function createAsset(event: FormEvent) {
    event.preventDefault();
    if (!mayDiscard()) return;
    await run(async () => {
      const created = await platformRequest<{ asset_id: string }>(
        base + "/assets",
        {
          method: "POST",
          body: JSON.stringify({
            title: newTitle,
            kind,
            parent_id: parentId || null,
          }),
        },
      );
      setAssets(await allPages<Asset>(base + "/assets"));
      await openAsset(created.asset_id);
      setNewTitle("");
      setNotice("已创建，当前为草稿。");
    });
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!detail) return;
    await run(async () => {
      const revision = await platformRequest<Revision>(
        endpoint + "/revisions",
        {
          method: "POST",
          body: JSON.stringify({
            title,
            content,
            expected_version: detail.revision.number,
          }),
        },
      );
      await updateSaved(revision);
      setNotice(`已保存版本 ${revision.number}，之前的正文仍保留在历史中。`);
    });
  }

  async function compare(id: string) {
    if (!detail) return;
    await run(async () => {
      const version = await platformRequest<Revision>(
        endpoint + `/revisions/${id}`,
      );
      setPreview(version);
      setDiff("");
      try {
        const result = await platformRequest<{
          diff: string;
          source_title: string;
          target_title: string;
        }>(endpoint + `/diff?source=${id}&target=${detail.revision.id}`);
        setDiff(
          `标题：${result.source_title} → ${result.target_title}\n${result.diff || "正文相同"}`,
        );
      } catch (err) {
        setDiff(
          err instanceof Error
            ? err.message
            : "差异预览失败，可在下方查看历史正文",
        );
      }
    });
  }

  async function restore() {
    if (!detail || !preview || !mayDiscard()) return;
    await run(async () => {
      const revision = await platformRequest<Revision>(
        endpoint + `/revisions/${preview.id}/restore`,
        {
          method: "POST",
          body: JSON.stringify({ expected_version: detail.revision.number }),
        },
      );
      await updateSaved(revision);
      setNotice(`已恢复为新版本 ${revision.number}，所有历史版本均保留。`);
    });
  }

  async function importFile(file?: File) {
    if (!file || !mayDiscard()) return;
    await run(async () => {
      if (file.size > 2 * 1024 * 1024)
        throw new Error("单个文件不能超过 2 MiB，请按章节拆分。");
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = () => reject(new Error("文件读取失败"));
        reader.readAsDataURL(file);
      });
      const created = await platformRequest<{ asset_id: string }>(
        base + "/import",
        {
          method: "POST",
          body: JSON.stringify({
            filename: file.name,
            data_base64: data,
            kind: kind === "material" ? "material" : "chapter",
            parent_id: kind === "chapter" ? parentId || null : null,
          }),
        },
      );
      setAssets(await allPages<Asset>(base + "/assets"));
      await openAsset(created.asset_id);
      setNotice("原稿已导入为草稿。原文件字节和校验值已单独保留。");
    });
  }

  async function exportDraft() {
    await run(async () => {
      const response = await fetch(`/api/v1${base}/export`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        if (response.status === 401) setExpired(true);
        const body = await response.json().catch(() => null);
        throw new Error(body?.error?.message || "导出失败");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `novelos-draft-${workId}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
      setNotice(
        "已导出已保存的草稿、历史版本和原文件，并附带校验清单。未保存的输入不在导出中。",
      );
    });
  }

  const eligibleParents = assets.filter(
    (a) => a.kind === (kind === "scene" ? "chapter" : "volume"),
  );
  return (
    <main className={shell.shell}>
      <header className={shell.header}>
        <div>
          <div className={shell.wordmark}>
            NovelOS <span>作品空间</span>
          </div>
          <p>保存每一次修改，让故事可以从容生长。</p>
        </div>
        <a className={styles.link} href="/">
          返回控制台
        </a>
      </header>
      <h1>作品与原稿</h1>
      <p>这里保存草稿及其历史。真实检测与其他必要审查接入前，正文不能定稿。</p>
      {error && (
        <div role="alert" className={shell.error}>
          {error}
        </div>
      )}
      {notice && (
        <div role="status" className={shell.notice}>
          {notice}
        </div>
      )}
      {expired && (
        <div className={shell.notice}>
          登录已失效，当前输入仍保留在本页。
          <a href="/" target="_blank" rel="noreferrer">
            在新标签页登录
          </a>
          后，
          <button
            disabled={busy}
            onClick={() =>
              run(async () => {
                await loadWorks();
                setExpired(false);
              })
            }
          >
            重新连接
          </button>
        </div>
      )}
      <section className={styles.workPicker} aria-label="作品选择">
        <label>
          当前作品
          <select
            value={workId}
            disabled={busy}
            onChange={(event) => {
              if (mayDiscard()) void run(() => openWork(event.target.value));
            }}
          >
            <option value="" disabled>
              选择作品
            </option>
            {works.map((w) => (
              <option key={w.id} value={w.id}>
                {w.title}
              </option>
            ))}
          </select>
        </label>
        <form onSubmit={createWork}>
          <label>
            新作品名称
            <input
              required
              maxLength={256}
              value={newWork}
              disabled={busy}
              onChange={(e) => setNewWork(e.target.value)}
            />
          </label>
          <button disabled={busy || !newWork.trim()}>创建作品</button>
        </form>
      </section>
      {!workId && (
        <p className={styles.empty}>选择或创建一部作品，开始整理章节与资料。</p>
      )}
      {workId && (
        <>
          <div className={styles.toolbar}>
            <strong>{works.find((w) => w.id === workId)?.title}</strong>
            <button disabled={busy} onClick={exportDraft}>
              导出草稿与历史
            </button>
            <button
              disabled={busy}
              onClick={() =>
                run(async () =>
                  setAudit(await platformRequest<Audit[]>(base + "/audit")),
                )
              }
            >
              查看最近操作
            </button>
          </div>
          <div className={styles.workspace}>
            <aside className={styles.sidebar}>
              <h2>作品目录</h2>
              <ul className={styles.assets}>
                {assets.map((a) => (
                  <li key={a.id}>
                    <button
                      className={detail?.id === a.id ? styles.selected : ""}
                      disabled={busy}
                      onClick={() => {
                        if (mayDiscard()) void run(() => openAsset(a.id));
                      }}
                    >
                      <small>
                        {kinds[a.kind]}
                        {a.parent_id
                          ? ` · ${assets.find((p) => p.id === a.parent_id)?.title || "上级资产"}`
                          : ""}
                      </small>
                      <span>{a.title}</span>
                      <small>版本 {a.head_version} · 草稿</small>
                    </button>
                  </li>
                ))}
              </ul>
              {!assets.length && <p>还没有章节或资料。</p>}
              <form className={styles.addForm} onSubmit={createAsset}>
                <label>
                  资产类型
                  <select
                    disabled={busy}
                    value={kind}
                    onChange={(e) => {
                      setKind(e.target.value as Kind);
                      setParentId("");
                    }}
                  >
                    {Object.entries(kinds).map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                {(kind === "chapter" || kind === "scene") && (
                  <label>
                    所属{kind === "scene" ? "章节" : "卷"}
                    <select
                      disabled={busy}
                      value={parentId}
                      required={kind === "scene"}
                      onChange={(e) => setParentId(e.target.value)}
                    >
                      <option value="">
                        {kind === "scene" ? "请选择章节" : "不分卷"}
                      </option>
                      {eligibleParents.map((p) => (
                        <option value={p.id} key={p.id}>
                          {p.title}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label>
                  新资产标题
                  <input
                    required
                    maxLength={256}
                    disabled={busy}
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                  />
                </label>
                <button
                  disabled={
                    busy || !newTitle.trim() || (kind === "scene" && !parentId)
                  }
                >
                  添加{kinds[kind]}
                </button>
              </form>
              {(kind === "chapter" || kind === "material") && (
                <label className={styles.importer}>
                  导入为{kinds[kind]}（UTF-8 TXT／MD，≤ 2 MiB）
                  <input
                    aria-label="导入原稿"
                    type="file"
                    accept=".txt,.md"
                    disabled={busy}
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      e.target.value = "";
                      void importFile(file);
                    }}
                  />
                </label>
              )}
            </aside>
            <section className={styles.editor} aria-label="正文编辑器">
              {!detail ? (
                <p className={styles.empty}>从目录选择资产，或导入已有原稿。</p>
              ) : (
                <>
                  <div className={styles.toolbar}>
                    <h2>{kinds[detail.kind]}编辑</h2>
                    <span>
                      {dirty
                        ? "有未保存的输入"
                        : `已保存版本 ${detail.revision.number}`}
                    </span>
                  </div>
                  <form onSubmit={save} className={styles.editForm}>
                    <label>
                      标题
                      <input
                        maxLength={256}
                        required
                        value={title}
                        disabled={busy}
                        onChange={(e) => setTitle(e.target.value)}
                      />
                    </label>
                    <div>
                      <label htmlFor="asset-content">正文</label>
                      <textarea
                        id="asset-content"
                        value={content}
                        disabled={busy}
                        onChange={(e) => setContent(e.target.value)}
                        spellCheck={false}
                      />
                    </div>
                    <div className={styles.toolbar}>
                      <button
                        className={shell.primary}
                        disabled={busy || !dirty || !title.trim()}
                      >
                        保存新版本
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => {
                          if (mayDiscard())
                            void run(() => openAsset(detail.id));
                        }}
                      >
                        读取最新版本
                      </button>
                      <span>
                        {Array.from(content).length.toLocaleString()} 字符 ·
                        草稿
                      </span>
                    </div>
                  </form>
                  <section className={styles.history}>
                    <h2>版本历史</h2>
                    <p>
                      比较以历史版本为起点、当前已保存版本为终点。恢复会新建版本。
                    </p>
                    <div className={styles.versions}>
                      {history.map((r) => (
                        <button
                          key={r.id}
                          disabled={busy}
                          onClick={() => compare(r.id)}
                          aria-pressed={preview?.id === r.id}
                        >
                          版本 {r.number}
                          {r.restored_from ? " · 恢复" : ""}
                          <small>
                            {new Date(r.created_at).toLocaleString()}
                          </small>
                        </button>
                      ))}
                    </div>
                    {history.length > 0 &&
                      history[history.length - 1].number > 1 && (
                        <button
                          disabled={busy}
                          onClick={() =>
                            run(async () => {
                              const older = await platformRequest<Revision[]>(
                                endpoint +
                                  `/revisions?before=${history[history.length - 1].number}`,
                              );
                              setHistory((current) => [...current, ...older]);
                            })
                          }
                        >
                          加载更早版本
                        </button>
                      )}
                    {preview && (
                      <div className={styles.preview}>
                        <h3>版本 {preview.number} 恢复预览</h3>
                        <p>标题：{preview.title}</p>
                        <pre aria-label="版本差异">{diff}</pre>
                        <label htmlFor="historical-content">历史正文</label>
                        <textarea id="historical-content" readOnly value={preview.content} />
                        <p className={styles.hash}>SHA-256：{preview.sha256}</p>
                        <button
                          disabled={busy || preview.id === detail.revision.id}
                          onClick={restore}
                        >
                          将此版本恢复为新版本
                        </button>
                      </div>
                    )}
                  </section>
                </>
              )}
            </section>
          </div>
          {audit.length > 0 && (
            <section className={styles.audit}>
              <h2>最近操作</h2>
              <ul>
                {audit.map((event) => (
                  <li key={event.id}>
                    {new Date(event.created_at).toLocaleString()} ·{" "}
                    {actions[event.action] || event.action}
                  </li>
                ))}
              </ul>
              <p>显示最近 50 次已提交的操作。</p>
            </section>
          )}
        </>
      )}
    </main>
  );
}
