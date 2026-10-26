"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { platformRequest } from "@/lib/platform";
import shell from "../platform.module.css";
import styles from "../budgets/budgets.module.css";

type Quota = {
  id: string;
  name: string;
  timezone: string;
  version: number;
  period: string;
  limit_tokens: number;
  spent_tokens: number;
  held_tokens: number;
  remaining_tokens: number;
  warning: boolean;
  over_limit: boolean;
  unknown_count: number;
  executor_ids: string[];
};
type Executor = { id: string; name: string; mode: string; api_config: unknown };
type Call = {
  id: string;
  work_title: string;
  period: string;
  state: string;
  reserved_tokens: number;
  input_tokens: number | null;
  output_tokens: number | null;
};
type Entry = {
  id: string;
  action: string;
  created_at: string;
  details: {
    before?: number;
    after?: number;
    executor_id?: string;
    limit_tokens?: number;
  };
};
const stateNames: Record<string, string> = {
  RESERVED: "未发送预留",
  DISPATCHED: "已登记发送",
  UNKNOWN: "用量待确认",
  SETTLED: "已结算",
  RELEASED: "已释放",
};
function tokenValue(value: string) {
  if (!/^\d{1,13}$/.test(value) || Number(value) > 10 ** 12)
    throw new Error("token 上限须为 0 至 1,000,000,000,000 的整数。");
  return Number(value);
}

export default function QuotasPage() {
  const [quotas, setQuotas] = useState<Quota[]>([]);
  const [executors, setExecutors] = useState<Executor[]>([]);
  const [selected, setSelected] = useState<Quota | null>(null);
  const [calls, setCalls] = useState<Call[]>([]);
  const [events, setEvents] = useState<Entry[]>([]);
  const [offset, setOffset] = useState(0);
  const [name, setName] = useState("");
  const [zone, setZone] = useState("Asia/Shanghai");
  const [initial, setInitial] = useState("");
  const [limit, setLimit] = useState("");
  const [executorId, setExecutorId] = useState("");
  const [pending, setPending] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [expired, setExpired] = useState(false);
  const requestKey = useRef("");
  const dirty = Boolean(
    name || initial || (selected && limit !== String(selected.limit_tokens)),
  );
  const busy = pending || expired;

  async function list() {
    const all: Quota[] = [];
    for (let start = 0; ; start += 100) {
      const rows = await platformRequest<Quota[]>(
        `/quotas?offset=${start}&limit=100`,
      );
      all.push(...rows);
      if (rows.length < 100) break;
    }
    setQuotas(all);
    setExecutors(await platformRequest<Executor[]>("/executors"));
  }
  async function load(id: string, start = 0) {
    const [current, rows, journal] = await Promise.all([
      platformRequest<Quota>(`/quotas/${id}`),
      platformRequest<Call[]>(`/quotas/${id}/calls?offset=${start}&limit=20`),
      platformRequest<Entry[]>(`/quotas/${id}/events?limit=20`),
    ]);
    setSelected(current);
    setLimit(String(current.limit_tokens));
    setCalls(rows);
    setEvents(journal);
    setOffset(start);
  }
  async function run(action: () => Promise<void>) {
    setPending(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setPending(false);
    }
  }
  useEffect(() => {
    void run(list);
    const onExpired = () => setExpired(true);
    window.addEventListener("novelos-session-expired", onExpired);
    return () =>
      window.removeEventListener("novelos-session-expired", onExpired);
  }, []);
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

  function create(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      const limit_tokens = tokenValue(initial);
      if (!requestKey.current) requestKey.current = crypto.randomUUID();
      const result = await platformRequest<{ id: string }>("/quotas", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          timezone: zone,
          limit_tokens,
          request_key: requestKey.current,
        }),
      });
      requestKey.current = "";
      setName("");
      setInitial("");
      await list();
      await load(result.id);
      setNotice("月度配额已创建；请绑定尚未调用的模型执行者。");
    });
  }
  return (
    <main className={shell.shell}>
      <header className={shell.header}>
        <div className={shell.wordmark}>
          NovelOS
          <span>月度配额</span>
        </div>
        <a href="/">返回控制台</a>
      </header>
      <h1>月度 token 配额</h1>
      <p>
        按账户或服务建立共享额度，把使用同一额度的执行者绑定在一起。此处记录平台调用的输入与输出
        token 总和，不代表供应商剩余额度。
      </p>
      <p>
        真实调用保持关闭。任务预算与作品预算仍同时生效，不会在免费额度耗尽后自动转为付费。
      </p>
      {expired && (
        <p role="alert">
          <a href="/">登录已过期，请返回控制台登录。</a>
        </p>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className={styles.notice} role="status">
          {notice}
        </p>
      )}
      <form
        className={styles.panel}
        onSubmit={create}
        aria-label="新建月度配额"
      >
        <h2>新建共享配额</h2>
        <div className={styles.fields}>
          <label>
            配额名称
            <input
              required
              maxLength={128}
              disabled={busy}
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                requestKey.current = "";
              }}
            />
          </label>
          <label>
            月度 token 上限
            <input
              required
              inputMode="numeric"
              disabled={busy}
              value={initial}
              onChange={(e) => {
                setInitial(e.target.value);
                requestKey.current = "";
              }}
            />
          </label>
          <label>
            月界时区
            <select
              aria-label="月界时区"
              disabled={busy}
              value={zone}
              onChange={(e) => {
                setZone(e.target.value);
                requestKey.current = "";
              }}
            >
              <option value="Asia/Shanghai">北京时间（UTC+08:00）</option>
              <option value="UTC">UTC</option>
            </select>
          </label>
        </div>
        <p>
          按所选时区的自然月计算，时区创建后固定。额度由你核对后填写，平台不预设朱雀额度，也不获取第三方账单。
        </p>
        <div className={styles.actions}>
          <button disabled={busy || !name.trim() || !initial}>
            创建月度配额
          </button>
        </div>
      </form>
      <label className={styles.work}>
        当前月度配额
        <select
          aria-label="当前月度配额"
          disabled={busy}
          value={selected?.id || ""}
          onChange={(e) => {
            const id = e.target.value;
            if (!id) return;
            if (
              selected &&
              limit !== String(selected.limit_tokens) &&
              !window.confirm("切换会丢弃尚未保存的配额上限，继续？")
            )
              return;
            setExecutorId("");
            void run(() => load(id));
          }}
        >
          <option value="">选择配额</option>
          {quotas.map((q) => (
            <option key={q.id} value={q.id}>
              {q.name}
            </option>
          ))}
        </select>
      </label>
      {selected && (
        <>
          <section className={styles.panel} aria-label="月度配额概况">
            <h2>{selected.name}</h2>
            <p>
              月份 {selected.period} · {selected.timezone} · 版本{" "}
              {selected.version}
            </p>
            <p>
              已结算 {selected.spent_tokens} token · 占用 {selected.held_tokens}{" "}
              token · 可用 {selected.remaining_tokens} token
            </p>
            <p>
              占用包括所有月份尚未确认的调用；结算按预留月份归属。跨月不会释放未知用量，旧月份的未发送预留须释放后重新领取。
            </p>
            {selected.warning && (
              <p role="status" className={styles.notice}>
                用量及占用已达到上限的 80%，请核对后续任务。
              </p>
            )}
            {selected.over_limit && (
              <p role="status" className={styles.error}>
                当前用量及占用超过上限，新的调用已被阻止。
              </p>
            )}
            {selected.unknown_count > 0 && (
              <p>
                {selected.unknown_count}{" "}
                笔未知用量阻止共享配额内的新调用，请在对应作品的预算中心核对。
              </p>
            )}
            <label>
              调整月度 token 上限
              <input
                disabled={busy}
                inputMode="numeric"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
              />
            </label>
            <div className={styles.actions}>
              <button
                disabled={busy || !limit}
                onClick={() =>
                  void run(async () => {
                    await platformRequest(`/quotas/${selected.id}`, {
                      method: "PUT",
                      body: JSON.stringify({
                        expected_version: selected.version,
                        limit_tokens: tokenValue(limit),
                      }),
                    });
                    await list();
                    await load(selected.id);
                    setNotice("配额已保存；暂停任务仍需手动恢复。");
                  })
                }
              >
                保存配额上限
              </button>
              <button
                disabled={busy}
                onClick={() => void run(() => load(selected.id))}
              >
                载入最新配额
              </button>
            </div>
            <p>
              降低上限不会删除账目或撤回已发送请求。页面不自动覆盖正在编辑的上限；冲突时保留输入。
            </p>
          </section>
          <section className={styles.panel} aria-label="共享执行者">
            <h2>共享此配额的执行者</h2>
            {selected.executor_ids.length ? (
              <ul>
                {selected.executor_ids.map((id) => (
                  <li key={id}>
                    {executors.find((e) => e.id === id)?.name || id}
                  </li>
                ))}
              </ul>
            ) : (
              <p>尚未绑定执行者，配额不会约束任何调用。</p>
            )}
            <p>
              仅支持已配置模型的 API
              执行者，须在第一次调用前绑定。归属固定，暂不支持解除或改绑；请核对其实际共享的账户额度。
            </p>
            <label>
              待绑定执行者
              <select
                aria-label="待绑定执行者"
                value={executorId}
                disabled={busy}
                onChange={(e) => setExecutorId(e.target.value)}
              >
                <option value="">选择模型执行者</option>
                {executors
                  .filter(
                    (e) =>
                      e.mode === "api" &&
                      e.api_config &&
                      !quotas.some((q) => q.executor_ids.includes(e.id)),
                  )
                  .map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.name}
                    </option>
                  ))}
              </select>
            </label>
            <div className={styles.actions}>
              <button
                disabled={
                  busy || !executorId || limit !== String(selected.limit_tokens)
                }
                onClick={() =>
                  void run(async () => {
                    await platformRequest(`/quotas/${selected.id}/bindings`, {
                      method: "POST",
                      body: JSON.stringify({
                        expected_version: selected.version,
                        executor_id: executorId,
                      }),
                    });
                    setExecutorId("");
                    await list();
                    await load(selected.id);
                    setNotice("执行者已绑定，共享额度开始生效。");
                  })
                }
              >
                绑定执行者
              </button>
            </div>
          </section>
          <section className={styles.panel} aria-label="月度调用记录">
            <h2>调用归属与用量</h2>
            <a href="/budgets">前往预算中心核对对应作品</a>
            {calls.length === 0 ? (
              <p>暂无调用记录。</p>
            ) : (
              calls.map((call) => (
                <div className={styles.record} key={call.id}>
                  <strong>
                    {call.work_title} · {call.period}
                  </strong>
                  <p>
                    {stateNames[call.state] || call.state} · 预留{" "}
                    {call.reserved_tokens} token
                    {call.state === "SETTLED"
                      ? ` · 结算 ${(call.input_tokens || 0) + (call.output_tokens || 0)} token`
                      : ""}
                  </p>
                </div>
              ))
            )}
            <div className={styles.actions}>
              <button
                disabled={
                  busy ||
                  offset === 0 ||
                  limit !== String(selected.limit_tokens)
                }
                onClick={() =>
                  void run(() => load(selected.id, Math.max(0, offset - 20)))
                }
              >
                上一页
              </button>
              <button
                disabled={
                  busy ||
                  calls.length < 20 ||
                  limit !== String(selected.limit_tokens)
                }
                onClick={() => void run(() => load(selected.id, offset + 20))}
              >
                下一页
              </button>
            </div>
          </section>
          <section className={styles.panel} aria-label="配额配置历史">
            <h2>最近 20 条配置记录</h2>
            {events.map((entry) => (
              <div className={styles.record} key={entry.id}>
                <strong>
                  {entry.action === "quota.created"
                    ? "创建配额"
                    : entry.action === "quota.bound"
                      ? "绑定执行者"
                      : "调整上限"}
                </strong>
                <p>
                  {new Date(entry.created_at).toLocaleString()}
                  {entry.details.after !== undefined
                    ? ` · ${entry.details.before} → ${entry.details.after} token`
                    : entry.details.executor_id
                      ? ` · ${executors.find((e) => e.id === entry.details.executor_id)?.name || entry.details.executor_id}`
                      : ` · ${entry.details.limit_tokens} token`}
                </p>
              </div>
            ))}
          </section>
        </>
      )}
    </main>
  );
}
