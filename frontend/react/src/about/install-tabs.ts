export function enableInstallTabs() {
  const strip = document.querySelector<HTMLElement>("[data-install-tabs]");
  if (!strip) return;
  const tabs = Array.from(strip.querySelectorAll<HTMLButtonElement>('[role="tab"]')).flatMap(button => {
    const panel = document.getElementById(button.getAttribute("aria-controls") ?? "");
    return panel ? [{ button, panel }] : [];
  });
  if (tabs.length === 0) return;

  function activate(index: number, focus = false) {
    tabs.forEach(({ button, panel }, position) => {
      const active = position === index;
      button.tabIndex = active ? 0 : -1;
      button.setAttribute("aria-selected", String(active));
      panel.hidden = !active;
    });
    if (focus) tabs[index].button.focus();
  }

  function selectFromHash() {
    const target = document.getElementById(location.hash.slice(1));
    const index = target ? tabs.findIndex(({ panel }) => panel.contains(target)) : -1;
    if (index < 0) return false;
    activate(index);
    target?.scrollIntoView();
    return true;
  }

  tabs.forEach(({ button, panel }, index) => {
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", button.id);
    panel.tabIndex = 0;
    button.addEventListener("click", () => activate(index));
    button.addEventListener("keydown", event => {
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length
        : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length
        : event.key === "Home" ? 0
        : event.key === "End" ? tabs.length - 1 : undefined;
      if (next === undefined) return;
      event.preventDefault();
      activate(next, true);
    });
  });
  strip.hidden = false;
  if (!selectFromHash()) activate(0);
  window.addEventListener("hashchange", selectFromHash);
  for (const shortcut of document.querySelectorAll<HTMLAnchorElement>("a[data-install-shortcut]")) {
    shortcut.addEventListener("click", event => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (shortcut.hash === location.hash) selectFromHash();
    });
  }
}
