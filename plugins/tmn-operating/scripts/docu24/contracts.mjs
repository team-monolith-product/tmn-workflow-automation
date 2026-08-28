import { statSync } from "node:fs";

export class DocumentJobError extends Error {}

const MAX_ATTACHMENT_BYTES = 500 * 1024 * 1024;

function requiredString(value, name) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new DocumentJobError(`${name} is required.`);
  }
  return value.trim();
}

function optionalString(value, name) {
  if (value == null) return undefined;
  return requiredString(value, name);
}

export function parseSearchRequest(raw) {
  const request = raw ?? {};
  const titleTerms = Array.isArray(request.titleTerms)
    ? request.titleTerms.map((term, index) => requiredString(term, `titleTerms[${index}]`))
    : [];
  const recipientTerms = Array.isArray(request.recipientTerms)
    ? request.recipientTerms.map((term, index) => requiredString(term, `recipientTerms[${index}]`))
    : [];

  if (titleTerms.length === 0 && recipientTerms.length === 0) {
    throw new DocumentJobError("At least one titleTerms or recipientTerms value is required.");
  }

  return { titleTerms, recipientTerms };
}

export function parsePreparationJob(raw) {
  const job = raw ?? {};
  const kind = requiredString(job.kind, "kind");
  if (kind !== "reuse" && kind !== "new") {
    throw new DocumentJobError("kind must be 'reuse' or 'new'.");
  }

  const common = {
    kind,
    sentDocumentSearch: parseSearchRequest(job.sentDocumentSearch),
    recipient: requiredString(job.recipient, "recipient"),
    expectedRecipient: requiredString(job.expectedRecipient ?? job.recipient, "expectedRecipient"),
    body: requiredString(job.body, "body"),
    title: optionalString(job.title, "title"),
    confirmSubmissionChecks: job.confirmSubmissionChecks === true,
  };

  if (!common.confirmSubmissionChecks) {
    throw new DocumentJobError("confirmSubmissionChecks must be true before a document can be prepared.");
  }

  if (kind === "reuse") {
    const source = job.sourceDocument ?? {};
    return {
      ...common,
      sourceDocument: {
        title: requiredString(source.title, "sourceDocument.title"),
        recipient: optionalString(source.recipient, "sourceDocument.recipient"),
        sentAt: optionalString(source.sentAt, "sourceDocument.sentAt"),
        documentNumber: optionalString(source.documentNumber, "sourceDocument.documentNumber"),
      },
    };
  }

  if (!common.title) {
    throw new DocumentJobError("title is required for a new document.");
  }
  if (job.reuseDecision !== "no-reusable-candidate") {
    throw new DocumentJobError("new documents require reuseDecision='no-reusable-candidate' after searching sent documents.");
  }

  const attachments = Array.isArray(job.attachments) ? job.attachments : [];
  const normalizedAttachments = attachments.map((attachment, index) => {
    const path = requiredString(attachment?.path ?? attachment, `attachments[${index}].path`);
    const info = statSync(path);
    if (!info.isFile()) {
      throw new DocumentJobError(`attachments[${index}] is not a file: ${path}`);
    }
    return { path, bytes: info.size };
  });
  const totalBytes = normalizedAttachments.reduce((total, attachment) => total + attachment.bytes, 0);
  if (totalBytes > MAX_ATTACHMENT_BYTES) {
    throw new DocumentJobError("Attachments exceed Document24's 500MB limit.");
  }

  return { ...common, reuseDecision: job.reuseDecision, attachments: normalizedAttachments };
}

export function assertFinalApproval(approval, summary) {
  if (approval?.confirmSend !== true) {
    throw new DocumentJobError("Final send requires an explicit confirmSend=true approval.");
  }
  if (approval.summaryId !== summary.id) {
    throw new DocumentJobError("Final approval does not match the reviewed document summary.");
  }
}
