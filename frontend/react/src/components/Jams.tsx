import { useEffect, useRef, useState } from "react";
import { Dialog, DialogBackdrop, DialogClose, DialogDescription, DialogTitle } from "@ui-sdk/react/dialog";
import { api, type JamInvite, type NearbyListening, type NearbySong } from "../api";
import { useStore } from "../store";
import { RoomAction } from "./RoomControls";
import { RoomIcon } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";

function songLabel(song: NearbySong) {
  return [song.title, song.artist].filter(Boolean).join(" · ");
}

export function Jams() {
  const store = useStore();
  const session = store.sessionSlice;
  const [open, setOpen] = useState(false);
  const [nearby, setNearby] = useState<NearbyListening | null>(null);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState("");
  const trigger = useRef<HTMLButtonElement>(null);
  const sharing = useRef(false);
  const busy = useRef(false);
  const revision = useRef(0);
  const following = session.followingJam;
  const update = (next: NearbyListening) => {
    session.setHostingJam(next.hosting);
    sharing.current = next.sharing;
    setNearby(next);
  };

  function sample() {
    return {
      source: store.member || session.personalQueue ? "personal" : "room",
      listening: session.readAudioPlaying?.() === true && !store.radioSounding,
      following: session.followingJam?.id,
    };
  }

  useEffect(() => {
    let stopped = false;
    let reading = false;
    async function refresh() {
      if (stopped || reading || busy.current || (document.hidden && !sharing.current)) return;
      reading = true;
      const epoch = revision.current;
      try {
        const next = sharing.current
          ? await session.jamAction<NearbyListening>("pulse", sample())
          : await api<NearbyListening>("/api/jams", {
              headers: session.outputHeaders, signal: AbortSignal.timeout(4000),
            });
        if (!stopped && epoch === revision.current) update(next);
      } catch {
        // The short lease on the server expires when this device loses contact.
      } finally {
        reading = false;
      }
    }
    const depart = () => {
      if (sharing.current) void session.jamAction("hide", {}, true).catch(() => {});
    };
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("pagehide", depart);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("pagehide", depart);
    };
  }, [store, session]);

  async function act(op: "share" | "hide" | "start" | "end" | "join", invite?: JamInvite) {
    if (busy.current) return;
    busy.current = true;
    revision.current++;
    setPending(true);
    setProblem("");
    try {
      if (op === "join" && invite) await session.joinJam(invite);
      else update(await session.jamAction<NearbyListening>(op, sample()));
    } catch (error) {
      setProblem(error instanceof Error ? error.message : "Could not update nearby listening.");
    } finally {
      busy.current = false;
      setPending(false);
    }
  }

  const listening = nearby?.people.filter(person => person.playing).length ?? 0;
  const suggestion = !following && (nearby?.invites.length ?? 0) > 0;
  const label = following ? `Jam with ${following.host}`
    : suggestion ? `Jams, ${listening} listening nearby, a Jam is available` : `Jams, ${listening} listening nearby`;

  return <>
    <button ref={trigger} type="button" className="quiet-button jams-trigger" title={label} aria-label={label}
      aria-haspopup="dialog" aria-expanded={open} data-active={Boolean(following || nearby?.hosting)}
      onClick={() => { setProblem(""); setOpen(true); }}>
      <RoomIcon name="people" />
      <span className="jams-label">Jams</span><span>{listening}</span>
      {suggestion && <i className="jams-indicator" aria-hidden="true" />}
    </button>
    {open && <Dialog open onOpenChange={next => {
      if (!next) {
        setOpen(false);
        requestAnimationFrame(() => trigger.current?.focus());
      }
    }}>
      <DialogBackdrop />
      <RoomDialogContent className="jams-dialog">
        <DialogClose className="dialog-close" aria-label="Close Jams"><RoomIcon name="close" /></DialogClose>
        <header className="jams-heading">
          <DialogTitle>Jams</DialogTitle>
          <DialogDescription>Listen together with people on your local network.</DialogDescription>
        </header>

        {problem && <p className="jams-problem" role="alert">{problem}</p>}
        {!nearby ? <p role="status">Checking who’s nearby…</p> : !nearby.available
          ? <p>Connect directly to Listening Room on your local network to find Jams.</p> : <>
            {following && <section className="jams-section" aria-label="Your Jam">
              <div className="jams-row">
                <div><strong>Listening with {following.host}</strong><small>{store.session?.current ? songLabel(store.session.current) : "Waiting for the host"}</small></div>
                <RoomAction onClick={() => { session.leaveJam(); void session.pollSession(); }}>Leave Jam</RoomAction>
              </div>
            </section>}
            <section className="jams-section" aria-label="Jam invitations">
              {nearby.invites.filter(invite => invite.id !== following?.id).map(invite => <div className="jams-row" key={invite.id}>
                <div><strong>{invite.host}’s Jam</strong><small>{songLabel(invite.song)}</small></div>
                <RoomAction primary icon="play" disabled={pending} onClick={() => void act("join", invite)}>Join Jam</RoomAction>
              </div>)}
              <div className="jams-row">
                <div>
                  <strong>{nearby.hosting ? "You’re hosting a Jam" : "Start a Jam"}</strong>
                  <small>{nearby.hosting ? "Nearby listeners can choose to join." : "Play a library song and share your listening to invite people."}</small>
                </div>
                {nearby.hosting
                  ? <RoomAction disabled={pending} onClick={() => void act("end")}>End Jam</RoomAction>
                  : <RoomAction icon="people" disabled={pending || !nearby.sharing || !!following}
                    onClick={() => void act("start")}>Start Jam</RoomAction>}
              </div>
            </section>
            <section className="jams-section" aria-label="Nearby listeners">
              <h3>{nearby.people.length} nearby · {listening} listening</h3>
              {nearby.people.length ? <ul className="jams-people">
                {nearby.people.map(person => <li key={person.name} className="jams-person">
                  <span className="jams-avatar" aria-hidden="true">{Array.from(person.name)[0]?.toUpperCase()}</span>
                  <div>
                    <strong>{person.name}</strong>
                    <small>{person.song ? `${person.playing ? "Playing" : "Last played"} · ${songLabel(person.song)}` : "Nearby"}</small>
                  </div>
                </li>)}
              </ul> : <p>Nobody nearby is sharing their listening yet.</p>}
            </section>
            <div className="jams-row jams-section">
              <div><strong>Share my listening</strong><small>People nearby can see your name and current or last library song.</small></div>
              <RoomAction aria-pressed={nearby.sharing} disabled={pending}
                onClick={() => void act(nearby.sharing ? "hide" : "share")}>{nearby.sharing ? "Stop sharing" : "Share listening"}</RoomAction>
            </div>
          </>}
      </RoomDialogContent>
    </Dialog>}
  </>;
}
