import { createToaster } from "@ui-sdk/react/toast";

// The vanilla toast stays 3.6 s; the toaster carries that duration so
// notify() keeps the same timing on the ui-sdk Toast region.
export const toaster = createToaster({ duration: 3600 });

export function notify(message: string) {
  toaster(message);
}
