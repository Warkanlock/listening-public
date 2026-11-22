import { api, type Discover } from "../api";
import { errorMessage, type SliceHost } from "./host";

// Deep Dive (plan 056): the weekly mix, its build state, and
// the re-poll timer while a mix is building.
export class DiscoverSlice {
  discover: Discover | null = null;
  discoverState: "idle" | "loading" | "ready" | "building" | "error" | "failed" =
    "idle";
  discoverError: string | null = null;
  private discoverTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(private readonly host: SliceHost) {}

  async loadDiscover() {
    clearTimeout(this.discoverTimer);
    this.discoverState = "loading";
    this.discoverError = null;
    this.host.bump();
    try {
      const data = await api<Discover>("/api/discover");
      this.discover = data;
      if (data.status === "building") {
        this.discoverState = "building";
        this.host.bump();
        if (this.host.view === "discover") {
          this.discoverTimer = setTimeout(() => {
            if (this.host.view === "discover") void this.loadDiscover();
          }, 5000);
        }
      } else if (data.status === "error") {
        this.discoverState = "error";
        this.host.bump();
      } else {
        this.discoverState = "ready";
        this.host.bump();
      }
    } catch (error) {
      this.discoverState = "failed";
      this.discoverError = errorMessage(error, "The mix could not load");
      this.host.bump();
    }
  }
}
