// Verify the containerised web app talks to the containerised API.
import {chromium} from "playwright";

const browser = await chromium.launch();
const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();

await page.goto("http://localhost:3000/login", {waitUntil: "networkidle"});
await page.fill('input[type="email"]', "admin@factory.app");
await page.fill('input[type="password"]', "factory-admin-2026");
await page.getByRole("button", {name: /enter studio/i}).click();
await page.waitForURL("http://localhost:3000/", {timeout: 25000});
console.log("logged in:", page.url());

await page.goto("http://localhost:3000/providers", {waitUntil: "networkidle"});
await page.getByText("remotion engine").waitFor({timeout: 20000});
const text = await page.textContent("body");
console.log("renderer chip:", /remotion/.test(text ?? ""));
console.log("google row  :", /YouTube \(Google OAuth\)/.test(text ?? ""));
console.log("oauth state :", (text ?? "").match(/\b(configured|unset)\b/)?.[0]);

await page.screenshot({path: "C:/Users/Asus/AppData/Local/Temp/opencode/docker-web.png"});
await browser.close();
