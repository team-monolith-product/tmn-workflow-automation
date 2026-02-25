#!/usr/bin/env node

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { readFileSync, writeFileSync } from "fs";
import { execFileSync } from "child_process";
import { join } from "path";

const CHAR_LIMIT = 5000;
const REPO_PATH = process.env.MEMORY_REPO_PATH;
const MEMORY_FILE = "MEMORY.md";

if (!REPO_PATH) {
  console.error("MEMORY_REPO_PATH is required");
  process.exit(1);
}

const filePath = join(REPO_PATH, MEMORY_FILE);

function git(...args: string[]): string {
  return execFileSync("git", ["-C", REPO_PATH!, ...args], { encoding: "utf-8" }).trim();
}

function readMemory(): string {
  try {
    return readFileSync(filePath, "utf-8");
  } catch {
    return "";
  }
}

const server = new Server(
  { name: "aha-memory", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "memory_update",
      description:
        "Update procedural memory. Replaces old_string with new_string in MEMORY.md. " +
        "To append, use an empty old_string. " +
        "Total content must not exceed 5000 characters. Write in English.",
      inputSchema: {
        type: "object" as const,
        properties: {
          old_string: {
            type: "string",
            description: "The text to find and replace. Empty string to append.",
          },
          new_string: {
            type: "string",
            description: "The replacement text.",
          },
          reason: {
            type: "string",
            description:
              "Why this change is being made and what improvement is expected. " +
              "This becomes the git commit message for future reference.",
          },
        },
        required: ["old_string", "new_string", "reason"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "memory_update") {
    return {
      content: [{ type: "text", text: `Unknown tool: ${request.params.name}` }],
      isError: true,
    };
  }

  const { old_string, new_string, reason } = request.params.arguments as {
    old_string: string;
    new_string: string;
    reason: string;
  };

  if (!reason || reason.trim().length === 0) {
    return {
      content: [{ type: "text", text: "reason is required." }],
      isError: true,
    };
  }

  const content = readMemory();

  // apply edit
  let updated: string;
  if (old_string === "") {
    updated = content + new_string;
  } else {
    const idx = content.indexOf(old_string);
    if (idx === -1) {
      return {
        content: [{ type: "text", text: "old_string not found in memory." }],
        isError: true,
      };
    }
    if (content.indexOf(old_string, idx + 1) !== -1) {
      return {
        content: [{ type: "text", text: "old_string is ambiguous (multiple matches). Provide more context." }],
        isError: true,
      };
    }
    updated = content.slice(0, idx) + new_string + content.slice(idx + old_string.length);
  }

  // char limit check
  if (updated.length > CHAR_LIMIT) {
    return {
      content: [{
        type: "text",
        text: `Rejected: result would be ${updated.length} chars, exceeding limit of ${CHAR_LIMIT}. Current: ${content.length} chars. Free: ${CHAR_LIMIT - content.length} chars.`,
      }],
      isError: true,
    };
  }

  // write + git commit + push
  writeFileSync(filePath, updated, "utf-8");

  try {
    git("add", MEMORY_FILE);
    const diff = git("diff", "--cached", "--stat");
    if (!diff) {
      return { content: [{ type: "text", text: "No changes detected." }] };
    }

    const commitMsg = `memory: ${reason}\n\n(${updated.length}/${CHAR_LIMIT} chars)`;
    git("commit", "-m", commitMsg);
    git("push");
  } catch (e) {
    return {
      content: [{ type: "text", text: `Git error: ${(e as Error).message}` }],
      isError: true,
    };
  }

  return {
    content: [{
      type: "text",
      text: `Updated. ${updated.length}/${CHAR_LIMIT} chars used.`,
    }],
  };
});

const transport = new StdioServerTransport();
await server.connect(transport);
