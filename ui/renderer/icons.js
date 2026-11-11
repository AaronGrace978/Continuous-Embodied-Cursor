// SPDX-License-Identifier: AGPL-3.0-or-later
"use strict";
function createIcon(name) {
  const paths = {
    cloud: ["M6 18a4 4 0 0 1-1-8 7 7 0 0 1 13-1 4.5 4.5 0 0 1 0 9H6Z"],
    arrow: ["M7 17 17 7", "M7 7h10v10"],
    cursor: ["m5 3 1 17 5-6 7 1L5 3Z"],
    workspace: ["M4 4h16v16H4z", "M9 4v16", "M9 10h11"],
    history: ["M4 10a8 8 0 1 1 0 5", "M4 4v6h6", "M12 7v5l3 2"],
    settings: [
      "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z",
      "m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 2 3 3-1 1-3 3-1 2-3-2-2 1-3-2-3-3 1-2-3-3 1Z",
    ],
    desktop: ["M3 4h18v13H3z", "M12 17v4", "M8 21h8"],
    diamond: ["m12 3 9 9-9 9-9-9Z", "m8 12 4-4 4 4-4 4Z"],
    close: ["m6 6 12 12", "M6 18 18 6"],
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  for (const d of paths[name] || paths.arrow) {
    const node = document.createElementNS(svg.namespaceURI, "path");
    node.setAttribute("d", d);
    svg.append(node);
  }
  return svg;
}
for (const [selector, name] of [
  [".model-mark", "cloud"],
  [".brand-icon", "cursor"],
  ["#workspaceNav .nav-icon", "workspace"],
  ["#historyNav .nav-icon", "history"],
  ["#settingsNav .nav-icon", "settings"],
  [".nav-end", "arrow"],
  [".model-edit", "arrow"],
  [".session-icon", "arrow"],
  [".illustration-cursor", "cursor"],
  ["#topSettings", "settings"],
  [".example-list button span", "arrow"],
  [".command-bottom .primary>span", "arrow"],
  ["#trySimulation span", "arrow"],
  [".help-link span", "arrow"],
  ["#closeSettings", "close"],
  [".dialog-footer .primary>span", "arrow"],
  [".field-baseline>span:last-child", "arrow"],
])
  for (const element of document.querySelectorAll(selector))
    element.replaceChildren(createIcon(name));
for (const id of ["liveMode", "simulationMode"]) {
  const element = document.getElementById(id);
  element.firstChild.textContent =
    id === "liveMode" ? " Live desktop" : " Simulation";
  element.prepend(createIcon(id === "liveMode" ? "desktop" : "diamond"));
}
for (const id of ["getKey", "testConnection", "exportTrace"]) {
  const element = document.getElementById(id);
  element.textContent = element.textContent.replace("↗", "").trim() + " ";
  element.append(createIcon("arrow"));
}
document.querySelector(".brand-period").textContent = "";
