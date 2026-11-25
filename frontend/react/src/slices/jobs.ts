import {
  api,
  postJson,
  type DownloadJobScope,
  type JobKind,
  type JobsResult,
  type ProcessSnapshot,
} from "../api";
import { notify } from "../toasts";
import { errorMessage, type SliceHost } from "./host";

// Jobs (plan 056): the Jobs console's result, its errors, and
// the Downloads panel's process snapshot.
export class JobsSlice {
  processSnapshot: ProcessSnapshot | null = null;
  jobs: JobsResult | null = null;
  jobsError: string | null = null;
  jobActionError: { kind: JobKind; message: string } | null = null;
  private jobsRevision = 0;

  constructor(private readonly host: SliceHost) {}

  async refreshProcess() {
    if (this.host.view !== "settings") return;
    try {
      this.processSnapshot = await api<ProcessSnapshot>("/api/process");
      this.host.bump();
    } catch {
      // The Downloads panel (Stage 4) renders its own error state.
    }
  }

  async refreshJobs() {
    if (this.host.view !== "jobs") return false;
    const revision = ++this.jobsRevision;
    try {
      const result = await api<JobsResult>("/api/jobs");
      if (revision === this.jobsRevision) {
        this.jobs = result;
        this.jobsError = null;
        this.host.bump();
      }
      return true;
    } catch (error) {
      if (revision === this.jobsRevision) {
        this.jobsError = errorMessage(error, "Jobs could not load");
        this.host.bump();
      }
      return false;
    }
  }

  async runJob(
    kind: JobKind,
    action: "start" | "stop",
    scope?: DownloadJobScope,
  ): Promise<boolean> {
    try {
      const result = await postJson<JobsResult & { status: string }>(
        "/api/jobs",
        { kind, action, scope },
      );
      this.jobsRevision++;
      this.jobs = result;
      this.jobsError = null;
      this.jobActionError = null;
      void this.host.refreshActivity();
      return true;
    } catch (error) {
      this.jobActionError = {
        kind,
        message: errorMessage(error, "That job could not be updated"),
      };
      return false;
    } finally {
      this.host.bump();
    }
  }

  async runWorkerAction(action: string): Promise<boolean> {
    try {
      await postJson("/api/process", { action });
      notify("Process updated");
      await Promise.all([
        this.refreshProcess(),
        this.host.refreshActivity(),
        this.host.loadSettings(),
      ]);
      return true;
    } catch (error) {
      notify(errorMessage(error, "Process update failed"));
      return false;
    }
  }
}
