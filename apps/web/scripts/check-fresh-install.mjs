// Check a brand-new installation: no mock providers, no demo channel.
import {chromium} from "playwright";

const browser = await chromium.launch();
const page = await (await browser.newContext({viewport: {width: 1440, height: 1100}})).newPage();

await page.goto("http://localhost:3000/login", {waitUntil: "networkidle"});
await page.fill('input[type="email"]', "admin@factory.app");
await page.fill('input[type="password"]', "factory-admin-2026");
await page.getByRole("button", {name: /enter studio/i}).click();
await page.waitForURL("http://localhost:3000/", {timeout: 25000});

const home = await page.textContent("body");
console.log("home has demo channel:", /Little Lantern/i.test(home ?? ""));
console.log("home mentions mock   :", /\bmock\b/i.test(home ?? ""));
await page.screenshot({path: "C:/Users/Asus/AppData/Local/Temp/opencode/fresh-home.png", fullPage: true});

await page.goto("http://localhost:3000/providers", {waitUntil: "networkidle"});
const providers = await page.textContent("body");
const rows = await page.locator("table tbody tr").allTextContents();
console.log("provider rows:", rows.length);
console.log("any mock row :", rows.some((r) => /mock/i.test(r)));
console.log("mock metric  :", (providers ?? "").match(/mock\s*\n?\s*(\d+)/i)?.[1] ?? "n/a");
await page.screenshot({path: "C:/Users/Asus/AppData/Local/Temp/opencode/fresh-providers.png", fullPage: true});

await page.goto("http://localhost:3000/channels", {waitUntil: "networkidle"});
const channels = await page.textContent("body");
console.log("channels page empty-ish:", !/Little Lantern/i.test(channels ?? ""));
await page.screenshot({path: "C:/Users/Asus/AppData/Local/Temp/opencode/fresh-channels.png", fullPage: true});

await browser.close();
