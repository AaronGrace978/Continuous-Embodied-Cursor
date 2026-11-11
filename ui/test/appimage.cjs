// SPDX-License-Identifier: AGPL-3.0-or-later
// AppImage's runtime wrapper cannot use Playwright's Electron Node loader.
// Exercise the real package via a loopback CDP connection instead.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
(async () => {
  const release = path.resolve(__dirname, '../release');
  const image = fs.readdirSync(release).find(name => name.endsWith('.AppImage'));
  assert.ok(image, 'Build the Linux AppImage first');
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'cec-appimage-'));
  const child = spawn(path.join(release, image), ['--appimage-extract-and-run', `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1',
    ...(process.env.CEC_ELECTRON_TEST_ARGS || '').split(' ').filter(Boolean)], {
    env: {...process.env, CEC_USER_DATA_DIR: userData}, detached: true, stdio: ['ignore','ignore','pipe']
  });
  child.stderr.on('data', () => {});
  let browser;
  try {
    let version;
    for (let i=0; i<150; i++) {
      try {const response=await fetch(`http://127.0.0.1:${port}/json/version`);version=await response.json();break;}
      catch {await new Promise(resolve=>setTimeout(resolve,100));}
    }
    assert.ok(version?.webSocketDebuggerUrl, 'AppImage did not expose its test CDP endpoint');
    browser=await chromium.connectOverCDP(version.webSocketDebuggerUrl);
    const context=browser.contexts()[0];
    const page=await Promise.any(context.pages().map(async candidate=>{await candidate.waitForURL('**/index.html');return candidate;}));
    await page.waitForSelector('#trySimulation');
    const result=await page.evaluate(async()=>{
      await window.cursor.saveSettings({provider:'offline'});
      await window.cursor.connect('simulation');
      const report=await window.cursor.run('click Export');
      await window.cursor.disconnect();
      return report;
    });
    assert.equal(result.status,'done');
    assert.equal(result.outcomes[0].intent.action,'click');
    console.log('PASS: actual AppImage startup, bundled Python source, native IPC and a complete simulated task.');
  } finally {
    await browser?.close();
    try {process.kill(-child.pid,'SIGTERM');} catch {}
    await new Promise(resolve=>{if(child.exitCode!==null)resolve();else {child.once('exit',resolve);setTimeout(resolve,2000).unref();}});
    fs.rmSync(userData,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
