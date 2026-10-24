"use client";

import { FormEvent, useEffect, useState } from "react";
import { platformRequest } from "@/lib/platform";
import shell from "../platform.module.css";
import styles from "./budgets.module.css";

type Work = { id: string; title: string };
type Budget = {
  configured: boolean;
  version: number;
  currency?: string;
  limit_microunits?: number;
  limit_tokens?: number;
  spent_microunits?: number;
  held_microunits?: number;
  available_microunits?: number;
  spent_tokens?: number;
  held_tokens?: number;
  available_tokens?: number;
  over_limit_microunits?: number;
  over_limit_tokens?: number;
  unknown_count?: number;
};
type Reservation = {
  id: string;
  task_id: string;
  executor_id: string;
  state: string;
  version: number;
  currency: string;
  reserved_microunits: number;
  reserved_tokens: number;
  actual_microunits: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  receipt_key: string | null;
};
type Entry = {
  id: string;
  kind: string;
  actor: string;
  created_at: string;
  details: { evidence?: string; over_reservation?: boolean; balance: Budget };
};
const names: Record<string, string> = {
  CONFIGURED: "设置预算",
  RESERVED: "已预留",
  DISPATCHED: "已登记发起",
  UNKNOWN: "费用待确认",
  SETTLED: "已结算",
  RELEASED: "已释放",
};
function amount(value = 0) {
  return (value / 1000000).toFixed(6).replace(/\.?0+$/, "") || "0";
}
function micros(value: string) {
  if (!/^\d{1,7}(\.\d{1,6})?$/.test(value))
    throw new Error("金额须为非负数，最多六位小数。");
  const [whole, fraction = ""] = value.split(".");
  const result = Number(whole) * 1000000 + Number(fraction.padEnd(6, "0"));
  if (result > 10 ** 12) throw new Error("金额不能超过 1,000,000。");
  return result;
}
function tokens(value: string) {
  if (!/^\d{1,13}$/.test(value) || Number(value) > 10 ** 12)
    throw new Error("token 数须为 0 至 1,000,000,000,000 的整数。");
  return Number(value);
}

export default function BudgetsPage() {
  const [works, setWorks] = useState<Work[]>([]);
  const [workId, setWorkId] = useState("");
  const [budget, setBudget] = useState<Budget | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [reservations, setReservations] = useState<Reservation[]>([]);
  const [offset, setOffset] = useState(0);
  const [unknownOnly, setUnknownOnly] = useState(false);
  const [currency, setCurrency] = useState("CNY");
  const [limit, setLimit] = useState("0");
  const [tokenLimit, setTokenLimit] = useState("0");
  const [selected, setSelected] = useState<Reservation | null>(null);
  const [actual, setActual] = useState("");
  const [inputTokens, setInputTokens] = useState("");
  const [outputTokens, setOutputTokens] = useState("");
  const [receipt, setReceipt] = useState("");
  const [evidence, setEvidence] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [ready, setReady] = useState(false);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const busy = pending || !ready || expired;
  const dirty =
    !!budget &&
    (currency !== (budget.currency || "CNY") ||
      limit !== amount(budget.limit_microunits) ||
      tokenLimit !== String(budget.limit_tokens || 0) ||
      (!!selected &&
        !!(actual || inputTokens || outputTokens || receipt || evidence)));

  useEffect(() => {
    let active = true;
    (async () => {
      const found: Work[] = [];
      for (let start = 0; ; start += 100) {
        const page = await platformRequest<Work[]>(
          `/works?offset=${start}&limit=100`,
        );
        found.push(...page);
        if (page.length < 100) break;
      }
      if (active) setWorks(found);
    })()
      .catch((err) => {
        if (active) setError(err.message);
      })
      .finally(() => {
        if (active) setReady(true);
      });
    const onExpired = () => setExpired(true);
    window.addEventListener("novelos-session-expired", onExpired);
    return () => {
      active = false;
      window.removeEventListener("novelos-session-expired", onExpired);
    };
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
  async function load(
    id: string,
    start = 0,
    onlyUnknown = unknownOnly,
    populate = false,
  ) {
    const base = `/works/${id}/budget`;
    const [current, rows, journal] = await Promise.all([
      platformRequest<Budget>(base),
      platformRequest<Reservation[]>(
        `${base}/reservations?offset=${start}&limit=20&unknown_only=${onlyUnknown}`,
      ),
      platformRequest<Entry[]>(`${base}/events?limit=20`),
    ]);
    setWorkId(id);
    setBudget(current);
    setReservations(rows);
    setEntries(journal);
    setOffset(start);
    setUnknownOnly(onlyUnknown);
    if (populate) {
      setCurrency(current.currency || "CNY");
      setLimit(amount(current.limit_microunits));
      setTokenLimit(String(current.limit_tokens || 0));
      if (id !== workId) setSelected(null);
    }
  }
  function mayLeave() {
    return !dirty || window.confirm("有未保存的预算或对账输入，确认放弃吗？");
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!budget) return;
    await run(async () => {
      await platformRequest(`/works/${workId}/budget`, {
        method: "PUT",
        body: JSON.stringify({
          expected_version: budget.version,
          currency,
          limit_microunits: micros(limit),
          limit_tokens: tokens(tokenLimit),
        }),
      });
      await load(workId, offset, unknownOnly, true);
      setNotice("预算已保存。设置额度不会启用外部调用。");
    });
  }
  async function reconcile(event: FormEvent) {
    event.preventDefault();
    if (!selected || !confirmed) return;
    await run(async () => {
      await platformRequest(
        `/works/${workId}/budget/reservations/${selected.id}/reconcile`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_version: selected.version,
            receipt_key: receipt,
            actual_microunits: micros(actual),
            input_tokens: tokens(inputTokens),
            output_tokens: tokens(outputTokens),
            evidence,
          }),
        },
      );
      setSelected(null);
      await load(workId, offset);
      setNotice("对账记录已保存。任务不会自动恢复，候选结果和正文不会改变。");
    });
  }

  return (
    <main className={shell.shell}>
      <header className={shell.header}>
        <div>
          <strong>NovelOS</strong>
          <p>预算中心 · 记录额度、占用与实际用量</p>
        </div>
        <a
          href="/"
          onClick={(event) => {
            if (!mayLeave()) event.preventDefault();
          }}
        >
          返回控制台
        </a>
      </header>
      <h1>作品预算</h1>
      <p className={styles.muted}>
        预算按作品累计，费用和 token 双重限制，不自动按月清零。真实模型与外部
        Agent 尚未启用。
      </p>
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className={styles.notice}>
          {notice}
        </p>
      )}
      {expired && (
        <p role="alert">
          会话已过期，当前输入保留。请在另一个页面登录后重新打开预算中心。
        </p>
      )}
      <label className={styles.work}>
        当前作品
        <select
          disabled={busy}
          value={workId}
          onChange={(event) => {
            if (mayLeave())
              void run(() => load(event.target.value, 0, false, true));
          }}
        >
          <option value="" disabled>
            选择作品
          </option>
          {works.map((work) => (
            <option key={work.id} value={work.id}>
              {work.title}
            </option>
          ))}
        </select>
      </label>
      {ready && !works.length && !expired && (
        <p>
          尚无可用作品。请先<a href="/works">登录并创建作品</a>。
        </p>
      )}
      {budget && (
        <>
          <section className={styles.panel} aria-label="预算概况">
            <h2>当前额度</h2>
            {!budget.configured ? (
              <p>
                尚未配置预算，外部调用额度默认关闭。人工与本地校验不消耗此额度。
              </p>
            ) : (
              <>
                <p>
                  币种：{budget.currency} · 作品累计 · 版本 {budget.version}
                </p>
                <div className={styles.tableWrap}>
                  <table>
                    <thead>
                      <tr>
                        <th>项目</th>
                        <th>上限</th>
                        <th>已结算</th>
                        <th>预留中</th>
                        <th>可用</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        <th>费用（{budget.currency}）</th>
                        <td>{amount(budget.limit_microunits)}</td>
                        <td>{amount(budget.spent_microunits)}</td>
                        <td>{amount(budget.held_microunits)}</td>
                        <td>{amount(budget.available_microunits)}</td>
                      </tr>
                      <tr>
                        <th>token</th>
                        <td>{budget.limit_tokens}</td>
                        <td>{budget.spent_tokens}</td>
                        <td>{budget.held_tokens}</td>
                        <td>{budget.available_tokens}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                {!!budget.unknown_count && (
                  <p className={styles.error}>
                    有 {budget.unknown_count}{" "}
                    笔费用待确认，预留额度继续占用，新增外部调用被阻止。
                  </p>
                )}
                {!!(
                  budget.over_limit_microunits || budget.over_limit_tokens
                ) && (
                  <p className={styles.error}>
                    已结算和占用超过上限：{amount(budget.over_limit_microunits)}{" "}
                    {budget.currency}，{budget.over_limit_tokens}{" "}
                    token。账目保留，新增调用被阻止。
                  </p>
                )}
              </>
            )}
            <button
              disabled={busy}
              onClick={() => void run(() => load(workId, offset))}
            >
              刷新账目
            </button>
          </section>
          <form className={styles.panel} onSubmit={save}>
            <h2>设置累计上限</h2>
            <div className={styles.fields}>
              <label>
                币种
                <select
                  value={currency}
                  disabled={busy || budget.configured}
                  onChange={(event) => setCurrency(event.target.value)}
                >
                  <option value="CNY">人民币 CNY</option>
                  <option value="USD">美元 USD</option>
                </select>
              </label>
              <label>
                费用上限
                <input
                  required
                  inputMode="decimal"
                  value={limit}
                  disabled={busy}
                  onChange={(event) => setLimit(event.target.value)}
                />
              </label>
              <label>
                token 上限
                <input
                  required
                  inputMode="numeric"
                  value={tokenLimit}
                  disabled={busy}
                  onChange={(event) => setTokenLimit(event.target.value)}
                />
              </label>
            </div>
            <p className={styles.muted}>
              币种保存后固定；金额最多六位小数。降低上限不会抹掉已发生费用或释放未确认的调用。本设置不等于供应商免费额度，也不启用付费服务。
            </p>
            <button disabled={busy} type="submit">
              保存预算
            </button>
          </form>
          <section className={styles.panel} aria-label="调用账目">
            <h2>调用账目</h2>
            <label className={styles.check}>
              <input
                type="checkbox"
                checked={unknownOnly}
                disabled={busy}
                onChange={(event) =>
                  void run(() => load(workId, 0, event.target.checked))
                }
              />
              只看待对账
            </label>
            {!reservations.length && (
              <p>没有调用记录。人工与本地校验不记为模型消费。</p>
            )}
            {reservations.map((item) => (
              <article className={styles.record} key={item.id}>
                <strong>
                  {names[item.state]} · {item.executor_id}
                </strong>
                <p className={styles.muted}>任务 {item.task_id}</p>
                <p>
                  预留 {amount(item.reserved_microunits)} {item.currency} /{" "}
                  {item.reserved_tokens} token
                </p>
                {item.actual_microunits !== null && (
                  <p>
                    结算 {amount(item.actual_microunits)} {item.currency} /{" "}
                    {(item.input_tokens || 0) + (item.output_tokens || 0)} token
                    · 回执 {item.receipt_key}
                  </p>
                )}
                {item.state === "UNKNOWN" && (
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (selected && !mayLeave()) return;
                      setSelected(item);
                      setActual("");
                      setInputTokens("");
                      setOutputTokens("");
                      setReceipt("");
                      setEvidence("");
                      setConfirmed(false);
                    }}
                  >
                    核对这笔调用
                  </button>
                )}
              </article>
            ))}
            <div className={styles.actions}>
              <button
                disabled={busy || offset === 0}
                onClick={() =>
                  void run(() => load(workId, Math.max(0, offset - 20)))
                }
              >
                上一页
              </button>
              <span>第 {offset / 20 + 1} 页</span>
              <button
                disabled={busy || reservations.length < 20}
                onClick={() => void run(() => load(workId, offset + 20))}
              >
                下一页
              </button>
            </div>
          </section>
          {selected && (
            <form className={styles.panel} onSubmit={reconcile}>
              <h2>核对未知费用</h2>
              <p>
                请依据供应商回执填写。无法确定时保持待对账状态；不要把未知费用填写为零。此操作只修正账目，不恢复任务。
              </p>
              <div className={styles.fields}>
                <label>
                  实际费用（{selected.currency}）
                  <input
                    required
                    value={actual}
                    inputMode="decimal"
                    disabled={busy}
                    onChange={(event) => setActual(event.target.value)}
                  />
                </label>
                <label>
                  输入 token
                  <input
                    required
                    value={inputTokens}
                    inputMode="numeric"
                    disabled={busy}
                    onChange={(event) => setInputTokens(event.target.value)}
                  />
                </label>
                <label>
                  输出 token
                  <input
                    required
                    value={outputTokens}
                    inputMode="numeric"
                    disabled={busy}
                    onChange={(event) => setOutputTokens(event.target.value)}
                  />
                </label>
              </div>
              <label>
                供应商回执标识
                <input
                  required
                  maxLength={128}
                  value={receipt}
                  disabled={busy}
                  onChange={(event) => setReceipt(event.target.value)}
                />
              </label>
              <label>
                核对依据
                <textarea
                  required
                  maxLength={2000}
                  value={evidence}
                  disabled={busy}
                  onChange={(event) => setEvidence(event.target.value)}
                />
              </label>
              <label className={styles.check}>
                <input
                  type="checkbox"
                  required
                  checked={confirmed}
                  disabled={busy}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                我已核对费用和 token 用量，并确认依据完整
              </label>
              <button type="submit" disabled={busy || !confirmed}>
                保存对账结果
              </button>
            </form>
          )}
          <section className={styles.panel}>
            <h2>最近 20 条账本记录</h2>
            {entries.length === 0 && <p>暂无记录。</p>}
            {entries.map((entry) => (
              <article key={entry.id} className={styles.record}>
                <strong>
                  {names[entry.kind]} ·{" "}
                  {entry.actor === "owner" ? "用户操作" : "执行记录"}
                </strong>
                <p>{new Date(entry.created_at).toLocaleString()}</p>
                <p>
                  费用上限 {amount(entry.details.balance.limit_microunits)}{" "}
                  {entry.details.balance.currency} · token 上限{" "}
                  {entry.details.balance.limit_tokens}
                </p>
                <p>
                  结算 {amount(entry.details.balance.spent_microunits)} / 占用{" "}
                  {amount(entry.details.balance.held_microunits)}{" "}
                  {entry.details.balance.currency}
                </p>
                {entry.details.evidence && (
                  <p>依据：{entry.details.evidence}</p>
                )}
                {entry.details.over_reservation && (
                  <p className={styles.error}>实际用量超出预留，已如实记账。</p>
                )}
              </article>
            ))}
          </section>
        </>
      )}
    </main>
  );
}
