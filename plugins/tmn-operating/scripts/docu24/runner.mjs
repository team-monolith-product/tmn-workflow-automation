import { createHash } from "node:crypto";

import { DocumentJobError, assertFinalApproval } from "./contracts.mjs";

const ORIGIN = "https://docu.gdoc.go.kr";
const HOME_URL = `${ORIGIN}/index.do`;
const SENT_DOCUMENTS_URL = `${ORIGIN}/doc/snd/sendDocList.do`;
const LOGIN_URL_PATTERN = /\/cmm\/main\/loginForm\.do/;
const WRITE_URL_PATTERN = /\/doc\/wte\/docWriteForm\.do/;
const DETAIL_URL_PATTERN = /\/doc\/snd\/sendDocDetail\.do/;

function normalizeText(value) {
  return value.replaceAll(/\s+/g, " ").trim();
}

function summaryId(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex").slice(0, 16);
}

async function expectExactly(locator, description) {
  const count = await locator.count();
  if (count !== 1) {
    throw new DocumentJobError(`${description} must match exactly one element; found ${count}.`);
  }
  return locator;
}

async function expectUrl(page, pattern, description) {
  if (!pattern.test(page.url())) {
    throw new DocumentJobError(`${description}: unexpected URL ${page.url()}`);
  }
}

export class Document24Runner {
  constructor(page) {
    this.page = page;
  }

  async openHome() {
    await this.page.goto(HOME_URL, { waitUntil: "domcontentloaded" });
  }

  async requiresLoginForSentDocuments() {
    await this.page.goto(SENT_DOCUMENTS_URL, { waitUntil: "domcontentloaded" });
    return LOGIN_URL_PATTERN.test(this.page.url());
  }

  async findReusableDocuments(search) {
    await this.page.goto(SENT_DOCUMENTS_URL, { waitUntil: "domcontentloaded" });
    await expectUrl(this.page, /\/doc\/snd\/sendDocList\.do/, "Sent documents list");
    const byRowText = new Map();
    for (const query of search.titleTerms) await this.#collectSentDocumentRows(query, byRowText, "title");
    for (const query of search.recipientTerms) await this.#collectSentDocumentRows(query, byRowText, "recipient");
    return [...byRowText.values()];
  }

  async prepare(job) {
    const priorCandidates = await this.findReusableDocuments(job.sentDocumentSearch);
    if (job.kind === "reuse") {
      await this.#openReusableDocument(job.sourceDocument);
    } else {
      await this.#openNewDocument();
    }

    await this.#confirmSubmissionChecks();
    await this.#selectRecipient(job.recipient, job.expectedRecipient);

    if (job.title) {
      await this.#replaceField("문서제목", job.title);
    }
    await this.#replaceBody(job.body);

    if (job.kind === "new") {
      await this.#uploadAttachments(job.attachments);
    }

    await this.#openPreview();
    return this.readPreviewSummary(job, priorCandidates.length);
  }

  async send(approval, summary) {
    assertFinalApproval(approval, summary);
    await this.#previewReady();
    await (await expectExactly(this.page.getByRole("button", { name: "보내기", exact: true }), "Final send button")).click();
  }

  async #openReusableDocument(source) {
    await this.page.goto(SENT_DOCUMENTS_URL, { waitUntil: "domcontentloaded" });
    await expectUrl(this.page, /\/doc\/snd\/sendDocList\.do/, "Sent documents list");
    const table = await expectExactly(this.page.getByRole("table", { name: "보낸 문서함", exact: true }), "Sent documents table");
    let rows = table.getByRole("row").filter({ hasText: source.title });
    if (source.recipient) rows = rows.filter({ hasText: source.recipient });
    if (source.sentAt) rows = rows.filter({ hasText: source.sentAt });
    if (source.documentNumber) rows = rows.filter({ hasText: source.documentNumber });
    const row = await expectExactly(rows, "Reusable document candidate");
    await (await expectExactly(row.getByRole("link", { name: source.title, exact: true }), "Reusable document title")).click();
    await expectUrl(this.page, DETAIL_URL_PATTERN, "Sent document detail");
    await (await expectExactly(this.page.getByRole("button", { name: "재작성", exact: true }), "Rewrite button")).click();
    await this.page.waitForURL(WRITE_URL_PATTERN, { waitUntil: "domcontentloaded" });
  }

  async #openNewDocument() {
    await this.openHome();
    await (await expectExactly(this.page.getByRole("link", { name: /문서 보내기/ }), "Document send link")).click();
    await this.page.waitForURL(WRITE_URL_PATTERN, { waitUntil: "domcontentloaded" });
  }

  async #collectSentDocumentRows(query, byRowText, field) {
    if (field === "title") {
      await (await expectExactly(this.page.locator("#defaultSearchWord:visible"), "Sent documents title search field")).fill(query);
      await (await expectExactly(this.page.locator("button:visible").filter({ hasText: /^검색$/ }), "Sent documents title search button")).click();
    } else {
      const openDetailSearch = this.page.getByRole("button", { name: "상세검색 열기", exact: true });
      if (await openDetailSearch.count()) await openDetailSearch.click();
      await (await expectExactly(this.page.getByRole("button", { name: "문서제목", exact: true }), "Sent documents search field menu")).click();
      const menu = await expectExactly(this.page.getByRole("menu"), "Sent documents search field options");
      await (await expectExactly(menu.getByText("받은기관", { exact: true }), "Recipient search field option")).click();
      await (await expectExactly(this.page.locator("#searchWord:visible"), "Sent documents recipient search field")).fill(query);
      await (await expectExactly(this.page.locator("#btnSearch:visible"), "Sent documents recipient search button")).click();
    }

    await this.page.waitForTimeout(1000);
    const table = await expectExactly(this.page.getByRole("table", { name: "보낸 문서함", exact: true }), "Sent documents table");
    const rows = await table.getByRole("row").all();
    for (const row of rows.slice(1)) {
      const rawText = normalizeText(await row.innerText());
      const candidate = byRowText.get(rawText) ?? { rawText, matchedQueries: [] };
      candidate.matchedQueries.push(query);
      byRowText.set(rawText, candidate);
    }
  }

  async #confirmSubmissionChecks() {
    const checks = await this.page.getByRole("checkbox", { name: "예", exact: true }).all();
    if (checks.length !== 4) {
      throw new DocumentJobError(`Expected four submission confirmation checkboxes; found ${checks.length}.`);
    }
    for (const check of checks) await check.check();
  }

  async #selectRecipient(query, expectedName) {
    await (await expectExactly(this.page.getByRole("button", { name: "받는기관 검색", exact: true }), "Recipient search button")).click();
    const dialog = await expectExactly(this.page.getByRole("dialog"), "Recipient search dialog");
    const input = await expectExactly(dialog.getByRole("textbox"), "Recipient search field");
    await input.fill(query);
    await (await expectExactly(dialog.getByRole("button", { name: "검색", exact: true }), "Recipient dialog search button")).click();
    await (await expectExactly(dialog.getByText(expectedName, { exact: true }), "Exact recipient result")).click();
    const selected = await (await expectExactly(this.page.getByRole("textbox", { name: "받는기관", exact: true }), "Recipient field")).inputValue();
    if (selected !== expectedName) {
      throw new DocumentJobError(`Recipient selection mismatch: expected ${expectedName}, got ${selected}.`);
    }
  }

  async #replaceField(label, value) {
    const field = await expectExactly(this.page.getByRole("textbox", { name: label, exact: true }), `${label} field`);
    await field.fill(value);
    if ((await field.inputValue()) !== value) {
      throw new DocumentJobError(`${label} did not retain the requested value.`);
    }
  }

  async #replaceBody(body) {
    const frameSelector = 'iframe[src*="hwpctrlmain"]:visible';
    await expectExactly(this.page.locator(frameSelector), "Visible Web Hangeul editor iframe");
    const editor = this.page.frameLocator(frameSelector);
    const area = await expectExactly(editor.getByRole("textbox"), "Web Hangeul body editor");
    await area.fill(body);
    if ((await area.inputValue()) !== body) {
      throw new DocumentJobError("Body editor did not retain the requested text.");
    }
  }

  async #uploadAttachments(attachments) {
    if (attachments.length === 0) return;
    const chooserPromise = this.page.waitForEvent("filechooser");
    await (await expectExactly(this.page.getByRole("button", { name: "파일 추가", exact: true }), "File add button")).click();
    const chooser = await chooserPromise;
    await chooser.setFiles(attachments.map((attachment) => attachment.path));
    for (const attachment of attachments) {
      const filename = attachment.path.split(/[\\/]/).at(-1);
      await expectExactly(this.page.getByText(filename, { exact: true }), `Uploaded attachment ${filename}`);
    }
  }

  async #openPreview() {
    const button = await expectExactly(this.page.getByRole("button", { name: "전송요청", exact: true }), "Transfer request button");
    if (!(await button.isEnabled())) {
      throw new DocumentJobError("Transfer request button is disabled. Required fields are incomplete.");
    }
    await button.click();
    await this.#previewReady();
  }

  async #previewReady() {
    await expectExactly(this.page.getByRole("button", { name: "수정", exact: true }), "Preview modify button");
    await expectExactly(this.page.getByRole("button", { name: "보내기", exact: true }), "Preview send button");
  }

  async readPreviewSummary(job, priorCandidateCount) {
    await this.#previewReady();
    const previewText = normalizeText(await this.page.locator("body").innerText());
    const expectedValues = [job.expectedRecipient, job.title, job.body].filter(Boolean);
    for (const expectedValue of expectedValues) {
      if (!previewText.includes(expectedValue)) {
        throw new DocumentJobError(`Preview does not contain the expected value: ${expectedValue}`);
      }
    }
    const attachments = job.kind === "new" ? job.attachments.map((attachment) => attachment.path.split(/[\\/]/).at(-1)) : [];
    const summary = {
      recipient: job.expectedRecipient,
      title: job.title,
      attachmentNames: attachments,
      priorCandidateCount,
      previewHash: createHash("sha256").update(previewText).digest("hex"),
    };
    return { id: summaryId(summary), ...summary };
  }
}
