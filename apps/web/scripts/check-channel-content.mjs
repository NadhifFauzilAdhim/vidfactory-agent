// Open the Little Lantern channel detail page and dump what it shows.
import {chromium} from "playwright";

const browser = await chromium.launch();
const page = await (await browser.newContext({viewport: {width: 1440, height: 1300}})).newPage();

await page.goto("http://localhost:3000/login", {waitUntil: "networkidle"});
await page.fill('input[type="email"]', "admin@factory.app");
await page.fill('input[type="password"]', "factory-admin-2026");
await page.getByRole("button", {name: /enter studio/i}).click();
await page.waitForURL("http://localhost:3000/", {timeout: 25000});

await page.goto("http://localhost:3000/channels", {waitUntil: "networkidle"});
const manage = page.getByRole("link", {name: /manage/i}).first();
console.log("manage href:", await manage.getAttribute("href"));
await manage.click();
await page.waitForLoadState("networkidle");
await page.waitForTimeout(1500);
console.log("url:", page.url());

const body = await page.textContent("body");
for (const needle of ["Anak usia 3", "Lila", "Bima", "Kak Mia", "kekerasan", "Indonesia"]) {
  console.log(`${needle}:`, (body ?? "").includes(needle));
}
await page.screenshot({path: "C:/Users/Asus/AppData/Local/Temp/opencode/channel-detail.png", fullPage: true});
await browser.close();
