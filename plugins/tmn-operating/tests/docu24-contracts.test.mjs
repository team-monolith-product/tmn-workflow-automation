import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { DocumentJobError, assertFinalApproval, parsePreparationJob, parseSearchRequest } from "../scripts/docu24/contracts.mjs";
import { Document24Runner } from "../scripts/docu24/runner.mjs";

test("search requires a deterministic title or recipient term", () => {
  assert.throws(() => parseSearchRequest({}), DocumentJobError);
  assert.deepEqual(parseSearchRequest({ titleTerms: ["강사위촉"] }), {
    titleTerms: ["강사위촉"],
    recipientTerms: [],
  });
});

test("final send approval must bind to the reviewed preview", () => {
  const summary = { id: "preview-1" };
  assert.throws(() => assertFinalApproval({ confirmSend: false, summaryId: "preview-1" }, summary), DocumentJobError);
  assert.throws(() => assertFinalApproval({ confirmSend: true, summaryId: "old-preview" }, summary), DocumentJobError);
  assert.doesNotThrow(() => assertFinalApproval({ confirmSend: true, summaryId: "preview-1" }, summary));
});

test("new documents require verified submission checks and local attachments", async () => {
  const directory = await mkdtemp(join(tmpdir(), "docu24-test-"));
  const attachment = join(directory, "attachment.pdf");
  await writeFile(attachment, "test attachment");

  try {
    assert.throws(
      () => parsePreparationJob({ kind: "new", recipient: "기관", title: "제목", body: "본문" }),
      DocumentJobError,
    );
    assert.throws(
      () => parsePreparationJob({
        kind: "new",
        recipient: "기관",
        title: "제목",
        body: "본문",
        confirmSubmissionChecks: true,
        sentDocumentSearch: { titleTerms: ["기존 공문 검색어"] },
      }),
      DocumentJobError,
    );
    assert.deepEqual(
      parsePreparationJob({
        kind: "new",
        recipient: "기관",
        title: "제목",
        body: "본문",
        confirmSubmissionChecks: true,
        sentDocumentSearch: { titleTerms: ["기존 공문 검색어"] },
        reuseDecision: "no-reusable-candidate",
        attachments: [attachment],
      }).attachments,
      [{ path: attachment, bytes: 15 }],
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("runner never clicks final send before approval matches the preview", async () => {
  let clicks = 0;
  const sendButton = {
    count: async () => 1,
    click: async () => { clicks += 1; },
  };
  const modifyButton = { count: async () => 1 };
  const page = {
    getByRole: (_role, options) => (options.name === "보내기" ? sendButton : modifyButton),
  };
  const runner = new Document24Runner(page);
  const summary = { id: "preview-1" };

  await assert.rejects(runner.send({ confirmSend: false, summaryId: "preview-1" }, summary), DocumentJobError);
  assert.equal(clicks, 0);

  await runner.send({ confirmSend: true, summaryId: "preview-1" }, summary);
  assert.equal(clicks, 1);
});

test("runner detects that the sent-documents route requires an interactive login", async () => {
  const page = {
    goto: async () => {},
    url: () => "https://docu.gdoc.go.kr/cmm/main/loginForm.do?redirect=%2Fdoc%2Fsnd%2FsendDocList.do",
  };

  const runner = new Document24Runner(page);
  assert.equal(await runner.requiresLoginForSentDocuments(), true);
});
