import { expect, test } from "@playwright/test";

const BASE = "http://localhost:9119";
const MEMBER_REPLY = "web-room-member-reply";

let token = "";
const createdProfiles: string[] = [];

function authHeaders() {
  return { "X-Hermes-Session-Token": token, "Content-Type": "application/json" };
}

test.beforeAll(async () => {
  const html = await (await fetch(`${BASE}/`)).text();
  token = html.match(/window\.__HERMES_SESSION_TOKEN__="([^"]*)"/)?.[1] ?? "";
  if (!token) throw new Error("Could not extract session token from SPA");
});

test.afterEach(async () => {
  for (const profile of createdProfiles.splice(0)) {
    const response = await fetch(`${BASE}/api/profiles/${profile}`, { method: "DELETE", headers: authHeaders() });
    expect(response.ok).toBe(true);
  }
});

test("Rooms: create a team, delegate work, and reload its durable workspace", async ({ page }) => {
  test.setTimeout(180_000);
  const suffix = `${Date.now()}`;
  const leaderProfile = `room_leader_${suffix}`;
  const teamName = `E2E Rooms ${suffix}`;
  const taskText = `Durable browser brief ${suffix} for @${leaderProfile}`;

  const profileResponse = await fetch(`${BASE}/api/profiles`, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ name: leaderProfile, clone_from: "default" }),
  });
  expect(profileResponse.ok, await profileResponse.text()).toBe(true);
  createdProfiles.push(leaderProfile);

  await page.context().setExtraHTTPHeaders({ "X-Hermes-Session-Token": token });
  await page.goto(`${BASE}/rooms`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "New team", exact: true }).click();

  const dialog = page.getByTestId("create-team-dialog");
  await dialog.getByRole("textbox", { name: "Team name" }).fill(teamName);
  await dialog.locator("fieldset label").filter({ hasText: /^default/i }).getByRole("checkbox").check();
  await dialog.locator("fieldset label").filter({ hasText: leaderProfile }).getByRole("checkbox").check();
  await dialog.getByRole("combobox", { name: "Team leader" }).selectOption(leaderProfile);

  const createResponsePromise = page.waitForResponse((response) =>
    response.url() === `${BASE}/api/rooms` && response.request().method() === "POST");
  await dialog.getByTestId("create-team-submit").click();
  const createResponse = await createResponsePromise;
  expect(createResponse.ok()).toBe(true);
  const roomId = (await createResponse.json()).room.room_id as string;

  await expect(dialog).toBeHidden();
  await expect(page.getByRole("heading", { name: teamName, exact: true, level: 1 })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Work recipient" }).locator("option")).toHaveCount(2);
  await page.getByRole("combobox", { name: "Work recipient" }).selectOption("default");
  await page.getByRole("textbox", { name: "New team task" }).fill(taskText);

  const sendResponsePromise = page.waitForResponse((response) =>
    response.url() === `${BASE}/api/rooms/${roomId}/messages` && response.request().method() === "POST");
  await page.getByTestId("delegate-work-submit").click();
  const sendResponse = await sendResponsePromise;
  expect(sendResponse.ok()).toBe(true);
  expect(sendResponse.request().postDataJSON()).toMatchObject({ recipient: "default", text: taskText });

  await expect.poll(async () => {
    const response = await fetch(`${BASE}/api/rooms/${roomId}/workspace`, { headers: authHeaders() });
    if (!response.ok) return "";
    const workspace = await response.json();
    return workspace.conversation
      .filter((item: { actor_kind: string; actor_id: string }) => item.actor_kind === "member" && item.actor_id.includes("default"))
      .map((item: { text: string }) => item.text)
      .join("\n");
  }, { timeout: 90_000 }).toContain(MEMBER_REPLY);

  const workspaceResponse = await fetch(`${BASE}/api/rooms/${roomId}/workspace`, { headers: authHeaders() });
  expect(workspaceResponse.ok).toBe(true);
  const workspace = await workspaceResponse.json();
  const memberIds = workspace.conversation
    .filter((item: { actor_kind: string }) => item.actor_kind === "member")
    .map((item: { actor_id: string }) => item.actor_id);
  expect(memberIds).toHaveLength(1);
  expect(memberIds[0]).toContain("default");

  await page.reload({ waitUntil: "networkidle" });
  await expect(page.getByRole("heading", { name: teamName, exact: true, level: 1 })).toBeVisible();
  const completedTask = page.locator("article").filter({ hasText: taskText });
  await expect(completedTask).toBeVisible();
  await expect(completedTask.getByText("completed", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "graph", exact: true }).click();
  await expect(page.getByRole("button", { name: new RegExp(`${taskText}.*@default`) })).toBeVisible();
  await page.getByRole("button", { name: "attempts", exact: true }).click();
  await expect(page.getByText(/Generation 1/i)).toBeVisible();

  await page.getByRole("tab", { name: "conversation", exact: true }).click();
  const conversation = page.locator('section[aria-label="Conversation thread"]');
  await expect(conversation).toContainText(taskText);
  await expect(conversation).toContainText(MEMBER_REPLY);

  await page.getByRole("tab", { name: "Timeline", exact: true }).click();
  await expect(page.getByText("Research brief received", { exact: true })).toBeVisible();
  await expect(page.getByText("Team update received", { exact: true })).toBeVisible();
  await expect(page.getByText("Work completed", { exact: true })).toBeVisible();
});
