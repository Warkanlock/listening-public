import type { AudioStoreMode, BulkDownloadFormat } from "../../api";

export interface FormState {
  folder: string;
  autoDownload: boolean;
  bulkDownloadFormat: BulkDownloadFormat;
  audioStore: AudioStoreMode;
  clientId: string;
}

export const EMPTY_FORM: FormState = {
  folder: "",
  autoDownload: false,
  bulkDownloadFormat: "mp3_preferred",
  audioStore: "database",
  clientId: "",
};

export const errorText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;
