"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { platformRequest } from "@/lib/platform";
import shell from "../platform.module.css";
import styles from "./tasks.module.css";
import AgentGrants from "./AgentGrants";
import TaskBudget, { TaskSpendingBudget, parseMoney } from "./TaskBudget";

type Work = { id: string; title: string };
type Asset = {
  id: string;
  title: string;
  revision_id: string;
  head_version: number;
};
type Executor = {
  api_config: Record<string, unknown> | null;
  currency: string | null;
  mode: string;
  id: string;
  name: string;
  capabilities: string[];
  adapter_ready: boolean;
  enabled: boolean;
  available: boolean;
};
type Task = {
  id: string;
  objective: string;
  task_type: string;
  executor_id: string;
  state: string;
  version: number;
  attempt_count: number;
  max_attempts: number;
  reason: string | null;
  deadline_at: string;
};
type Checkpoint = {
  id: string;
  attempt_id: string;
  sequence: number;
  step: string;
  payload: { note?: string };
};
type Detail = Task & {
  spending_budget: TaskSpendingBudget;
  routing: {
    requested_executor_id: string;
    selected_executor_id: string;
    snapshot: { policy_version: number };
  } | null;
  input: {
    revision_id: string;
    title: string;
    current: boolean;
    sha256: string;
  };
  attempts: {
    id: string;
    number: number;
    state: string;
    lease_until: string;
  }[];
  checkpoint: Checkpoint | null;
  submissions: { id: string; disposition: string; reason: string | null }[];
  dependencies: { id: string; state: string }[];
};
type Lease = {
  task_id: string;
  attempt_id: string;
  lease_token: string;
  executor_id: string;
  input: { revision_id: string; content: string };
  checkpoint: Checkpoint | null;
};
type Submission = {
  disposition: string;
  reason: string | null;
  envelope: {
    usage?: { accounting: string; input_tokens: number; output_tokens: number };
    output_artifacts: { kind: string; title: string; content: string }[];
    warnings: string[];
  };
};
const states: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "执行中",
  RETRY_WAIT: "等待重试",
  PAUSED: "已暂停",
  SUCCEEDED: "候选已接收",
  FAILED: "失败",
  CANCELLED: "已取消",
};
const types: Record<string, string> = {
  analyze: "分析",
  draft: "创作",
  revise: "修改",
  review: "审查",
  research: "研究",
};
const dispositions: Record<string, string> = {
  ACCEPTED: "已接收候选",
  PARTIAL: "部分结果",
  QUARANTINED: "隔离结果",
  FAILED: "失败记录",
};
const reasons: Record<string, string> = {
  INPUT_STALE: "输入正文已有新版本，需要重新创建任务",
  LEASE_EXPIRED: "执行失联，检查点已保留",
  ATTEMPTS_EXHAUSTED: "已达到尝试次数上限",
  DEADLINE_EXCEEDED: "已超过任务期限",
  LIMIT_EXCEEDED: "已达到次数或期限上限",
  DEPENDENCY_FAILED: "依赖任务失败或取消",
  OUTPUT_INCOMPLETE: "结果不完整，需要复查",
  USER_REQUEST: "用户操作",
  ATTEMPT_INACTIVE: "来自已结束或过期的执行",
  OUTPUT_SCOPE_INVALID: "结果超出授权输入范围",
  BUDGET_UNCERTAIN: "调用费用尚不明确，请在预算中心核对后恢复",
  EXECUTOR_BLOCKED: "执行者或作品规则已变化，请在执行者设置中核对后恢复",
  AGENT_REVOKED: "Agent 授权已撤销；重新授权后可恢复排队",
  MODEL_PREFLIGHT_FAILED:
    "模型协议检查未通过，未发送请求；请核对配置、凭证和预算",
  TASK_BUDGET_EXCEEDED: "任务累计预算不足或尚未配置，请核对任务预算后恢复",
};

async function allPages<T>(path: string) {
  const output: T[] = [];
  for (let offset = 0; ; offset += 100) {
    const page = await platformRequest<T[]>(
      `${path}?offset=${offset}&limit=100`,
    );
    output.push(...page);
    if (page.length < 100) return output;
  }
}

export default function TasksPage() {
  const [works, setWorks] = useState<Work[]>([]);
  const [workId, setWorkId] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [executors, setExecutors] = useState<Executor[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [lease, setLease] = useState<Lease | null>(null);
  const [candidate, setCandidate] = useState<Submission | null>(null);
  const [assetId, setAssetId] = useState("");
  const [executorId, setExecutorId] = useState("human");
  const [type, setType] = useState("analyze");
  const [objective, setObjective] = useState("");
  const [dependency, setDependency] = useState("");
  const [maxAttempts, setMaxAttempts] = useState(3);
  const [taskMoney, setTaskMoney] = useState("");
  const [taskTokens, setTaskTokens] = useState("");
  const [content, setContent] = useState("");
  const [note, setNote] = useState("");
  const [savedNote, setSavedNote] = useState("");
  const [ready, setReady] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [expired, setExpired] = useState(false);
  const createKey = useRef({ signature: "", key: "" });
  const resultKey = useRef({ signature: "", key: "" });
  const sequence = useRef(0);
  const busy = pending || !ready;
  const dirty = !!content || note !== savedNote;
  const selectedTask = detail?.id;

  useEffect(() => {
    Promise.all([
      allPages<Work>("/works"),
      platformRequest<Executor[]>("/executors"),
    ])
      .then(([list, providers]) => {
        setWorks(list);
        setExecutors(providers);
        setType(
          providers.find((e) => e.id === "human")?.capabilities[0] || "analyze",
        );
      })
      .catch((err) => setError(err.message))
      .finally(() => setReady(true));
    function expiredSession() {
      setExpired(true);
      setLease(null);
    }
    window.addEventListener("novelos-session-expired", expiredSession);
    return () =>
      window.removeEventListener("novelos-session-expired", expiredSession);
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

  const refresh = useCallback(async () => {
    if (!workId) return;
    const [list, current] = await Promise.all([
      platformRequest<Task[]>(`/tasks?work_id=${workId}&limit=100`),
      selectedTask
        ? platformRequest<Detail>(`/tasks/${selectedTask}`)
        : Promise.resolve(null),
    ]);
    setTasks(list);
    if (current)
      setDetail((old) =>
        old?.id === current.id && old.version > current.version ? old : current,
      );
  }, [workId, selectedTask]);

  useEffect(() => {
    if (!workId || expired) return;
    // Avoid an earlier selection's request replacing a later selection.
    let active = true;
    const timer = window.setInterval(() => {
      Promise.all([
        platformRequest<Task[]>(`/tasks?work_id=${workId}&limit=100`),
        selectedTask
          ? platformRequest<Detail>(`/tasks/${selectedTask}`)
          : Promise.resolve(null),
      ])
        .then(([list, current]) => {
          if (active) {
            setTasks(list);
            if (current)
              setDetail((old) =>
                old?.id === current.id && old.version > current.version
                  ? old
                  : current,
              );
          }
        })
        .catch((err) => {
          if (active) setError(err.message);
        });
    }, 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [workId, selectedTask, expired]);

  useEffect(() => {
    if (!lease) return;
    let active = true;
    const timer = window.setInterval(() => {
      platformRequest(`/tasks/${lease.task_id}/heartbeat`, {
        method: "POST",
        body: JSON.stringify({
          attempt_id: lease.attempt_id,
          lease_token: lease.lease_token,
        }),
      }).catch((err) => {
        if (active) {
          setError(err.message);
          setLease(null);
        }
      });
    }, 20000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [lease]);

  useEffect(() => {
    if (detail && detail.state !== "RUNNING") setLease(null);
  }, [detail]);

  async function run(operation: () => Promise<void>) {
    setPending(true);
    setError("");
    setNotice("");
    try {
      await operation();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setPending(false);
    }
  }
  function mayLeave() {
    return (
      !dirty || window.confirm("当前人工输入尚未提交。放弃未保存输入并切换吗？")
    );
  }
  async function openTask(id: string) {
    const item = await platformRequest<Detail>(`/tasks/${id}`);
    setDetail(item);
    setLease(null);
    setContent("");
    setNote("");
    setSavedNote("");
    setCandidate(null);
  }
  async function openWork(id: string) {
    const [items, list] = await Promise.all([
      allPages<Asset>(`/works/${id}/assets`),
      platformRequest<Task[]>(`/tasks?work_id=${id}&limit=100`),
    ]);
    setWorkId(id);
    setAssets(items);
    setTasks(list);
    setAssetId("");
    setDependency("");
    setDetail(null);
    setLease(null);
    setContent("");
    setNote("");
    setSavedNote("");
    setCandidate(null);
  }
  async function create(event: FormEvent) {
    event.preventDefault();
    if (!mayLeave()) return;
    await run(async () => {
      const asset = assets.find((a) => a.id === assetId);
      if (!asset) throw new Error("请选择已保存的输入资产。");
      const selected = executors.find((item) => item.id === executorId);
      const custom = selected?.api_config && (taskMoney || taskTokens);
      if (
        custom &&
        (!taskMoney ||
          !/^\d+$/.test(taskTokens) ||
          Number(taskTokens) > 10 ** 12)
      )
        throw new Error(
          "请同时填写任务费用和 token 上限，token 须为支持范围内的非负整数。",
        );
      const payload = {
        spending_limit: custom
          ? {
              currency: selected?.currency,
              limit_microunits: parseMoney(taskMoney),
              limit_tokens: Number(taskTokens),
            }
          : null,
        work_id: workId,
        input_revision_id: asset.revision_id,
        executor_id: executorId,
        task_type: type,
        objective,
        max_attempts: maxAttempts,
        dependencies: dependency ? [dependency] : [],
      };
      const signature = JSON.stringify(payload);
      if (createKey.current.signature !== signature)
        createKey.current = { signature, key: crypto.randomUUID() };
      const created = await platformRequest<{ id: string }>("/tasks", {
        method: "POST",
        body: JSON.stringify({
          ...payload,
          request_key: createKey.current.key,
        }),
      });
      setTasks(
        await platformRequest<Task[]>(`/tasks?work_id=${workId}&limit=100`),
      );
      await openTask(created.id);
      setObjective("");
      setNotice("任务已入队。结果将保留为候选，不会自动改写正文。");
      createKey.current = { signature: "", key: "" };
    });
  }
  async function claim(takeover = false) {
    if (!detail) return;
    await run(async () => {
      const envelope = await platformRequest<Lease>(
        `/tasks/${detail.id}/${takeover ? "takeover" : "claim"}`,
        {
          method: "POST",
          ...(takeover
            ? { body: JSON.stringify({ expected_version: detail.version }) }
            : {}),
        },
      );
      // Read current state before installing a lease, so a stale QUEUED detail cannot clear it.
      setDetail(await platformRequest<Detail>(`/tasks/${detail.id}`));
      setLease(envelope);
      sequence.current =
        envelope.checkpoint?.attempt_id === envelope.attempt_id
          ? envelope.checkpoint.sequence
          : 0;
      const restoredNote = envelope.checkpoint?.payload.note || "";
      if (!note) {
        setNote(restoredNote);
        setSavedNote(restoredNote);
      }
      setNotice(
        takeover
          ? "已接续人工任务，其他页面的旧执行凭证已失效。"
          : "已领取人工任务，完成进度可保存为检查点。",
      );
    });
  }
  async function saveCheckpoint() {
    if (!lease) return;
    await run(async () => {
      const current = await platformRequest<Detail>(`/tasks/${lease.task_id}`);
      const previous =
        current.checkpoint?.attempt_id === lease.attempt_id
          ? current.checkpoint
          : null;
      sequence.current = previous?.sequence || 0;
      if (
        previous?.step === "manual_progress" &&
        previous.payload.note === note
      ) {
        setSavedNote(note);
        setDetail(current);
        setNotice("检查点已经保存。");
        return;
      }
      await platformRequest(`/tasks/${lease.task_id}/checkpoints`, {
        method: "POST",
        body: JSON.stringify({
          attempt_id: lease.attempt_id,
          lease_token: lease.lease_token,
          sequence: sequence.current + 1,
          step: "manual_progress",
          payload: { note },
        }),
      });
      sequence.current += 1;
      setSavedNote(note);
      await refresh();
      setNotice("检查点已保存，中断后可以恢复查看。");
    });
  }
  async function submit() {
    if (!lease || !detail) return;
    await run(async () => {
      const payload = {
        task_id: lease.task_id,
        executor_id: lease.executor_id,
        attempt_id: lease.attempt_id,
        result_status: "success",
        finish_reason: "complete",
        output_artifacts: [
          { kind: detail.task_type, title: "人工候选结果", content },
        ],
        evidence_revision_ids: [lease.input.revision_id],
      };
      const signature = JSON.stringify(payload);
      if (resultKey.current.signature !== signature)
        resultKey.current = { signature, key: crypto.randomUUID() };
      const received = await platformRequest<{ disposition: string }>(
        `/tasks/${detail.id}/results`,
        {
          method: "POST",
          body: JSON.stringify({
            ...payload,
            lease_token: lease.lease_token,
            submission_id: resultKey.current.key,
          }),
        },
      );
      setLease(null);
      setContent("");
      setSavedNote(note);
      await refresh();
      setNotice(
        `结果已保存：${dispositions[received.disposition]}。正文和定稿状态未变。`,
      );
    });
  }
  async function control(action: string) {
    if (!detail) return;
    await run(async () => {
      await platformRequest(`/tasks/${detail.id}/control/${action}`, {
        method: "POST",
        body: JSON.stringify({ expected_version: detail.version }),
      });
      setLease(null);
      await refresh();
      setNotice("任务状态已更新，已保存的检查点和结果仍保留。");
    });
  }

  return (
    <main className={shell.shell}>
      <header className={shell.header}>
        <div>
          <div className={shell.wordmark}>
            NovelOS <span>任务中心</span>
          </div>
          <p>组织执行、保存进度，并核对每一份候选成果。</p>
        </div>
        <a href="/">返回控制台</a>
      </header>
      <h1>任务中心</h1>
      <p>
        当前支持人工、本地校验和任务专属授权的离线 Agent。真实模型 API
        及付费调用尚未开放。
      </p>
      {error && (
        <div className={shell.error} role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className={shell.notice} role="status">
          {notice}
        </div>
      )}
      {expired && (
        <p className={shell.notice}>
          登录已失效，输入保留在本页。
          <a href="/" target="_blank" rel="noreferrer">
            在新标签页登录
          </a>
          后，
          <button
            onClick={() =>
              run(async () => {
                await refresh();
                setExpired(false);
              })
            }
          >
            重新连接
          </button>
        </p>
      )}
      <label className={styles.workPicker}>
        当前作品
        <select
          value={workId}
          disabled={busy}
          onChange={(e) => {
            if (mayLeave()) void run(() => openWork(e.target.value));
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
      {!workId ? (
        <p>
          先选择作品；可在<a href="/works">作品空间</a>建立章节和资料。
        </p>
      ) : (
        <>
          <form className={styles.create} onSubmit={create}>
            <h2>新建任务</h2>
            <div className={styles.fields}>
              <label>
                输入版本
                <select
                  required
                  value={assetId}
                  disabled={busy}
                  onChange={(e) => setAssetId(e.target.value)}
                >
                  <option value="">选择已保存资产</option>
                  {assets.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.title} · v{a.head_version}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                执行者
                <select
                  aria-label="执行者"
                  disabled={busy}
                  value={executorId}
                  onChange={(e) => {
                    setExecutorId(e.target.value);
                    setTaskMoney("");
                    setTaskTokens("");
                    setType(
                      e.target.value === "auto"
                        ? "analyze"
                        : executors.find((item) => item.id === e.target.value)
                            ?.capabilities[0] || "analyze",
                    );
                  }}
                >
                  <option value="auto">按作品规则自动选择</option>
                  {executors
                    .filter((e) => e.adapter_ready)
                    .map((e) => (
                      <option key={e.id} value={e.id}>
                        {e.name}
                        {!e.enabled || !e.available ? "（当前不可用）" : ""}
                      </option>
                    ))}
                </select>
              </label>
              <label>
                任务类型
                <select
                  disabled={busy}
                  value={type}
                  onChange={(e) => setType(e.target.value)}
                >
                  {(
                    (executorId === "auto"
                      ? Object.keys(types)
                      : executors.find((e) => e.id === executorId)
                          ?.capabilities) || []
                  ).map((value) => (
                    <option key={value} value={value}>
                      {types[value]}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                最多尝试次数
                <input
                  type="number"
                  min={1}
                  max={5}
                  disabled={busy}
                  value={maxAttempts}
                  onChange={(e) => setMaxAttempts(Number(e.target.value))}
                />
              </label>
              <label>
                等待前置任务
                <select
                  disabled={busy}
                  value={dependency}
                  onChange={(e) => setDependency(e.target.value)}
                >
                  <option value="">无依赖</option>
                  {tasks.map((task) => (
                    <option key={task.id} value={task.id}>
                      {task.objective.slice(0, 60)} · {states[task.state]}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label>
              任务目标
              <input
                required
                maxLength={4000}
                disabled={busy}
                value={objective}
                onChange={(e) => setObjective(e.target.value)}
              />
            </label>
            {executors.find((item) => item.id === executorId)?.api_config && (
              <section aria-label="创建任务预算">
                <p>
                  任务预算按所有尝试累计，币种为{" "}
                  {executors.find((item) => item.id === executorId)?.currency}
                  。留空时按单次预留 × 最大尝试次数设置，作品预算同时生效。
                </p>
                <div className={styles.fields}>
                  <label>
                    任务费用上限（可选）
                    <input
                      inputMode="decimal"
                      disabled={busy}
                      value={taskMoney}
                      onChange={(event) => setTaskMoney(event.target.value)}
                    />
                  </label>
                  <label>
                    任务 token 上限（可选）
                    <input
                      inputMode="numeric"
                      disabled={busy}
                      value={taskTokens}
                      onChange={(event) => setTaskTokens(event.target.value)}
                    />
                  </label>
                </div>
              </section>
            )}
            <p>
              任务最长保留 24 小时执行窗口，单次执行最多 1
              小时。关闭页面后，本地校验继续；人工进度请先保存检查点。
            </p>
            {executorId === "local_test" && (
              <p>
                本地校验只核对字符数、段落数和输入哈希，用于验证任务流程，不进行小说创作或文学评审。
              </p>
            )}
            <div className={styles.actions}>
              <button
                className={shell.primary}
                disabled={busy || !assetId || !objective.trim()}
              >
                创建任务
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  run(async () =>
                    setAssets(await allPages<Asset>(`/works/${workId}/assets`)),
                  )
                }
              >
                刷新输入版本
              </button>
            </div>
          </form>
          <div className={styles.workspace}>
            <aside className={styles.list}>
              <h2>最近 100 个任务</h2>
              <button disabled={busy} onClick={() => run(refresh)}>
                刷新任务
              </button>
              <ul>
                {tasks.map((task) => (
                  <li key={task.id}>
                    <button
                      disabled={busy}
                      aria-pressed={detail?.id === task.id}
                      onClick={() => {
                        if (mayLeave()) void run(() => openTask(task.id));
                      }}
                    >
                      <strong>{task.objective}</strong>
                      <small>
                        {states[task.state]} ·{" "}
                        {executors.find((e) => e.id === task.executor_id)
                          ?.name || task.executor_id}
                      </small>
                    </button>
                  </li>
                ))}
              </ul>
            </aside>
            <section className={styles.detail} aria-label="任务详情">
              {!detail ? (
                <p>选择任务查看进度、检查点和候选结果。</p>
              ) : (
                <>
                  <h2>{detail.objective}</h2>
                  <p>
                    执行者：
                    {executors.find((e) => e.id === detail.executor_id)?.name ||
                      detail.executor_id}
                    {detail.routing?.requested_executor_id === "auto" &&
                      `（自动选择，规则版本 ${detail.routing.snapshot.policy_version}）`}
                  </p>
                  <p>
                    状态：<strong>{states[detail.state]}</strong> · 尝试{" "}
                    {detail.attempt_count} / {detail.max_attempts}
                  </p>
                  <p>
                    输入：{detail.input.title} ·{" "}
                    {detail.input.current
                      ? "仍为当前版本"
                      : "已出现新版本，旧输入成果需复查"}
                  </p>
                  <p>截止：{new Date(detail.deadline_at).toLocaleString()}</p>
                  {detail.spending_budget.supported && (
                    <TaskBudget
                      key={detail.id}
                      task={detail}
                      refresh={refresh}
                    />
                  )}
                  {detail.reason && (
                    <p className={shell.notice}>
                      {reasons[detail.reason] || detail.reason}
                    </p>
                  )}
                  {!!detail.dependencies.length && (
                    <p>
                      前置任务：
                      {detail.dependencies
                        .map((dep) => states[dep.state])
                        .join("、")}
                    </p>
                  )}
                  <div className={styles.actions}>
                    {detail.executor_id === "human" &&
                      ["QUEUED", "RETRY_WAIT"].includes(detail.state) && (
                        <button disabled={busy} onClick={() => claim()}>
                          领取人工任务
                        </button>
                      )}
                    {detail.executor_id === "human" &&
                      detail.state === "RUNNING" &&
                      !lease && (
                        <button disabled={busy} onClick={() => claim(true)}>
                          接续人工任务
                        </button>
                      )}
                    {detail.state === "PAUSED" && (
                      <button disabled={busy} onClick={() => control("resume")}>
                        恢复排队
                      </button>
                    )}
                    {["QUEUED", "RUNNING", "RETRY_WAIT"].includes(
                      detail.state,
                    ) && (
                      <button disabled={busy} onClick={() => control("pause")}>
                        暂停任务
                      </button>
                    )}
                    {!["SUCCEEDED", "FAILED", "CANCELLED"].includes(
                      detail.state,
                    ) && (
                      <button disabled={busy} onClick={() => control("cancel")}>
                        取消任务
                      </button>
                    )}
                  </div>
                  {detail.executor_id === "human" &&
                    (lease || content || note) && (
                      <div className={styles.manual}>
                        <h3>人工处理</h3>
                        {lease && (
                          <details>
                            <summary>查看绑定的输入正文</summary>
                            <pre>{lease.input.content}</pre>
                          </details>
                        )}
                        <label htmlFor="task-note">进度笔记</label>
                        <textarea
                          id="task-note"
                          maxLength={8000}
                          readOnly={!lease}
                          disabled={busy}
                          value={note}
                          onChange={(e) => setNote(e.target.value)}
                        />
                        <button
                          disabled={busy || !lease}
                          onClick={saveCheckpoint}
                        >
                          保存检查点
                        </button>
                        <label htmlFor="task-result">候选结果</label>
                        <textarea
                          id="task-result"
                          readOnly={!lease}
                          disabled={busy}
                          value={content}
                          onChange={(e) => setContent(e.target.value)}
                        />
                        <button
                          disabled={busy || !lease || !content.trim()}
                          onClick={submit}
                        >
                          提交候选结果
                        </button>
                      </div>
                    )}
                  {detail.checkpoint && (
                    <section>
                      <h3>最近检查点</h3>
                      <pre className={styles.record}>
                        {JSON.stringify(detail.checkpoint.payload, null, 2)}
                      </pre>
                    </section>
                  )}
                  {executors.find((e) => e.id === detail.executor_id)?.mode ===
                    "agent" && (
                    <AgentGrants
                      key={detail.id}
                      task={detail}
                      refresh={refresh}
                    />
                  )}
                  <h3>执行记录</h3>
                  <ul>
                    {detail.attempts.map((attempt) => (
                      <li key={attempt.id}>
                        第 {attempt.number} 次 ·{" "}
                        {states[attempt.state] || attempt.state}
                      </li>
                    ))}
                  </ul>
                  <h3>候选与隔离结果</h3>
                  <p>
                    任务完成只表示候选被接收。正文、正式记忆和定稿状态不会自动改变。
                  </p>
                  <ul className={styles.submissions}>
                    {detail.submissions.map((item) => (
                      <li key={item.id}>
                        <button
                          disabled={busy}
                          onClick={() =>
                            run(async () => {
                              setCandidate(
                                await platformRequest<Submission>(
                                  `/tasks/${detail.id}/submissions/${item.id}`,
                                ),
                              );
                            })
                          }
                        >
                          {dispositions[item.disposition]}
                          {item.reason
                            ? ` · ${reasons[item.reason] || item.reason}`
                            : ""}
                        </button>
                      </li>
                    ))}
                  </ul>
                  {candidate && (
                    <section className={styles.manual}>
                      <h3>{dispositions[candidate.disposition]}</h3>
                      {candidate.envelope.usage?.accounting === "settled" && (
                        <p>
                          模拟用量已入账：输入{" "}
                          {candidate.envelope.usage.input_tokens} token，输出{" "}
                          {candidate.envelope.usage.output_tokens} token。
                          <a href="/budgets">查看预算记录</a>
                        </p>
                      )}
                      {candidate.envelope.output_artifacts.map(
                        (artifact, index) => (
                          <div key={index}>
                            <label htmlFor={`candidate-${index}`}>
                              {artifact.title}
                            </label>
                            <textarea
                              id={`candidate-${index}`}
                              readOnly
                              value={artifact.content}
                            />
                          </div>
                        ),
                      )}
                      {candidate.envelope.warnings.map((warning, index) => (
                        <p key={index}>{warning}</p>
                      ))}
                    </section>
                  )}
                </>
              )}
            </section>
          </div>
        </>
      )}
    </main>
  );
}
