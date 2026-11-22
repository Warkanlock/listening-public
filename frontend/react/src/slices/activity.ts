import { api, type Activity, type ActivityExplore } from "../api";
import { errorMessage, type SliceHost } from "./host";

// Download activity (plan 056): the activity snapshot behind
// the top bar, and the explorer payload behind the activity
// dialog.
export class ActivitySlice {
  activity: Activity | null = null;
  activityFailed = false;
  downloadActivity: ActivityExplore | null = null;
  downloadActivityError: string | null = null;
  activityDialogOpen = false;

  constructor(private readonly host: SliceHost) {}

  async refreshActivity() {
    try {
      this.activity = await api<Activity>("/api/activity");
      this.activityFailed = false;
    } catch {
      this.activityFailed = true;
    }
    this.host.bump();
  }

  async refreshDownloadExplorer() {
    if (!this.activityDialogOpen) return;
    try {
      const data = await api<ActivityExplore>("/api/activity/explore");
      if (!this.activityDialogOpen) return;
      this.downloadActivity = data;
      this.downloadActivityError = null;
      this.host.bump();
    } catch (error) {
      if (!this.activityDialogOpen) return;
      this.downloadActivityError = errorMessage(error, "Activity could not load");
      this.host.bump();
    }
  }

  setActivityDialogOpen(open: boolean) {
    this.activityDialogOpen = open;
    if (open) void this.refreshDownloadExplorer();
    this.host.bump();
  }
}
