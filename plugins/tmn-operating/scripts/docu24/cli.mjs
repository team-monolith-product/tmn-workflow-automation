import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";

import { chromium } from "playwright";

import { DocumentJobError, assertFinalApproval, parsePreparationJob, parseSearchRequest } from "./contracts.mjs";
import { Document24Runner } from "./runner.mjs";

function usage() {
  return [
    "Usage:",
    "  npm run docu24 -- search --job search.json --profile-dir /absolute/profile",
    "  npm run docu24 -- prepare --job prepared-job.json --profile-dir /absolute/profile",
    "  npm run docu24 -- send --job prepared-job.json --approval approval.json --profile-dir /absolute/profile",
  ].join("\n");
}

function readArgument(name, args) {
  const index = args.indexOf(name);
  return index === -1 ? undefined : args[index + 1];
}

async function readJson(path) {
  return JSON.parse(await readFile(resolve(path), "utf8"));
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  const jobPath = readArgument("--job", args);
  const profileDir = readArgument("--profile-dir", args);
  if (!command || !jobPath || !profileDir) throw new DocumentJobError(usage());

  const context = await chromium.launchPersistentContext(resolve(profileDir), {
    channel: "chrome",
    headless: false,
  });
  const page = context.pages()[0] ?? await context.newPage();
  const runner = new Document24Runner(page);

  try {
    const rawJob = await readJson(jobPath);
    if (command === "search") {
      console.log(JSON.stringify(await runner.findReusableDocuments(parseSearchRequest(rawJob)), null, 2));
      return;
    }

    const job = parsePreparationJob(rawJob);
    const summary = await runner.prepare(job);
    console.log(JSON.stringify(summary, null, 2));

    if (command === "prepare") {
      const prompt = createInterface({ input, output });
      await prompt.question("미리보기를 검토한 뒤 Enter를 눌러 종료하세요. 실제 발송은 승인 JSON으로 send 명령을 실행합니다. ");
      prompt.close();
      return;
    }

    if (command !== "send") throw new DocumentJobError(usage());
    const approvalPath = readArgument("--approval", args);
    if (!approvalPath) throw new DocumentJobError("send requires --approval approval.json.");
    const approval = await readJson(approvalPath);
    assertFinalApproval(approval, summary);
    await runner.send(approval, summary);
  } finally {
    await context.close();
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
