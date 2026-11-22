const browserAgent = navigator.userAgent;
export const macRoomClient = navigator.platform.startsWith("Mac") &&
  (browserAgent.includes("ListeningRoomMac") ||
    (browserAgent.includes("AppleWebKit/") && !browserAgent.includes("Safari/") &&
      !browserAgent.includes("Mobile") && !browserAgent.includes("Chromium/")));

// iPadOS can report a desktop Mac user agent.
export const usesSystemAudio = macRoomClient || /iPhone|iPad|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

export function selectPlaybackAudioSession() {
  if (!usesSystemAudio || !("audioSession" in navigator)) return;
  const session = navigator.audioSession;
  if (session && typeof session === "object" && "type" in session) {
    session.type = "playback";
  }
}

// A 10 ms silent PCM sample allows the media elements to receive the
// browser's user-gesture permission before a network command returns.
export const silence = "data:audio/wav;base64,UklGRnQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YVAAAACAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgA==";
