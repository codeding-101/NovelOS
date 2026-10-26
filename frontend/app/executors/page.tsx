"use client";
import { FormEvent, useEffect, useRef, useState } from "react";
import { platformRequest, Credential } from "@/lib/platform";
import shell from "../platform.module.css";
import styles from "../budgets/budgets.module.css";

type Executor = {
  api_config: {
    endpoint: string;
    model: string;
    max_output_tokens: number;
    timeout_seconds: number;
  } | null;
  agent_protocol: string | null;
  id: string;
  mode: string;
  version: number;
  name: string;
  capabilities: string[];
  enabled: boolean;
  available: boolean;
  priority: number;
  currency: string | null;
  input_rate: number | null;
  output_rate: number | null;
  context_tokens: number | null;
  credential_id: string | null;
  adapter_ready: boolean;
};
type Form = {
  apiProfile: boolean;
  endpoint: string;
  model: string;
  maxOutput: number;
  timeout: number;
  agentProtocol: boolean;
  name: string;
  mode: string;
  capabilities: string[];
  enabled: boolean;
  available: boolean;
  priority: number;
  currency: string;
  inputPrice: string;
  outputPrice: string;
  context: string;
  credential: string;
};
type Policy = {
  version: number;
  allowed_ids: string[];
  preferred_ids: string[];
  auto_local_diagnostics: boolean;
};
type Route = {
  selected_id: string | null;
  policy_version: number;
  candidates: {
    agent_protocol: string | null;
    id: string;
    name: string;
    mode: string;
    reasons: string[];
    estimated_microunits: number | null;
    currency: string | null;
    estimated_tokens: number;
  }[];
};
const types: Record<string, string> = {
  analyze: "分析",
  draft: "创作",
  revise: "修改",
  review: "审查",
  research: "研究",
};
const reasons: Record<string, string> = {
  NOT_ALLOWED: "作品规则不允许",
  DISABLED: "已停用",
  UNAVAILABLE: "标记为不可用",
  CAPABILITY_MISMATCH: "不支持任务类型",
  DIAGNOSTIC_OPT_IN_REQUIRED: "未允许自动使用本地诊断",
  ADAPTER_NOT_READY: "外部适配器尚未开放",
  MODEL_API_DISABLED: "真实模型调用未开放，当前只允许测试环境的本机模拟服务",
  CREDENTIAL_MISSING: "缺少匹配类型的凭证",
  CONTEXT_LIMIT_UNKNOWN: "尚未声明上下文上限",
  CONTEXT_EXCEEDED: "输入与最大输出超过上下文上限",
  PRICE_UNKNOWN: "尚未填写完整价格",
  BUDGET_MISSING: "作品尚未设置预算",
  BUDGET_UNCERTAIN: "作品有未知费用待对账",
  CURRENCY_MISMATCH: "与预算币种不一致",
  MONEY_LIMIT: "费用额度不足",
  TOKEN_LIMIT: "token 额度不足",
  MONTHLY_QUOTA_EXCEEDED: "共享月度配额不足或有待对账用量",
};
const empty: Form = {
  apiProfile: false,
  endpoint: "",
  model: "",
  maxOutput: 2048,
  timeout: 30,
  agentProtocol: false,
  name: "",
  mode: "api",
  capabilities: ["analyze"],
  enabled: false,
  available: true,
  priority: 0,
  currency: "CNY",
  inputPrice: "",
  outputPrice: "",
  context: "",
  credential: "",
};
function price(value: number | null) {
  return value === null
    ? ""
    : (value / 1000000).toFixed(6).replace(/\.?0+$/, "") || "0";
}
function units(value: string) {
  if (!/^\d{1,7}(\.\d{1,6})?$/.test(value))
    throw new Error("价格须为非负数，最多六位小数。");
  const [whole, fraction = ""] = value.split(".");
  const result = Number(whole) * 1000000 + Number(fraction.padEnd(6, "0"));
  if (result > 10 ** 12) throw new Error("价格超出支持范围。");
  return result;
}
function fromExecutor(item: Executor): Form {
  return {
    apiProfile: !!item.api_config,
    endpoint: item.api_config?.endpoint || "",
    model: item.api_config?.model || "",
    maxOutput: item.api_config?.max_output_tokens || 2048,
    timeout: item.api_config?.timeout_seconds || 30,
    agentProtocol: item.agent_protocol === "offline_v1",
    name: item.name,
    mode: item.mode,
    capabilities: item.capabilities,
    enabled: item.enabled,
    available: item.available,
    priority: item.priority,
    currency: item.currency || "CNY",
    inputPrice: price(item.input_rate),
    outputPrice: price(item.output_rate),
    context: item.context_tokens === null ? "" : String(item.context_tokens),
    credential: item.credential_id || "",
  };
}

export default function ExecutorsPage() {
  const [executors, setExecutors] = useState<Executor[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [works, setWorks] = useState<{ id: string; title: string }[]>([]);
  const [editing, setEditing] = useState<Executor | null>(null);
  const [form, setForm] = useState<Form>(empty);
  const [savedForm, setSavedForm] = useState(JSON.stringify(empty));
  const [workId, setWorkId] = useState("");
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [savedPolicy, setSavedPolicy] = useState("");
  const [taskType, setTaskType] = useState("analyze");
  const [input, setInput] = useState(1000);
  const [output, setOutput] = useState(1000);
  const [route, setRoute] = useState<Route | null>(null);
  const [ready, setReady] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [expired, setExpired] = useState(false);
  const requestKey = useRef({ signature: "", key: "" });
  const dirty =
    JSON.stringify(form) !== savedForm ||
    (policy !== null && JSON.stringify(policy) !== savedPolicy);
  const busy = pending || !ready || expired;
  const builtin = form.mode === "human" || form.mode === "local_test";
  function field<K extends keyof Form>(key: K, value: Form[K]) {
    setForm((old) => ({ ...old, [key]: value }));
  }

  async function refreshRegistry() {
    const [items, keys] = await Promise.all([
      platformRequest<Executor[]>("/executors"),
      platformRequest<Credential[]>("/credentials"),
    ]);
    setExecutors(items);
    setCredentials(keys);
    return items;
  }
  useEffect(() => {
    (async () => {
      await refreshRegistry();
      const list: { id: string; title: string }[] = [];
      for (let start = 0; ; start += 100) {
        const page = await platformRequest<{ id: string; title: string }[]>(
          `/works?offset=${start}&limit=100`,
        );
        list.push(...page);
        if (page.length < 100) break;
      }
      setWorks(list);
    })()
      .catch((err) => setError(err.message))
      .finally(() => setReady(true));
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
  function mayLeave() {
    return !dirty || window.confirm("配置有未保存的修改，确认放弃吗？");
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
  function edit(item: Executor | null) {
    const value = item ? fromExecutor(item) : empty;
    setEditing(item);
    setForm(value);
    setSavedForm(JSON.stringify(value));
  }
  async function loadPolicy(id: string) {
    const value = await platformRequest<Policy>(
      `/works/${id}/execution-policy`,
    );
    setWorkId(id);
    setPolicy(value);
    setSavedPolicy(JSON.stringify(value));
    setRoute(null);
  }
  async function saveExecutor(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      const hasPrice = !!(form.inputPrice || form.outputPrice);
      const values = {
        api_config:
          form.mode === "api" && form.apiProfile
            ? {
                endpoint: form.endpoint,
                model: form.model,
                max_output_tokens: form.maxOutput,
                timeout_seconds: form.timeout,
              }
            : null,
        agent_protocol:
          form.mode === "agent" && form.agentProtocol ? "offline_v1" : null,
        name: form.name,
        capabilities: form.capabilities,
        enabled: form.enabled,
        available: form.available,
        priority: form.priority,
        currency:
          builtin || form.agentProtocol || !hasPrice ? null : form.currency,
        input_rate:
          builtin || form.agentProtocol || !hasPrice
            ? null
            : units(form.inputPrice),
        output_rate:
          builtin || form.agentProtocol || !hasPrice
            ? null
            : units(form.outputPrice),
        context_tokens: builtin || !form.context ? null : Number(form.context),
        credential_id:
          builtin || form.agentProtocol || !form.credential
            ? null
            : form.credential,
      };
      let id = editing?.id;
      if (editing)
        await platformRequest(`/executors/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify({
            ...values,
            expected_version: editing.version,
          }),
        });
      else {
        const signature = JSON.stringify({ ...values, mode: form.mode });
        if (requestKey.current.signature !== signature)
          requestKey.current = { signature, key: crypto.randomUUID() };
        const created = await platformRequest<{ id: string }>("/executors", {
          method: "POST",
          body: JSON.stringify({
            ...values,
            mode: form.mode,
            request_key: requestKey.current.key,
          }),
        });
        id = created.id;
      }
      const items = await refreshRegistry();
      edit(items.find((item) => item.id === id) || null);
      setRoute(null);
      requestKey.current = { signature: "", key: "" };
      setNotice(
        "执行者配置已保存。真实模型调用仍关闭，测试环境仅允许本机模拟服务。",
      );
    });
  }
  async function savePolicy(event: FormEvent) {
    event.preventDefault();
    if (!policy) return;
    await run(async () => {
      await platformRequest(`/works/${workId}/execution-policy`, {
        method: "PUT",
        body: JSON.stringify({
          expected_version: policy.version,
          allowed_ids: policy.allowed_ids,
          preferred_ids: policy.preferred_ids,
          auto_local_diagnostics: policy.auto_local_diagnostics,
        }),
      });
      await loadPolicy(workId);
      setNotice("作品路由规则已保存，排队任务领取时会重新检查。");
    });
  }
  function movePreferred(index: number, delta: number) {
    if (!policy) return;
    const ids = [...policy.preferred_ids];
    [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
    setPolicy({ ...policy, preferred_ids: ids });
  }

  return (
    <main className={shell.shell}>
      <header className={shell.header}>
        <div>
          <strong>NovelOS</strong>
          <p>执行者登记与作品路由</p>
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
      <h1>执行者与路由</h1>
      <p>
        为任务声明能力、可用状态和选择顺序。支持人工、本地校验和离线 Agent；模型
        API 仍只登记和检查配置。
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
        <p role="alert">会话已过期，输入仍保留。请重新登录后读取最新设置。</p>
      )}
      <section className={styles.panel} aria-label="执行者列表">
        <h2>已登记执行者</h2>
        <div className={styles.actions}>
          <button
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await refreshRegistry();
                setNotice(
                  "列表已刷新，编辑输入保持不变。选择条目可载入最新配置。",
                );
              })
            }
          >
            刷新执行者列表
          </button>
          <button
            disabled={busy}
            onClick={() => {
              if (
                JSON.stringify(form) === savedForm ||
                window.confirm("放弃未保存的执行者配置？")
              )
                edit(null);
            }}
          >
            新增外部执行者
          </button>
        </div>
        {executors.map((item) => (
          <article className={styles.record} key={item.id}>
            <strong>{item.name}</strong>
            <p>
              {item.adapter_ready ? "可执行类型" : "登记阶段，适配器未开放"} ·{" "}
              {item.enabled ? "规则中启用" : "已停用"} ·{" "}
              {item.available ? "用户标记可用" : "用户标记不可用"}
            </p>
            <p>
              能力：{item.capabilities.map((value) => types[value]).join("、")}{" "}
              · 优先级 {item.priority}
            </p>
            <button
              disabled={busy}
              onClick={() => {
                if (
                  JSON.stringify(form) === savedForm ||
                  window.confirm("放弃未保存的执行者配置？")
                )
                  edit(item);
              }}
            >
              编辑 {item.name}
            </button>
          </article>
        ))}
      </section>
      <form className={styles.panel} onSubmit={saveExecutor}>
        <h2>{editing ? `编辑执行者：${editing.name}` : "登记外部执行者"}</h2>
        <div className={styles.fields}>
          <label>
            执行者名称
            <input
              required
              maxLength={128}
              disabled={busy}
              value={form.name}
              onChange={(event) => field("name", event.target.value)}
            />
          </label>
          <label>
            执行者类型
            <select
              aria-label="执行者类型"
              disabled={busy || !!editing}
              value={form.mode}
              onChange={(event) => {
                field("mode", event.target.value);
                field("credential", "");
                field("agentProtocol", false);
                field("apiProfile", false);
              }}
            >
              <option value="api">模型 API（登记）</option>
              <option value="agent">外部 Agent（登记）</option>
              {builtin && (
                <option value={form.mode}>
                  {form.mode === "human" ? "人工" : "本地校验"}
                </option>
              )}
            </select>
          </label>
          <label>
            排序优先级
            <input
              type="number"
              min={-100}
              max={100}
              step={1}
              required
              disabled={busy}
              value={form.priority}
              onChange={(event) =>
                field("priority", Number(event.target.value))
              }
            />
          </label>
        </div>
        <div className={styles.actions}>
          {Object.entries(types).map(([key, label]) => (
            <label className={styles.check} key={key}>
              <input
                type="checkbox"
                disabled={busy || form.mode === "local_test"}
                checked={form.capabilities.includes(key)}
                onChange={(event) =>
                  field(
                    "capabilities",
                    event.target.checked
                      ? [...form.capabilities, key]
                      : form.capabilities.filter((value) => value !== key),
                  )
                }
              />
              {label}
            </label>
          ))}
        </div>
        <label className={styles.check}>
          <input
            type="checkbox"
            disabled={busy}
            checked={form.enabled}
            onChange={(event) => field("enabled", event.target.checked)}
          />
          在路由规则中启用
        </label>
        <label className={styles.check}>
          <input
            type="checkbox"
            disabled={busy}
            checked={form.available}
            onChange={(event) => field("available", event.target.checked)}
          />
          用户标记为可用（不代表已完成连通性检测）
        </label>
        {form.mode === "agent" && (
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={form.agentProtocol}
              disabled={busy}
              onChange={(event) => {
                field("agentProtocol", event.target.checked);
                if (event.target.checked) {
                  field("credential", "");
                  field("inputPrice", "");
                  field("outputPrice", "");
                }
              }}
            />
            启用离线 Agent 协议（只接收零外部用量，每任务单独授权）
          </label>
        )}
        {form.mode === "api" && (
          <>
            <label className={styles.check}>
              <input
                type="checkbox"
                checked={form.apiProfile}
                disabled={busy}
                onChange={(event) => field("apiProfile", event.target.checked)}
              />
              登记 Chat Completions 配置（当前仅本机测试可运行）
            </label>
            {form.apiProfile && (
              <>
                <p>
                  保存地址不会发起真实调用。模型协议测试只在隔离测试环境执行，按声明的上下文额度和价格预留模拟预算；不是供应商账单。
                </p>
                <div className={styles.fields}>
                  <label>
                    模型接口完整地址
                    <input
                      type="url"
                      required
                      maxLength={512}
                      disabled={busy}
                      value={form.endpoint}
                      onChange={(event) =>
                        field("endpoint", event.target.value)
                      }
                      placeholder="https://provider.example/v1/chat/completions"
                    />
                  </label>
                  <label>
                    模型标识
                    <input
                      required
                      maxLength={128}
                      disabled={busy}
                      value={form.model}
                      onChange={(event) => field("model", event.target.value)}
                    />
                  </label>
                  <label>
                    最大输出 token
                    <input
                      type="number"
                      min={1}
                      max={32768}
                      step={1}
                      required
                      disabled={busy}
                      value={form.maxOutput}
                      onChange={(event) =>
                        field("maxOutput", Number(event.target.value))
                      }
                    />
                  </label>
                  <label>
                    请求超时秒数
                    <input
                      type="number"
                      min={1}
                      max={30}
                      step={1}
                      required
                      disabled={busy}
                      value={form.timeout}
                      onChange={(event) =>
                        field("timeout", Number(event.target.value))
                      }
                    />
                  </label>
                </div>
              </>
            )}
          </>
        )}
        {!builtin && !form.agentProtocol && (
          <>
            <div className={styles.fields}>
              <label>
                凭证引用
                <select
                  aria-label="凭证引用"
                  value={form.credential}
                  disabled={busy}
                  onChange={(event) => field("credential", event.target.value)}
                >
                  <option value="">暂不关联</option>
                  {credentials
                    .filter(
                      (item) =>
                        item.provider_type ===
                        (form.mode === "api" ? "model_api" : "external_agent"),
                    )
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                </select>
              </label>
              <label>
                上下文 token 上限
                <input
                  type="number"
                  min={1}
                  max={10000000}
                  step={1}
                  value={form.context}
                  disabled={busy}
                  onChange={(event) => field("context", event.target.value)}
                />
              </label>
              <label>
                价格币种
                <select
                  value={form.currency}
                  disabled={busy}
                  onChange={(event) => field("currency", event.target.value)}
                >
                  <option value="CNY">CNY</option>
                  <option value="USD">USD</option>
                </select>
              </label>
            </div>
            <div className={styles.fields}>
              <label>
                输入价格（每百万 token）
                <input
                  inputMode="decimal"
                  value={form.inputPrice}
                  disabled={busy}
                  onChange={(event) => field("inputPrice", event.target.value)}
                />
              </label>
              <label>
                输出价格（每百万 token）
                <input
                  inputMode="decimal"
                  value={form.outputPrice}
                  disabled={busy}
                  onChange={(event) => field("outputPrice", event.target.value)}
                />
              </label>
            </div>
            <p>
              价格按所选币种填写，最多六位小数；两项均空表示未知价格。凭证在控制台保存，此处只关联名称。
            </p>
          </>
        )}
        <button type="submit" disabled={busy || form.capabilities.length === 0}>
          保存执行者
        </button>
      </form>
      <section className={styles.panel}>
        <h2>作品路由规则</h2>
        <label>
          当前作品
          <select
            disabled={busy}
            value={workId}
            onChange={(event) => {
              if (
                !policy ||
                JSON.stringify(policy) === savedPolicy ||
                window.confirm("放弃未保存的作品规则？")
              )
                void run(() => loadPolicy(event.target.value));
            }}
          >
            <option value="" disabled>
              选择作品
            </option>
            {works.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </label>
        {policy && (
          <form onSubmit={savePolicy}>
            <p>
              规则版本 {policy.version}
              。空允许列表会阻止该作品创建和领取新任务；已在执行的任务可正常结束，需要立即停止时请到任务中心暂停或取消。
            </p>
            <h3>允许使用</h3>
            {executors.map((item) => (
              <label key={item.id} className={styles.check}>
                <input
                  type="checkbox"
                  aria-label={`允许 ${item.name}`}
                  disabled={busy}
                  checked={policy.allowed_ids.includes(item.id)}
                  onChange={(event) =>
                    setPolicy({
                      ...policy,
                      allowed_ids: event.target.checked
                        ? [...policy.allowed_ids, item.id]
                        : policy.allowed_ids.filter((id) => id !== item.id),
                      preferred_ids: event.target.checked
                        ? policy.preferred_ids
                        : policy.preferred_ids.filter((id) => id !== item.id),
                    })
                  }
                />
                {item.name}
              </label>
            ))}
            <h3>首选顺序</h3>
            <p>
              先按首选顺序，再按执行者优先级和稳定标识排序。未列为首选但被允许的执行者仍可参与。
            </p>
            {executors
              .filter((item) => policy.allowed_ids.includes(item.id))
              .map((item) => (
                <label key={item.id} className={styles.check}>
                  <input
                    type="checkbox"
                    aria-label={`首选 ${item.name}`}
                    disabled={busy}
                    checked={policy.preferred_ids.includes(item.id)}
                    onChange={(event) =>
                      setPolicy({
                        ...policy,
                        preferred_ids: event.target.checked
                          ? [...policy.preferred_ids, item.id]
                          : policy.preferred_ids.filter((id) => id !== item.id),
                      })
                    }
                  />
                  {item.name}
                </label>
              ))}
            {policy.preferred_ids.map((id, index) => (
              <div key={id} className={styles.actions}>
                <span>
                  {index + 1}.{" "}
                  {executors.find((item) => item.id === id)?.name || id}
                </span>
                <button
                  type="button"
                  disabled={busy || index === 0}
                  onClick={() => movePreferred(index, -1)}
                  aria-label={`上移首选 ${index + 1}`}
                >
                  上移
                </button>
                <button
                  type="button"
                  disabled={busy || index === policy.preferred_ids.length - 1}
                  onClick={() => movePreferred(index, 1)}
                  aria-label={`下移首选 ${index + 1}`}
                >
                  下移
                </button>
              </div>
            ))}
            <label className={styles.check}>
              <input
                type="checkbox"
                disabled={busy}
                checked={policy.auto_local_diagnostics}
                onChange={(event) =>
                  setPolicy({
                    ...policy,
                    auto_local_diagnostics: event.target.checked,
                  })
                }
              />
              允许自动选择本地流程校验（仅诊断，不创作）
            </label>
            <div className={styles.actions}>
              <button type="submit" disabled={busy}>
                保存路由规则
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  if (
                    JSON.stringify(policy) === savedPolicy ||
                    window.confirm("放弃输入并读取最新规则？")
                  )
                    void run(() => loadPolicy(workId));
                }}
              >
                重载路由规则
              </button>
            </div>
          </form>
        )}
      </section>
      {workId && (
        <section className={styles.panel} aria-label="路由检查">
          <h2>检查已保存规则</h2>
          <p>
            检查不创建任务或预留额度。token
            数由你提供，属于规划估算；创建任务时会重新选择并记录实际执行者。
          </p>
          <div className={styles.fields}>
            <label>
              检查任务类型
              <select
                value={taskType}
                disabled={busy}
                onChange={(event) => setTaskType(event.target.value)}
              >
                {Object.entries(types).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              估计输入 token
              <input
                type="number"
                min={0}
                max={10000000}
                value={input}
                disabled={busy}
                onChange={(event) => setInput(Number(event.target.value))}
              />
            </label>
            <label>
              最大输出 token
              <input
                type="number"
                min={1}
                max={1000000}
                value={output}
                disabled={busy}
                onChange={(event) => setOutput(Number(event.target.value))}
              />
            </label>
          </div>
          <button
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const result = await platformRequest<Route>(
                  "/executor-routing/preview",
                  {
                    method: "POST",
                    body: JSON.stringify({
                      work_id: workId,
                      task_type: taskType,
                      input_tokens: input,
                      max_output_tokens: output,
                    }),
                  },
                );
                setRoute(result);
              })
            }
          >
            检查路由
          </button>
          {route && (
            <div>
              <p>
                检查规则版本：{route.policy_version} ·{" "}
                {route.selected_id
                  ? `当前可选：${route.candidates.find((item) => item.id === route.selected_id)?.name}`
                  : "没有符合规则的可执行者"}
              </p>
              {route.candidates.map((item) => (
                <article key={item.id} className={styles.record}>
                  <strong>{item.name}</strong>
                  <p>
                    {item.reasons.length
                      ? item.reasons
                          .map((code) => reasons[code] || code)
                          .join("；")
                      : "符合当前规则"}
                  </p>
                  <p>
                    {item.mode === "human" ||
                    item.mode === "local_test" ||
                    item.agent_protocol === "offline_v1"
                      ? "不产生模型消费"
                      : item.estimated_microunits === null
                        ? "费用无法估算"
                        : `规划费用：${price(item.estimated_microunits)} ${item.currency}，${item.estimated_tokens} token`}
                  </p>
                </article>
              ))}
            </div>
          )}
          <p>
            <a
              href="/tasks"
              onClick={(event) => {
                if (!mayLeave()) event.preventDefault();
              }}
            >
              进入任务中心
            </a>
            ，选择“按作品规则自动选择”使用已保存规则。
          </p>
        </section>
      )}
    </main>
  );
}
