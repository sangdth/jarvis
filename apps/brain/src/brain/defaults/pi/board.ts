/**
 * Brain board extension for Pi 1.5.0 (`-e board.ts`).
 *
 * Exposes three structured board tools (`upsert_task`, `record_note`, `defer_for_approval`)
 * that talk to `reconciler.py` over the per-run Unix socket `BRAIN_RPC_SOCK`, and installs
 * a `tool_call` guardrail confining file edits/writes and destructive bash commands to
 * `BRAIN_WORKTREE_DIR`.
 */

import { existsSync, realpathSync } from "node:fs";
import { createConnection } from "node:net";
import { dirname, resolve, sep } from "node:path";

import {
	createBashToolDefinition,
	createEditToolDefinition,
	createWriteToolDefinition,
	type ExtensionAPI,
	type ToolCallEvent,
	type ToolCallEventResult,
} from "@earendil-works/pi-coding-agent";

function _nearest_existing_resolved(targetPath: string): string {
	const abs = resolve(targetPath);
	let cur = abs;
	const tail: string[] = [];
	while (!existsSync(cur)) {
		const parent = dirname(cur);
		if (parent === cur) break;
		tail.unshift(cur.slice(parent.length).replace(/^[\\/]+/, ""));
		cur = parent;
	}
	const base = existsSync(cur) ? realpathSync(cur) : cur;
	return tail.length > 0 ? resolve(base, ...tail) : base;
}

function isWithinDir(targetPath: string, rootDir: string): boolean {
	const resolvedRoot = _nearest_existing_resolved(rootDir);
	const resolvedTarget = _nearest_existing_resolved(targetPath);
	return resolvedTarget === resolvedRoot || resolvedTarget.startsWith(`${resolvedRoot}${sep}`);
}

function callBoardRpc(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
	const sockPath = process.env.BRAIN_RPC_SOCK;
	if (!sockPath) {
		return Promise.reject(new Error("BRAIN_RPC_SOCK is not set."));
	}
	return new Promise((resolveRpc, rejectRpc) => {
		const sock = createConnection(sockPath);
		let buf = "";
		sock.setEncoding("utf8");
		sock.on("connect", () => {
			sock.write(`${JSON.stringify({ method, params })}\n`);
		});
		sock.on("data", (chunk: string) => {
			buf += chunk;
			const nl = buf.indexOf("\n");
			if (nl >= 0) {
				sock.end();
				try {
					const parsed = JSON.parse(buf.slice(0, nl)) as { ok?: boolean; result?: Record<string, unknown>; error?: string };
					if (parsed.ok) resolveRpc(parsed.result ?? {});
					else rejectRpc(new Error(parsed.error ?? "Board RPC error"));
				} catch (err) {
					rejectRpc(err as Error);
				}
			}
		});
		sock.on("error", (err) => rejectRpc(err));
	});
}

const BLOCKED_BASH_PATTERNS = [
	/\bgit\s+push\b/i,
	/\bgit\s+checkout\s+(master|main)\b/i,
	/\brm\s+-[a-z]*r[a-z]*f\s+\/(?:\s|$)/i,
	/\bstate\.db\b/i,
	/\bsqlite3\b/i,
];

function checkWorktreeWriteGuard(rawPath: string, worktreeDir: string, taskKind?: string): string | null {
	const targetPath = resolve(worktreeDir, rawPath);
	if (!isWithinDir(targetPath, worktreeDir)) {
		return `Write blocked: path ${rawPath} resolves outside the isolated worktree (${worktreeDir}).`;
	}
	if (taskKind === "problem" && existsSync(targetPath)) {
		const rel = _nearest_existing_resolved(targetPath).slice(_nearest_existing_resolved(worktreeDir).length + 1);
		if (/^tests?(?:[\\/]|$)/i.test(rel)) {
			return "Modifying existing test files under tests/ while fixing a problem is blocked.";
		}
	}
	return null;
}

function checkBashCommandGuard(cmd: string, taskKind?: string): string | null {
	for (const pat of BLOCKED_BASH_PATTERNS) {
		if (pat.test(cmd)) {
			return `Bash command blocked by Brain guardrail (${pat}).`;
		}
	}
	if (taskKind === "problem" && /\b(?:rm|git\s+rm)\b.*\btests?\//i.test(cmd)) {
		return "Deleting test files while fixing a problem is blocked.";
	}
	return null;
}

function extractTextBlocks(content: Array<{ type: string; text?: string }>): string {
	return content
		.filter((b) => b.type === "text" && typeof b.text === "string")
		.map((b) => b.text as string)
		.join("\n");
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "upsert_task",
		label: "Upsert Brain Task",
		description:
			"Create or update a Brain goal or problem in an allowed repository. Goals require a metric_target.",
		promptSnippet: "Create or update a verifiable goal or problem task on the Brain board.",
		parameters: {
			type: "object",
			properties: {
				id: { type: "string", description: "Optional existing task ID (e.g. SG-1 or PRB-1)" },
				parent_id: { type: "string", description: "Optional parent goal ID" },
				kind: { type: "string", enum: ["goal", "problem"] },
				mode: {
					type: "string",
					enum: ["ASSIST", "AUTONOMOUS"],
					description:
						"'ASSIST' for tasks related to the current topic or active tasks resolved in ASSIST mode; 'AUTONOMOUS' for tasks resolved in AUTONOMOUS mode",
				},
				repo: { type: "string", description: "Relative repo path under ~/Projects (must be in ALLOWED_REPOS)" },
				title: { type: "string" },
				spec: { type: "string", description: "Hypothesis for a goal, or symptom/stacktrace for a problem" },
				metric_target: { type: "string", description: "Required when kind='goal' (e.g. 'pytest passes')" },
				verify_cmd: { type: "string", description: "Deterministic shell verification command" },
				priority: { type: "integer", description: "0 = urgent P0, 50 = normal, 90 = low" },
				child_strategy: {
					type: "string",
					enum: ["all", "any"],
					description: "'all' when all child steps must finish; 'any' for competing hypotheses",
				},
				ready: { type: "boolean", description: "True to mark ready for execution immediately" },
			},
			required: ["kind", "repo", "title", "spec"],
		} as never,
		async execute(_toolCallId, params: Record<string, unknown>) {
			const res = await callBoardRpc("upsert_task", params);
			return {
				content: [{ type: "text", text: JSON.stringify(res) }],
				details: res,
			};
		},
	});

	pi.registerTool({
		name: "record_note",
		label: "Record Progress Note",
		description: "Append a concise handoff note to the active task's progress markdown file.",
		promptSnippet: "Record a progress or handoff note for the current task.",
		parameters: {
			type: "object",
			properties: {
				note_markdown: { type: "string", description: "Markdown note summarizing findings or next steps" },
			},
			required: ["note_markdown"],
		} as never,
		async execute(_toolCallId, params: Record<string, unknown>) {
			const res = await callBoardRpc("record_note", params);
			return {
				content: [{ type: "text", text: JSON.stringify(res) }],
				details: res,
			};
		},
	});

	pi.registerTool({
		name: "defer_for_approval",
		label: "Request Owner Yes/No Approval",
		description:
			"Ask the owner a binary yes/no question before taking a sensitive action. Suspends this session in AUTONOMOUS mode or asks live in ASSIST mode.",
		promptSnippet: "Request binary yes/no approval from the owner.",
		parameters: {
			type: "object",
			properties: {
				summary_spoken: {
					type: "string",
					description: "One short sentence spoken aloud by Jarvis (e.g. 'Approve deploying migration X?')",
				},
				details: { type: "string", description: "Context or diff summary" },
			},
			required: ["summary_spoken"],
		} as never,
		async execute(_toolCallId, params: Record<string, unknown>, _signal, _onUpdate, ctx) {
			const sessionId = ctx.sessionManager.getSessionId();
			const res = await callBoardRpc("defer_for_approval", { ...params, session_id: sessionId });
			if (res.deferred) {
				ctx.abort();
			}
			return {
				content: [{ type: "text", text: JSON.stringify(res) }],
				details: res,
			};
		},
	});

	pi.registerTool({
		name: "report_diagnostic",
		label: "Report Workflow Diagnostic",
		description:
			"Record a structured diagnostic when a skill or procedure falls short, hits tool friction, or needs a recurring workaround.",
		promptSnippet: "Record a short diagnostic about skill/tool friction for future self-improvement.",
		parameters: {
			type: "object",
			properties: {
				summary: {
					type: "string",
					description: "Concise description of what went wrong or what recurring procedure was needed",
				},
			},
			required: ["summary"],
		} as never,
		async execute(_toolCallId, params: Record<string, unknown>) {
			const res = await callBoardRpc("report_diagnostic", params);
			return {
				content: [{ type: "text", text: JSON.stringify(res) }],
				details: res,
			};
		},
	});

	pi.registerTool({
		name: "propose_skill_change",
		label: "Propose Self-Improving Skill Change",
		description:
			"Propose creating, appending to, updating, or retiring a personal skill based on past attempt evidence. Prefer action='append' to add a single rule bullet without rewriting existing rules. Creating requires >= 2 attempt IDs; append/update requires >= 1 attempt ID and expected_sha256.",
		promptSnippet: "Submit a verified skill creation, incremental append, update, or retirement proposal.",
		parameters: {
			type: "object",
			properties: {
				action: { type: "string", enum: ["create", "append", "update", "retire"] },
				slug: { type: "string", description: "Lowercase hyphenated skill slug (e.g. 'pytest-async-fixtures')" },
				lesson: { type: "string", description: "One-sentence lesson learned from the evidence" },
				description: { type: "string", description: "When to load this skill (1-2 sentences; optional for 'append')" },
				body_markdown: { type: "string", description: "Markdown rule bullet (for 'append') or full body (<= 150 lines)" },
				evidence_attempt_ids: {
					type: "array",
					items: { type: "integer" },
					description: "Attempt IDs supporting this change (>= 2 for create, >= 1 for append/update)",
				},
				expected_sha256: { type: "string", description: "Required for action='append' or 'update': current skill sha256" },
				retire_slug: {
					type: "string",
					description: "Required when creating a skill at the 10-skill cap: slug of existing skill to retire",
				},
				retire_reason: {
					type: "string",
					description: "Why the new skill is more valuable than retire_slug",
				},
			},
			required: ["action", "slug", "lesson"],
		} as never,
		async execute(_toolCallId, params: Record<string, unknown>) {
			const res = await callBoardRpc("propose_skill_change", params);
			return {
				content: [{ type: "text", text: JSON.stringify(res) }],
				details: res,
			};
		},
	});

	pi.registerTool({
		name: "schedule_followup",
		label: "Schedule a Later Check",
		description:
			"Ask the brain to run a short read-only check later (delay >= 5 minutes). Use only when the work truly cannot finish in this turn; the answer reaches the owner through the outbox.",
		promptSnippet: "Schedule a one-shot follow-up check for later.",
		parameters: {
			type: "object",
			properties: {
				delay_minutes: { type: "integer", description: "Minutes from now (minimum 5, maximum 20160)" },
				reason: { type: "string", description: "What to check when the follow-up runs" },
			},
			required: ["delay_minutes", "reason"],
		} as never,
		async execute(_toolCallId, params: Record<string, unknown>) {
			const res = await callBoardRpc("schedule_followup", params);
			return {
				content: [{ type: "text", text: JSON.stringify(res) }],
				details: res,
			};
		},
	});

	pi.registerTool({
		name: "propose_memory_update",
		label: "Propose a Memory Update",
		description:
			"Improver only: write the small memory index (MEMORY.md) or one repository note (repos/<slug>.md), after checking the claim against traces and live files.",
		promptSnippet: "Propose one curated memory update from verified evidence.",
		parameters: {
			type: "object",
			properties: {
				name: { type: "string", description: "'MEMORY.md' or 'repos/<slug>.md'" },
				content: { type: "string", description: "Full markdown body (MEMORY.md <= 150 lines; repo note <= 80)" },
				expected_sha256: {
					type: "string",
					description: "Current sha256 when updating an existing file; omit only for a new file",
				},
			},
			required: ["name", "content"],
		} as never,
		async execute(_toolCallId, params: Record<string, unknown>) {
			const res = await callBoardRpc("propose_memory_update", params);
			return {
				content: [{ type: "text", text: JSON.stringify(res) }],
				details: res,
			};
		},
	});

	pi.registerTool({
		name: "research_scout",
		label: "Run Evidence-Preserving Research Scout",
		description:
			"Delegate read-only codebase or log reconnaissance to an isolated fast scout subagent. Every returned quote is verified byte-for-byte against files on disk before reaching your context.",
		promptSnippet: "Delegate read-only codebase/log reconnaissance and receive a byte-verified evidence receipt.",
		parameters: {
			type: "object",
			properties: {
				question: {
					type: "string",
					description: "Focused reconnaissance question (e.g. 'Find where verify_cmd output is parsed and list exact lines')",
				},
				paths: {
					type: "array",
					items: { type: "string" },
					description: "Optional relative file or directory paths to focus the scout on",
				},
			},
			required: ["question"],
		} as never,
		async execute(_toolCallId, params: Record<string, unknown>) {
			const res = await callBoardRpc("research_scout", params);
			const text = typeof res.receipt_text === "string" ? res.receipt_text : JSON.stringify(res);
			return {
				content: [{ type: "text", text }],
				details: res,
			};
		},
	});

	pi.registerTool({
		name: "edit_and_run",
		label: "Action Fusion: Edit File and Run Command",
		description:
			"SoL-Pi Action Fusion: apply an exact text edit (or create a file when old_text is empty) inside the worktree and immediately run its follow-up test/check command in one turn.",
		promptSnippet: "Edit a file and run its follow-up verification command in one fused turn.",
		parameters: {
			type: "object",
			properties: {
				path: { type: "string", description: "File path inside the worktree" },
				old_text: {
					type: "string",
					description: "Exact text to replace (empty string only when creating a new file)",
				},
				new_text: { type: "string", description: "Replacement text" },
				command: { type: "string", description: "Follow-up shell command to run if the edit succeeds" },
				timeout_seconds: { type: "integer", description: "Command timeout in seconds (default 60)" },
			},
			required: ["path", "old_text", "new_text", "command"],
		} as never,
		async execute(toolCallId, params: Record<string, unknown>, signal, _onUpdate, ctx) {
			const worktreeDir = process.env.BRAIN_WORKTREE_DIR ?? process.cwd();
			const taskKind = process.env.BRAIN_TASK_KIND;
			const rawPath = String(params.path ?? "");
			const oldText = String(params.old_text ?? "");
			const newText = String(params.new_text ?? "");
			const command = String(params.command ?? "");
			const rawTimeout = params.timeout_seconds;
			const timeoutSecs =
				typeof rawTimeout === "number" && Number.isFinite(rawTimeout) && rawTimeout > 0
					? rawTimeout
					: 60;

			const writeErr = checkWorktreeWriteGuard(rawPath, worktreeDir, taskKind);
			if (writeErr) {
				return {
					content: [{ type: "text", text: `${writeErr}\n\n[then_run:skipped] The file mutation did not complete successfully; the command was not run.` }],
					details: { status: "skipped" },
					isError: true,
				} as never;
			}
			const bashErr = checkBashCommandGuard(command, taskKind);
			if (bashErr) {
				return {
					content: [{ type: "text", text: `${bashErr}\n\n[then_run:skipped] Command blocked by guardrail.` }],
					details: { status: "skipped" },
					isError: true,
				} as never;
			}

			const execCtx = { ...ctx, cwd: worktreeDir };
			const targetPath = resolve(worktreeDir, rawPath);
			let mutationText = `Updated ${rawPath}.`;
			try {
				const mutRes =
					!existsSync(targetPath) && oldText === ""
						? await createWriteToolDefinition(worktreeDir).execute(
								toolCallId,
								{ path: rawPath, content: newText },
								signal,
								undefined,
								execCtx,
							)
						: await createEditToolDefinition(worktreeDir).execute(
								toolCallId,
								{ path: rawPath, edits: [{ oldText, newText }] },
								signal,
								undefined,
								execCtx,
							);
				mutationText = extractTextBlocks(mutRes.content as Array<{ type: string; text?: string }>) || mutationText;
				if ((mutRes as { isError?: boolean }).isError) {
					return {
						content: [{ type: "text", text: `${mutationText}\n\n[then_run:skipped] The file mutation did not complete successfully; the command was not run.` }],
						details: { status: "skipped" },
						isError: true,
					} as never;
				}
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				return {
					content: [{ type: "text", text: `${msg}\n\n[then_run:skipped] The file mutation did not complete successfully; the command was not run.` }],
					details: { status: "skipped" },
					isError: true,
				} as never;
			}

			const bashDef = createBashToolDefinition(worktreeDir, { exposeSessionEnvironment: false });
			try {
				const bashRes = await bashDef.execute(
					`${toolCallId}:then_run`,
					{ command, timeout: timeoutSecs },
					signal,
					undefined,
					execCtx,
				);
				const failed = Boolean((bashRes as { isError?: boolean }).isError);
				const outText = extractTextBlocks(bashRes.content as Array<{ type: string; text?: string }>);
				const tag = failed ? "[then_run:failed]" : "[then_run:succeeded]";
				return {
					content: [{ type: "text", text: [mutationText, outText ? `${tag}\n${outText}` : tag].join("\n\n") }],
					details: { status: failed ? "failed" : "succeeded" },
					...(failed ? { isError: true } : {}),
				} as never;
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				return {
					content: [{ type: "text", text: [mutationText, "[then_run:failed]", msg].join("\n\n") }],
					details: { status: "failed" },
					isError: true,
				} as never;
			}
		},
	});

	pi.on("tool_call", async (event: ToolCallEvent): Promise<ToolCallEventResult | undefined> => {
		const worktreeDir = process.env.BRAIN_WORKTREE_DIR;
		const skillsDir = process.env.BRAIN_SKILLS_DIR;
		const taskKind = process.env.BRAIN_TASK_KIND;

		if (event.toolName === "read" && skillsDir) {
			const rawPath = String((event.input as Record<string, unknown>).path ?? "");
			const targetPath = resolve(worktreeDir ?? process.cwd(), rawPath);
			if (isWithinDir(targetPath, skillsDir)) {
				const rel = _nearest_existing_resolved(targetPath).slice(_nearest_existing_resolved(skillsDir).length + 1);
				const slug = rel.split(/[\\/]/)[0];
				if (slug) {
					await callBoardRpc("record_skill_load", { slug }).catch(() => {});
				}
			}
		}

		if ((event.toolName === "edit" || event.toolName === "write") && worktreeDir) {
			const rawPath = String((event.input as Record<string, unknown>).path ?? "");
			const writeErr = checkWorktreeWriteGuard(rawPath, worktreeDir, taskKind);
			if (writeErr) {
				return { block: true, reason: writeErr };
			}
		}

		if (event.toolName === "bash") {
			const cmd = String((event.input as Record<string, unknown>).command ?? "");
			const bashErr = checkBashCommandGuard(cmd, taskKind);
			if (bashErr) {
				return { block: true, reason: bashErr };
			}
		}
		return undefined;
	});
}
