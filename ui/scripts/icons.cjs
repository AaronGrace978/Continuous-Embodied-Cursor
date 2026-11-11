// SPDX-License-Identifier: AGPL-3.0-or-later
// Rasterize our original SVG for native app/installer icons.
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
(async () => {
  const folder = path.resolve(__dirname, "../assets");
  const browser = await chromium.launch({
    executablePath:
      process.env.CHROMIUM_PATH ||
      (fs.existsSync("/usr/bin/chromium") ? "/usr/bin/chromium" : undefined),
    args: ["--no-sandbox"],
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 512, height: 512 },
      deviceScaleFactor: 1,
    });
    await page.setContent(
      "<style>html,body{margin:0;width:100%;height:100%;overflow:hidden}svg{width:100%;height:100%}</style>" +
        fs.readFileSync(path.join(folder, "icon.svg"), "utf8"),
    );
    const images = [];
    for (const size of [16, 32, 48, 64, 128, 256, 512]) {
      await page.setViewportSize({ width: size, height: size });
      const png = await page.screenshot({ omitBackground: true });
      if (size === 512) fs.writeFileSync(path.join(folder, "icon.png"), png);
      else images.push({ size, png });
    }
    const header = Buffer.alloc(6 + 16 * images.length);
    header.writeUInt16LE(1, 2);
    header.writeUInt16LE(images.length, 4);
    let offset = header.length;
    images.forEach(({ size, png }, i) => {
      const pos = 6 + 16 * i;
      header[pos] = size === 256 ? 0 : size;
      header[pos + 1] = size === 256 ? 0 : size;
      header.writeUInt16LE(1, pos + 4);
      header.writeUInt16LE(32, pos + 6);
      header.writeUInt32LE(png.length, pos + 8);
      header.writeUInt32LE(offset, pos + 12);
      offset += png.length;
    });
    fs.writeFileSync(
      path.join(folder, "icon.ico"),
      Buffer.concat([header, ...images.map((i) => i.png)]),
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
