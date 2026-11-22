import type { AudioStoreMode, BulkDownloadFormat } from "../../api";

// The three settings forms share one body (vanilla settingsBody()),
// so their field values live together here in the parent: a save
// from any panel posts the values every panel currently shows.
export interface FormState {
  folder: string;
  sockseek: string;
  soulseekUsername: string;
  soulseekPassword: string;
  autoDownload: boolean;
  bulkDownloadFormat: BulkDownloadFormat;
  audioStore: AudioStoreMode;
  nicotine: string;
  shareFolder: string;
  shareUsername: string;
  sharePassword: string;
  clientId: string;
}

export const EMPTY_FORM: FormState = {
  folder: "",
  sockseek: "",
  soulseekUsername: "",
  soulseekPassword: "",
  autoDownload: false,
  bulkDownloadFormat: "mp3_preferred",
  audioStore: "database",
  nicotine: "",
  shareFolder: "",
  shareUsername: "",
  sharePassword: "",
  clientId: "",
};

export const errorText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;
