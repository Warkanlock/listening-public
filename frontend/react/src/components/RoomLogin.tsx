import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { offlineAccount, setOfflineAccount } from "../hikingStorage";
import { accountChangedKey, announceAccountChange, api, postJson, setExpectedAccount } from "../api";

export type RoomRole = "owner" | "member" | "listener" | "paired";
type RoomAccess = { kind: "checking" } | { kind: "signed-in"; role: RoomRole; id: string; personal?: boolean } |
  { kind: "sign-in"; setupRequired: boolean };

function roomAccess(value: unknown): RoomAccess {
  if (typeof value !== "object" || value === null || !("enabled" in value) || !value.enabled ||
      !("role" in value)) return { kind: "signed-in", role: "paired", id: "paired" };
  if (value.role === "owner") return { kind: "signed-in", role: "owner", id: "owner",
    personal: "username" in value && typeof value.username === "string" };
  if (value.role === "member" && "id" in value && typeof value.id === "string")
    return { kind: "signed-in", role: "member", id: value.id, personal: true };
  if (value.role === "listener" && !new URLSearchParams(location.search).has("signin"))
    return { kind: "signed-in", role: "listener", id: "paired" };
  if (value.role === "bootstrap") return { kind: "signed-in", role: "paired", id: "paired" };
  return { kind: "sign-in", setupRequired: "configured" in value && value.configured === false };
}

export function verifiedRoomAccount(value: unknown) {
  if (typeof value !== "object" || value === null || !("role" in value) ||
      (value.role !== "owner" && value.role !== "member" && value.role !== "listener")) return "";
  const access = roomAccess(value);
  return access.kind === "signed-in" ? access.id : "";
}

export function RoomLogin({ children, onUnavailable }: {
  children: (role: RoomRole, appearanceAccount: string | null) => ReactNode;
  onUnavailable: () => boolean;
}) {
  const [access, setAccess] = useState<RoomAccess>({ kind: "checking" });
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const applyAccess = (next: RoomAccess) => {
    const account = next.kind === "signed-in" ? next.id : "";
    if (offlineAccount() !== account) setOfflineAccount("");
    setExpectedAccount(account);
    setAccess(next);
  };
  useEffect(() => {
    const controller = new AbortController();
    const refresh = (event: StorageEvent) => { if (event.key === accountChangedKey) location.reload(); };
    window.addEventListener("storage", refresh);
    const timeout = offlineAccount() ? window.setTimeout(() => {
      if (onUnavailable()) controller.abort();
    }, 6500) : undefined;
    api<unknown>("/api/account", { signal: controller.signal, cache: "no-store" }).then((data) => {
      if (!controller.signal.aborted) applyAccess(roomAccess(data));
    }).catch(() => {
      if (!controller.signal.aborted && !onUnavailable()) applyAccess({ kind: "signed-in", role: "paired", id: "paired" });
    }).finally(() => window.clearTimeout(timeout));
    return () => {
      controller.abort();
      window.clearTimeout(timeout);
      window.removeEventListener("storage", refresh);
    };
  }, [onUnavailable]);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (working) return;
    const fields = new FormData(event.currentTarget);
    setWorking(true);
    setError("");
    try {
      const account = await postJson<unknown>("/api/account/login", {
        username: fields.get("username"), password: fields.get("password"),
      });
      const next = roomAccess(account);
      if (next.kind !== "signed-in" || (next.role !== "owner" && next.role !== "member"))
        throw new Error("The room did not confirm your sign-in.");
      applyAccess(next);
      announceAccountChange();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Couldn't sign in. Try again.");
    } finally {
      setWorking(false);
    }
  }

  if (access.kind === "signed-in") return children(access.role, access.personal ? access.id : null);
  if (access.kind === "checking") return <main className="room-login" aria-busy="true"><p role="status">Connecting to the room…</p></main>;

  return <main className="room-login">
    <section className="room-login-panel">
      <div className="brand" aria-label="Listening">
        <span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /></span>
        <span>listening<span className="brand-dot">.</span></span>
      </div>
      <h1>{access.setupRequired ? "Set up the room owner" : "Sign in to Listening"}</h1>
      {access.setupRequired ? <>
        <p>On the server, set an owner name and password, then return here.</p>
        <code>listening account set-owner OWNER</code>
        <button className="quiet-button" type="button" onClick={() => location.reload()}>Check setup</button>
      </> : <>
        <form onSubmit={(event) => void signIn(event)}>
          <label>Username<input name="username" autoComplete="username" required maxLength={64} /></label>
          <label>Password<input name="password" type="password" autoComplete="current-password" required maxLength={1024} /></label>
          {error && <p role="alert">{error}</p>}
          <button className="primary-action" type="submit" disabled={working}>{working ? "Signing in…" : "Sign in"}</button>
        </form>
        <p>Have an invitation from the room owner? <a href="/?pair=1">Pair this device</a>.</p>
      </>}
      <p><a href={`${import.meta.env.BASE_URL}about/`}>About Listening</a></p>
    </section>
  </main>;
}
