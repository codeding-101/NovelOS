"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  AuthState,
  Credential,
  Runtime,
  platformRequest,
} from "@/lib/platform";
import styles from "./platform.module.css";

const credentialTypes: Record<string, string> = {
  model_api: "模型 API",
  external_agent: "外部 Agent",
  detector: "检测服务",
};

export default function Page() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("owner");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [providerType, setProviderType] = useState("model_api");
  const [secret, setSecret] = useState("");

  const refresh = useCallback(async () => {
    const current = await platformRequest<AuthState>("/auth/status");
    setAuth(current);
    if (current.authenticated) {
      const [status, saved] = await Promise.all([
        platformRequest<Runtime>("/runtime"),
        platformRequest<Credential[]>("/credentials"),
      ]);
      setRuntime(status);
      setCredentials(saved);
    } else {
      setRuntime(null);
      setCredentials([]);
    }
  }, []);

  useEffect(() => {
    refresh().catch((err) => setError(err.message));
    function expired() {
      setAuth((current) =>
        current ? { ...current, authenticated: false } : current,
      );
      setRuntime(null);
      setCredentials([]);
      setSecret("");
    }
    window.addEventListener("novelos-session-expired", expired);
    return () => window.removeEventListener("novelos-session-expired", expired);
  }, [refresh]);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await platformRequest("/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setPassword("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    setError("");
    try {
      await platformRequest("/auth/logout", { method: "POST" });
      setSecret("");
      setNotice("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "退出失败");
    } finally {
      setBusy(false);
    }
  }

  async function saveCredential(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await platformRequest("/credentials", {
        method: "POST",
        body: JSON.stringify({ name, provider_type: providerType, secret }),
      });
      setSecret("");
      setName("");
      await refresh();
      setNotice("凭证已加密保存。执行者接入将在后续阶段开放。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function removeCredential(id: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await platformRequest(`/credentials/${id}`, { method: "DELETE" });
      await refresh();
      setNotice("凭证已删除。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className={styles.shell}>
      <header className={styles.header}>
        <div>
          <div className={styles.wordmark}>
            NovelOS <span>创作控制平台</span>
          </div>
          <p>让作品、记忆与创作进程持续生长。</p>
        </div>
        {auth?.authenticated && (
          <div className={styles.account}>
            <span>{auth.username}</span>
            <button onClick={signOut} disabled={busy}>
              退出登录
            </button>
          </div>
        )}
      </header>
      {error && (
        <div className={styles.error} role="alert">
          {error}
          {!auth && (
            <button
              onClick={() => {
                setError("");
                refresh().catch((err) => setError(err.message));
              }}
            >
              重新连接
            </button>
          )}
        </div>
      )}
      {notice && (
        <div className={styles.notice} role="status">
          {notice}
        </div>
      )}
      {!auth && !error && <p role="status">正在连接工作空间…</p>}
      {auth && !auth.authenticated && (
        <section className={styles.login}>
          <div className={styles.eyebrow}>个人工作空间</div>
          <h1>登录 NovelOS</h1>
          <p>进入你的作品与创作控制环境。</p>
          {!auth.initialized ? (
            <div className={styles.notice}>
              工作空间尚未初始化。请在服务器上按照部署指南创建个人账户，然后刷新此页。
            </div>
          ) : (
            <form onSubmit={signIn} className={styles.form}>
              <label>
                用户名
                <input
                  autoComplete="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  required
                  maxLength={128}
                />
              </label>
              <label>
                密码
                <input
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                  maxLength={1024}
                />
              </label>
              <button className={styles.primary} disabled={busy}>
                {busy ? "正在登录…" : "进入工作空间"}
              </button>
            </form>
          )}
        </section>
      )}
      {auth?.authenticated && (
        <>
          <section className={styles.intro}>
            <div className={styles.eyebrow}>工作空间总览</div>
            <h1>创作环境已建立</h1>
            <p>
              作品空间已开放草稿编辑、版本历史和原稿导入导出。任务执行与质量门禁将按开发计划逐步接入。
            </p>
            <a href="/works">进入作品空间 →</a>
            <p>
              <a href="/tasks">进入任务中心 →</a>
            </p>
            <p>
              <a href="/budgets">进入预算中心 →</a>
            </p>
            <p>
              <a href="/executors">执行者与路由设置 →</a>
            </p>
            <p>
              <a href="/quotas">管理月度配额 →</a>
            </p>
            <p>
              <a href="/memory">记忆与审核 →</a>
            </p>
          </section>
          <div className={styles.cards}>
            <section className={styles.card}>
              <h2>服务状态</h2>
              <strong>{runtime ? "已连接" : "正在读取"}</strong>
              <p>当前阶段：持久任务与预算基础</p>
              <p>
                后台进程：
                {runtime?.workers.filter(
                  (worker) => worker.online && worker.state !== "stopped",
                ).length ?? 0}{" "}
                个在线
              </p>
              <button
                disabled={busy}
                onClick={() => refresh().catch((err) => setError(err.message))}
              >
                刷新状态
              </button>
            </section>
            <section className={styles.card}>
              <h2>创作执行</h2>
              <strong>人工、本地校验与离线 Agent</strong>
              <p>
                已开放任务授权、检查点、候选结果和预算基础。真实模型 API
                与供应商计费接入仍在开发中。
              </p>
            </section>
            <section className={styles.card}>
              <h2>定稿门槛</h2>
              <strong>朱雀人工特征 100%</strong>
              <p>
                真实检测服务接入前，平台不授予正文定稿状态。文学质量与一致性检查将同时参与。
              </p>
            </section>
          </div>
          <section className={styles.credentials}>
            <div>
              <div className={styles.eyebrow}>执行者接入准备</div>
              <h2>服务凭证</h2>
              <p>凭证在服务器加密保存；保存后仅显示名称和类型。</p>
            </div>
            <form onSubmit={saveCredential} className={styles.credentialForm}>
              <label>
                名称
                <input
                  placeholder="例如：我的模型服务"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  required
                  maxLength={128}
                />
              </label>
              <label>
                服务类型
                <select
                  value={providerType}
                  onChange={(event) => setProviderType(event.target.value)}
                >
                  {Object.entries(credentialTypes).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                密钥
                <input
                  type="password"
                  autoComplete="new-password"
                  value={secret}
                  onChange={(event) => setSecret(event.target.value)}
                  required
                  maxLength={16384}
                />
              </label>
              <button className={styles.primary} disabled={busy}>
                加密保存
              </button>
            </form>
            {credentials.length === 0 ? (
              <p className={styles.empty}>
                还没有保存服务凭证。你可以稍后添加。
              </p>
            ) : (
              <ul className={styles.list}>
                {credentials.map((item) => (
                  <li key={item.id}>
                    <div>
                      <strong>{item.name}</strong>
                      <span>{credentialTypes[item.provider_type]}</span>
                    </div>
                    <button
                      disabled={busy}
                      onClick={() => removeCredential(item.id)}
                      aria-label={`删除凭证 ${item.name}`}
                    >
                      删除
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
      <footer className={styles.footer}>NovelOS · 个人长篇创作运行环境</footer>
    </main>
  );
}
