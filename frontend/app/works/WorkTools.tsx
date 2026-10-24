"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { platformRequest } from "@/lib/platform";
import shell from "../platform.module.css";
import styles from "./works.module.css";

type Asset = { id: string; title: string; revision_id: string };
type Attachment = {
  id: string;
  filename: string;
  size: number;
  sha256: string;
  asset_id: string | null;
};
type Reference = {
  id: string;
  source_title: string;
  source_version: number;
  target_title: string;
  target_version: number;
  relation: string;
  source_is_current: boolean;
  target_is_current: boolean;
};
type Status = {
  counts: Record<string, number>;
  indexed_current_assets: number;
  total_assets: number;
  failures: {
    id: string;
    kind: string;
    state: string;
    attempts: number;
    error_code: string;
  }[];
};
type Mapping = {
  id: string;
  source_id: string;
  table_name: string;
  legacy_id: string;
  asset_id: string | null;
};

export default function WorkTools({
  workId,
  assets,
  onOpen,
}: {
  workId: string;
  assets: Asset[];
  onOpen: (id: string) => void;
}) {
  const base = `/works/${workId}`;
  const [status, setStatus] = useState<Status | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [references, setReferences] = useState<Reference[]>([]);
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [payload, setPayload] = useState("");
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [relation, setRelation] = useState("reference");
  const [attachmentAsset, setAttachmentAsset] = useState("");
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<
    { id: string; title: string; number: number }[]
  >([]);
  const [searched, setSearched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const versions = assets.map((a) => a.revision_id).join(",");

  const refresh = useCallback(async () => {
    const [delivery, files, refs, imported] = await Promise.all([
      platformRequest<Status>(base + "/projections"),
      platformRequest<Attachment[]>(base + "/attachments"),
      platformRequest<Reference[]>(base + "/references"),
      platformRequest<Mapping[]>(base + "/legacy-records"),
    ]);
    setStatus(delivery);
    setAttachments(files);
    setReferences(refs);
    setMappings(imported);
  }, [base]);

  useEffect(() => {
    void refresh().catch((err) => setError(err.message));
  }, [refresh, versions]);
  useEffect(() => {
    let mounted = true;
    const timer = window.setInterval(() => {
      platformRequest<Status>(base + "/projections")
        .then((current) => {
          if (mounted) setStatus(current);
        })
        .catch((err) => {
          if (mounted) setError(err.message);
        });
    }, 5000);
    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, [base]);

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

  async function upload(file?: File) {
    if (!file) return;
    await run(async () => {
      if (file.size > 4 * 1024 * 1024) throw new Error("单个附件最多 4 MiB。");
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = () => reject(new Error("无法读取附件"));
        reader.readAsDataURL(file);
      });
      await platformRequest(base + "/attachments", {
        method: "POST",
        body: JSON.stringify({
          filename: file.name,
          data_base64: data,
          asset_id: attachmentAsset || null,
        }),
      });
      await refresh();
      setNotice(
        "附件原文件已保存，可以下载或随作品导出。后台正在同步文件副本。",
      );
    });
  }

  async function addReference(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      const from = assets.find((a) => a.id === source);
      const to = assets.find((a) => a.id === target);
      if (!from || !to) throw new Error("请选择同一作品内的两个资产。");
      await platformRequest(base + "/references", {
        method: "POST",
        body: JSON.stringify({
          source_revision_id: from.revision_id,
          target_revision_id: to.revision_id,
          relation,
        }),
      });
      await refresh();
      setNotice(
        "引用已绑定双方当前已保存的版本。修改正文后可建立新的引用记录。",
      );
    });
  }

  return (
    <section className={styles.tools} aria-label="附件、检索与来源">
      <h2>附件、检索与来源</h2>
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
      <section className={styles.toolSection}>
        <h3>检索与文件同步</h3>
        <p>
          已建立索引：{status?.indexed_current_assets ?? "—"} /{" "}
          {status?.total_assets ?? "—"} 个当前资产。 待处理{" "}
          {status?.counts.pending ?? 0} · 处理中 {status?.counts.running ?? 0} ·
          失败 {status?.counts.failed ?? 0}。
        </p>
        <p>
          搜索匹配已保存标题和正文中的文字。未完成索引的内容暂不参与结果；旧版本不会混入当前正文。
        </p>
        <div className={styles.toolbar}>
          <button disabled={busy} onClick={() => run(refresh)}>
            刷新同步状态
          </button>
          <button
            disabled={busy}
            onClick={() =>
              run(async () => {
                const result = await platformRequest<{ queued: number }>(
                  base + "/projections/rebuild",
                  { method: "POST" },
                );
                await refresh();
                setNotice(
                  `已排队 ${result.queued} 项重建，后台运行后更新状态，原文保持完整。`,
                );
              })
            }
          >
            重建索引与文件副本
          </button>
        </div>
        <p>后台未运行时任务会保留在队列中；单个任务自动重试最多 5 次。</p>
        {status?.failures.map((job) => (
          <div className={styles.toolbar} key={job.id}>
            <span>
              {job.kind} · 已尝试 {job.attempts} 次 · {job.error_code}
            </span>
            {job.state === "failed" && (
              <button
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    await platformRequest(
                      base + `/projections/${job.id}/retry`,
                      { method: "POST" },
                    );
                    await refresh();
                  })
                }
              >
                重试失败任务
              </button>
            )}
          </div>
        ))}
        <form
          className={styles.toolForm}
          onSubmit={(event) => {
            event.preventDefault();
            void run(async () => {
              setMatches(
                await platformRequest(
                  base + `/search?q=${encodeURIComponent(query)}`,
                ),
              );
              setSearched(true);
            });
          }}
        >
          <label>
            搜索当前正文
            <input
              maxLength={200}
              required
              disabled={busy}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <button disabled={busy || !query.trim()}>搜索</button>
        </form>
        {searched && <p>本次显示 {matches.length} 条结果，最多 50 条。</p>}
        <ul className={styles.resultList}>
          {matches.map((item) => (
            <li key={item.id}>
              <button disabled={busy} onClick={() => onOpen(item.id)}>
                {item.title} · 版本 {item.number}
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section className={styles.toolSection}>
        <h3>原始附件</h3>
        <p>附件按原文件保存并提供下载，当前不解析或执行文件内容。</p>
        <div className={styles.toolForm}>
          <label>
            附件所属资产
            <select
              disabled={busy}
              value={attachmentAsset}
              onChange={(e) => setAttachmentAsset(e.target.value)}
            >
              <option value="">整部作品</option>
              {assets.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.title}
                </option>
              ))}
            </select>
          </label>
          <label>
            上传附件（≤ 4 MiB）
            <input
              aria-label="上传附件"
              type="file"
              disabled={busy}
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                void upload(file);
              }}
            />
          </label>
        </div>
        <ul className={styles.resultList}>
          {attachments.map((file) => (
            <li key={file.id}>
              <a
                href={`/api/v1${base}/attachments/${file.id}/download`}
                download
              >
                {file.filename}
              </a>
              <span> · {file.size.toLocaleString()} 字节</span>
              <small className={styles.hash}>SHA-256：{file.sha256}</small>
            </li>
          ))}
        </ul>
        {attachments.length > 0 && attachments.length % 50 === 0 && (
          <button
            disabled={busy}
            onClick={() =>
              run(async () => {
                const more = await platformRequest<Attachment[]>(
                  base + `/attachments?offset=${attachments.length}`,
                );
                setAttachments((old) => [...old, ...more]);
                if (!more.length) setNotice("已显示全部附件。");
              })
            }
          >
            更多附件
          </button>
        )}
      </section>
      <section className={styles.toolSection}>
        <h3>版本引用与依赖</h3>
        <p>
          记录明确引用的版本，便于后续追踪。这里不自动推断剧情影响，也不迁移引用到新版本。
        </p>
        <form className={styles.toolForm} onSubmit={addReference}>
          <label>
            引用来源
            <select
              required
              disabled={busy}
              value={source}
              onChange={(e) => setSource(e.target.value)}
            >
              <option value="">选择资产</option>
              {assets.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.title}
                </option>
              ))}
            </select>
          </label>
          <label>
            引用目标
            <select
              required
              disabled={busy}
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            >
              <option value="">选择资产</option>
              {assets
                .filter((a) => a.id !== source)
                .map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.title}
                  </option>
                ))}
            </select>
          </label>
          <label>
            关系
            <select
              disabled={busy}
              value={relation}
              onChange={(e) => setRelation(e.target.value)}
            >
              <option value="reference">参考</option>
              <option value="depends_on">依赖</option>
            </select>
          </label>
          <button disabled={busy || !source || !target || source === target}>
            保存版本引用
          </button>
        </form>
        <ul className={styles.resultList}>
          {references.map((ref) => (
            <li key={ref.id}>
              {ref.source_title} v{ref.source_version} → {ref.target_title} v
              {ref.target_version}（
              {ref.relation === "reference" ? "参考" : "依赖"}）
              <small>
                {ref.source_is_current
                  ? "来源仍为当前版本"
                  : "来源已产生新版本"}
                ；
                {ref.target_is_current
                  ? "目标仍为当前版本"
                  : "目标已产生新版本，请复查"}
              </small>
            </li>
          ))}
        </ul>
        {references.length > 0 && references.length % 50 === 0 && (
          <button
            disabled={busy}
            onClick={() =>
              run(async () => {
                const more = await platformRequest<Reference[]>(
                  base + `/references?offset=${references.length}`,
                );
                setReferences((old) => [...old, ...more]);
                if (!more.length) setNotice("已显示全部引用。");
              })
            }
          >
            更多引用
          </button>
        )}
      </section>
      <section className={styles.toolSection}>
        <h3>旧库来源记录</h3>
        <p>
          旧库导入后，可在这里核对来源、原始记录与章节映射。旧设定和状态是待审资料，不自动成为正式记忆。
        </p>
        {!mappings.length && <p>当前作品没有旧库导入记录。</p>}
        <ul className={styles.resultList}>
          {mappings.map((record) => (
            <li key={record.id}>
              <button
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const original = await platformRequest(
                      base + `/legacy-records/${record.id}`,
                    );
                    setPayload(JSON.stringify(original, null, 2));
                  })
                }
              >
                {record.table_name} · {record.legacy_id}
              </button>
              <small>来源：{record.source_id}</small>
            </li>
          ))}
        </ul>
        {mappings.length > 0 && mappings.length % 50 === 0 && (
          <button
            disabled={busy}
            onClick={() =>
              run(async () => {
                const more = await platformRequest<Mapping[]>(
                  base + `/legacy-records?offset=${mappings.length}`,
                );
                setMappings((old) => [...old, ...more]);
                if (!more.length) setNotice("已显示全部来源记录。");
              })
            }
          >
            更多来源记录
          </button>
        )}
        {payload && (
          <pre className={styles.record} aria-label="旧库原始记录">
            {payload}
          </pre>
        )}
      </section>
    </section>
  );
}
