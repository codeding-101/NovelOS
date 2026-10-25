"use client";
import { useEffect, useState } from "react";
import { platformRequest } from "@/lib/platform";
import styles from "../budgets/budgets.module.css";

export type TaskSpendingBudget = {
  supported: boolean;
  configured: boolean;
  currency: string | null;
  limit_microunits: number | null;
  limit_tokens: number | null;
  spent_microunits: number;
  spent_tokens: number;
  held_microunits: number;
  held_tokens: number;
  remaining_microunits: number | null;
  remaining_tokens: number | null;
  unknown_count: number;
  over_limit: boolean;
};

export function parseMoney(value: string) {
  if (!/^\d{1,7}(\.\d{1,6})?$/.test(value))
    throw new Error("费用上限须为非负数，最多六位小数。");
  const [whole, fraction = ""] = value.split(".");
  const amount = Number(whole) * 1000000 + Number(fraction.padEnd(6, "0"));
  if (amount > 10 ** 12) throw new Error("费用上限超出支持范围。");
  return amount;
}

function money(value: number | null) {
  return value === null ? "" : (value / 1000000).toFixed(6);
}

export default function TaskBudget({
  task,
  refresh,
}: {
  task: {
    id: string;
    version: number;
    state: string;
    spending_budget: TaskSpendingBudget;
  };
  refresh: () => Promise<void>;
}) {
  const budget = task.spending_budget;
  const [amount, setAmount] = useState(money(budget.limit_microunits));
  const [tokens, setTokens] = useState(
    budget.limit_tokens === null ? "" : String(budget.limit_tokens),
  );
  const [version, setVersion] = useState(task.version);
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const terminal = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(task.state);
  useEffect(() => {
    if (!dirty) {
      setAmount(money(budget.limit_microunits));
      setTokens(
        budget.limit_tokens === null ? "" : String(budget.limit_tokens),
      );
      setVersion(task.version);
    }
  }, [budget.limit_microunits, budget.limit_tokens, task.version, dirty]);
  async function save() {
    setPending(true);
    setError("");
    setNotice("");
    try {
      if (!/^\d+$/.test(tokens) || Number(tokens) > 10 ** 12)
        throw new Error("token 上限须为支持范围内的非负整数。");
      await platformRequest(`/tasks/${task.id}/budget`, {
        method: "PUT",
        body: JSON.stringify({
          expected_version: version,
          currency: budget.currency,
          limit_microunits: parseMoney(amount),
          limit_tokens: Number(tokens),
        }),
      });
      setDirty(false);
      await refresh();
      setNotice("任务预算已保存；暂停任务仍需手动恢复。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setPending(false);
    }
  }
  return (
    <section className={styles.panel} aria-label="任务累计预算">
      <h3>任务累计预算</h3>
      <p>
        累计计算所有尝试。作品总预算也同时生效；调整上限不能撤回已发出的请求，未知费用继续占用额度。
      </p>
      {!budget.configured && <p>此任务尚未设置独立预算，模型调用保持阻断。</p>}
      <p>
        币种：{budget.currency} · 已结算 {money(budget.spent_microunits)} /{" "}
        {budget.spent_tokens} token · 占用 {money(budget.held_microunits)} /{" "}
        {budget.held_tokens} token
      </p>
      {budget.configured && (
        <p>
          可用 {money(budget.remaining_microunits)} / {budget.remaining_tokens}{" "}
          token
        </p>
      )}
      {budget.over_limit && (
        <p role="status">已结算和占用超过任务上限，后续调用被阻止。</p>
      )}
      {budget.unknown_count > 0 && (
        <p>{budget.unknown_count} 笔未知费用须先在预算中心核对。</p>
      )}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <div className={styles.fields}>
        <label>
          任务累计费用上限（{budget.currency}）
          <input
            inputMode="decimal"
            disabled={pending || terminal}
            value={amount}
            onChange={(event) => {
              setAmount(event.target.value);
              setDirty(true);
            }}
          />
        </label>
        <label>
          任务累计 token 上限
          <input
            inputMode="numeric"
            disabled={pending || terminal}
            value={tokens}
            onChange={(event) => {
              setTokens(event.target.value);
              setDirty(true);
            }}
          />
        </label>
      </div>
      <div className={styles.actions}>
        <button
          disabled={pending || terminal || !amount || !tokens}
          onClick={() => void save()}
        >
          保存任务预算
        </button>
        <button
          disabled={pending}
          onClick={() => {
            setDirty(false);
            setError("");
            setNotice("");
            void refresh().catch((err) => setError(err.message));
          }}
        >
          载入最新任务预算
        </button>
      </div>
    </section>
  );
}
