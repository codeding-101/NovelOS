"use client";
import { useEffect, useState } from "react";
import { platformRequest } from "@/lib/platform";
import styles from "../budgets/budgets.module.css";

type Grant = {
  id: string;
  name: string;
  expires_at: string;
  revoked_at: string | null;
};
export default function AgentGrants({
  task,
  refresh,
}: {
  task: { id: string; version: number; state: string };
  refresh: () => Promise<void>;
}) {
  const [grants, setGrants] = useState<Grant[]>([]);
  const [secret, setSecret] = useState("");
  const [name, setName] = useState("离线 Agent");
  const [hours, setHours] = useState(1);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const base = `/tasks/${task.id}/agent-grants`;
  useEffect(() => {
    setSecret("");
    setMessage("");
    setError("");
  }, [task.id]);
  useEffect(() => {
    let active = true;
    platformRequest<Grant[]>(base)
      .then((rows) => {
        if (active) setGrants(rows);
      })
      .catch((err) => {
        if (active) setError(err.message);
      });
    return () => {
      active = false;
    };
  }, [base, task.version]);
  async function run(action: () => Promise<void>) {
    setPending(true);
    setError("");
    try {
      await action();
      await refresh();
      setGrants(await platformRequest<Grant[]>(base));
    } catch (err) {
      setError(err instanceof Error ? err.message : "授权操作失败");
    } finally {
      setPending(false);
    }
  }
  return (
    <section className={styles.panel} aria-label="Agent 任务授权">
      <h3>Agent 任务授权</h3>
      <p>
        令牌仅允许操作当前任务。正文写入、供应商密钥、付费调用和定稿均不授权。
      </p>
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      <label>
        授权名称
        <input
          value={name}
          maxLength={128}
          disabled={pending}
          onChange={(event) => setName(event.target.value)}
        />
      </label>
      <label>
        令牌有效小时
        <input
          type="number"
          min={1}
          max={24}
          step={1}
          value={hours}
          disabled={pending}
          onChange={(event) => setHours(Number(event.target.value))}
        />
      </label>
      <button
        disabled={
          pending ||
          !name.trim() ||
          !["QUEUED", "RETRY_WAIT", "PAUSED"].includes(task.state)
        }
        onClick={() =>
          void run(async () => {
            const issued = await platformRequest<Grant & { token: string }>(
              base,
              {
                method: "POST",
                body: JSON.stringify({
                  name,
                  expected_version: task.version,
                  expires_seconds: hours * 3600,
                }),
              },
            );
            setSecret(issued.token);
            setMessage(
              "令牌仅此次返回。复制并交给你的 Agent；未保存时请撤销后重新签发。",
            );
          })
        }
      >
        签发任务令牌
      </button>
      {secret && (
        <>
          <label>
            本次 Agent 令牌
            <input type="password" readOnly value={secret} autoComplete="off" />
          </label>
          <button
            disabled={pending}
            onClick={() => {
              void navigator.clipboard
                .writeText(secret)
                .then(() => setMessage("令牌已复制，请妥善保管。"))
                .catch(() => setError("复制失败，请选中令牌输入框手动复制。"));
            }}
          >
            复制令牌
          </button>
          <button onClick={() => setSecret("")}>清除页面令牌</button>
        </>
      )}
      <p>任务 ID：{task.id}</p>
      <p>
        参考客户端读取任务输入后进行本地协议校验，不会调用模型。令牌撤销会暂停正在执行的任务，迟到回报无法生效。
      </p>
      {grants.map((grant) => (
        <article className={styles.record} key={grant.id}>
          <strong>{grant.name}</strong>
          <p>
            到期：{new Date(grant.expires_at).toLocaleString()} ·{" "}
            {grant.revoked_at
              ? "已撤销"
              : Date.parse(grant.expires_at) <= Date.now()
                ? "已过期"
                : "有效"}
          </p>
          {!grant.revoked_at && (
            <button
              disabled={pending}
              aria-label={`撤销授权 ${grant.name}`}
              onClick={() =>
                void run(async () => {
                  await platformRequest(`${base}/${grant.id}`, {
                    method: "DELETE",
                  });
                  setSecret("");
                  setMessage("授权已撤销。");
                })
              }
            >
              撤销授权
            </button>
          )}
        </article>
      ))}
    </section>
  );
}
