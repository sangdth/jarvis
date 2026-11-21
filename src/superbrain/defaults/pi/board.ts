/**
 * Superbrain board extension for Pi 1.5.0 (`-e board.ts`).
 *
 * Exposes three structured board tools (`upsert_task`, `record_note`, `defer_for_approval`)
 * that talk to `reconciler.py` over the per-run Unix socket `SUPERBRAIN_RPC_SOCK`, and installs
 * a `tool_call` guardrail confining file edits/writes and destructive bash commands to
 * `SUPERBRAIN_WORKTREE_DIR`.
 */

import { existsSync, realpathSync } from "node:fs";
import { createConnection } from "node:net";
import { dirname, resolve, sep } from "node:path";

import type { ExtensionAPI, ToolCallEvent, ToolCallEventResult } from "@earendil-works/pi-coding-agent";

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
	const sockPath = process.env.SUPERBRAIN_RPC_SOCK;
	if (!sockPath) {
		return Promise.reject(new Error("SUPERBRAIN_RPC_SOCK is not set."));
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
	/\bgit\s+push\s+.*--force\b/i,
	/\bgit\s+checkout\s+(master|main)\b/i,
	/\brm\s+-[a-z]*r[a-z]*f\s+\/(?:\s|$)/i,
	/\bstate\.db\b/i,
	/\bsqlite3\b/i,
];

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "upsert_task",
		label: "Upsert Superbrain Task",
		description:
			"Create or update a Superbrain goal or problem in an allowed repository. Goals require a metric_target.",
		promptSnippet: "Create or update a verifiable goal or problem task on the Superbrain board.",
		parameters: {
			type: "object",
			properties: {
				id: { type: "string", description: "Optional existing task ID (e.g. SG-1 or PRB-1)" },
				parent_id: { type: "string", description: "Optional parent goal ID" },
				kind: { type: "string", enum: ["goal", "problem"] },
				repo: { type: "string", description: "Relative repo path under ~/Projects (must be in ALLOWED_REPOS)" },
				title: { type: "string" },
				spec: { type: "string", description: "Hypothesis for a goal, or symptom/stacktrace for a problem" },
				metric_target: { type: "string", description: "Required when kind='goal' (e.g. 'pytest passes')" },
				verify_cmd: { type: "string", description: "Deterministic shell verification command" },
				priority: { type: "integer", description: "0 = urgent P0, 50 = normal, 90 = low" },
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

	pi.on("tool_call", async (event: ToolCallEvent): Promise<ToolCallEventResult | undefined> => {
		const worktreeDir = process.env.SUPERBRAIN_WORKTREE_DIR;
		const skillsDir = process.env.SUPERBRAIN_SKILLS_DIR;
		const taskKind = process.env.SUPERBRAIN_TASK_KIND;

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
			const targetPath = resolve(worktreeDir, rawPath);
			if (!isWithinDir(targetPath, worktreeDir)) {
				return {
					block: true,
					reason: `Write blocked: path ${rawPath} resolves outside the isolated worktree (${worktreeDir}).`,
				};
			}
			if (taskKind === "problem" && existsSync(targetPath)) {
				const rel = _nearest_existing_resolved(targetPath).slice(_nearest_existing_resolved(worktreeDir).length + 1);
				if (/^tests?(?:[\\/]|$)/i.test(rel)) {
					return {
						block: true,
						reason: "Modifying existing test files under tests/ while fixing a problem is blocked.",
					};
				}
			}
		}

		if (event.toolName === "bash") {
			const cmd = String((event.input as Record<string, unknown>).command ?? "");
			for (const pat of BLOCKED_BASH_PATTERNS) {
				if (pat.test(cmd)) {
					return {
						block: true,
						reason: `Bash command blocked by Superbrain guardrail (${pat}).`,
					};
				}
			}
			if (taskKind === "problem" && /\b(?:rm|git\s+rm)\b.*\btests?\//i.test(cmd)) {
				return {
					block: true,
					reason: "Deleting test files while fixing a problem is blocked.",
				};
			}
		}
		return undefined;
	});
}
