// SPDX-License-Identifier: AGPL-3.0-or-later
// Ship the pinned runtime with the app; no remote JavaScript is executed.
const fs = require("node:fs");
const path = require("node:path");
const root = path.dirname(require.resolve("@mediapipe/tasks-vision"));
const target = path.resolve(__dirname, "../renderer/vendor/vision");
fs.mkdirSync(target, { recursive: true });
for (const file of ["vision_bundle.mjs", "wasm"]) {
  fs.cpSync(path.join(root, file), path.join(target, file), {
    recursive: true,
  });
}
