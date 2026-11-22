/**
 * Jarvis bridge: lets `jarvis connect pi` talk to this Pi window over a Unix socket.
 * Installed by Jarvis into ~/.pi/agent/extensions/jarvis.ts; Jarvis overwrites it on update.
 *
 * One JSON object per line. To Jarvis: hello, busy, settled, prompt_start, prompt_end.
 * From Jarvis: {"type":"prompt","text":"..."} and {"type":"abort"}.
 */

import { chmodSync, mkdirSync, rmSync } from "node:fs";
import { createServer, type Server, type Socket } from "node:net";
import { homedir } from "node:os";
import { join } from "node:path";

import type { ExtensionAPI, ExtensionContext, SessionMessageEntry } from "@earendil-works/pi-coding-agent";

// Same folder for an installed and a dev Jarvis, like its one-mic lock.
const DIR = join(homedir(), ".config", "jarvis", "pi");
const PATH = join(DIR, `${process.pid}.sock`); // by process: /new and /resume keep the socket Jarvis knows

function textOf(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.filter((c): c is { type: "text"; text: string } => c?.type === "text" && typeof c.text === "string")
		.map((c) => c.text)
		.join("\n");
}

export default function (pi: ExtensionAPI) {
	let server: Server | undefined;
	let ctx: ExtensionContext | undefined;
	let lastText = "";
	const clients = new Set<Socket>();

	const send = (record: object) => {
		const line = `${JSON.stringify(record)}\n`;
		for (const c of clients) c.write(line);
	};

	const hello = () => {
		const sm = ctx?.sessionManager;
		const firstUser = sm
			?.getBranch()
			.find((e): e is SessionMessageEntry => e.type === "message" && e.message.role === "user");
		return {
			type: "hello",
			session: sm?.getSessionId() ?? "",
			cwd: sm?.getCwd() ?? process.cwd(),
			name: sm?.getSessionName() ?? "",
			first: textOf(firstUser && "content" in firstUser.message ? firstUser.message.content : "").slice(0, 80),
			busy: ctx ? !ctx.isIdle() : false,
		};
	};

	const onLine = (line: string) => {
		let cmd: { type?: unknown; text?: unknown };
		try {
			cmd = JSON.parse(line);
		} catch {
			return;
		}
		if (cmd.type === "prompt" && typeof cmd.text === "string" && cmd.text.trim()) {
			// While a turn runs, Pi needs a delivery mode; a follow-up waits for the turn to finish.
			if (ctx?.isIdle()) pi.sendUserMessage(cmd.text);
			else pi.sendUserMessage(cmd.text, { deliverAs: "followUp" });
		} else if (cmd.type === "abort") {
			ctx?.abort();
		}
	};

	pi.on("session_start", async (_event, c) => {
		ctx = c;
		if (server) return;
		try {
			mkdirSync(DIR, { recursive: true, mode: 0o700 });
			chmodSync(DIR, 0o700); // whoever can connect can make Pi run commands
			rmSync(PATH, { force: true });
			server = createServer((sock) => {
				clients.add(sock);
				let buf = "";
				sock.setEncoding("utf8");
				sock.on("data", (chunk: string) => {
					buf += chunk;
					let nl: number;
					while ((nl = buf.indexOf("\n")) >= 0) {
						onLine(buf.slice(0, nl));
						buf = buf.slice(nl + 1);
					}
				});
				sock.on("close", () => clients.delete(sock));
				sock.on("error", () => clients.delete(sock));
				sock.write(`${JSON.stringify(hello())}\n`);
			});
			server.on("error", (err) => c.ui.notify(`Jarvis bridge stopped: ${err.message}`, "warning"));
			server.listen(PATH);
		} catch (err) {
			c.ui.notify(`Jarvis bridge could not start: ${(err as Error).message}`, "warning");
		}
	});

	pi.on("session_shutdown", async () => {
		for (const c of clients) c.destroy();
		clients.clear();
		server?.close();
		server = undefined;
		rmSync(PATH, { force: true });
	});

	pi.on("session_info_changed", async () => send(hello()));
	pi.on("agent_start", async () => send({ type: "busy" }));
	pi.on("agent_end", async (event) => {
		const last = [...event.messages].reverse().find((m) => m.role === "assistant");
		if (last && "content" in last) lastText = textOf(last.content);
	});
	pi.on("agent_settled", async (event) => {
		send({ type: "settled", aborted: Boolean(event.aborted), text: lastText });
		lastText = "";
	});
	pi.on("ui_prompt_start", async (event) => send({ type: "prompt_start", kind: event.kind, title: event.title ?? "" }));
	pi.on("ui_prompt_end", async () => send({ type: "prompt_end" }));
}
