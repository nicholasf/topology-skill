/**
 * topology — pi extension that makes the topology skill get used proactively.
 *
 * All logic lives in Python (`topology hook ...`); this file only wires pi's
 * lifecycle events to it.
 *
 * - session_start: fetch the topology summary once.
 * - before_agent_start: on the first user prompt of a session, add the summary;
 *   on every user prompt, add a hint when the prompt mentions a topology host,
 *   model or playbook alias.
 *
 * Context is returned as a `message` (added at the end of the conversation),
 * never by editing the system prompt, so the cached prompt prefix stays valid.
 *
 * Installed by: topology hooks install --target pi
 */

import { spawnSync } from "node:child_process";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const skillsHome = process.env.SKILLS_HOME || path.join(os.homedir(), ".agents", "skills");
const srcDir = path.join(skillsHome, "topology-skill", "src");

function runHook(event: "session-start" | "prompt", input = ""): string {
	const result = spawnSync("python3", ["-m", "topology.cli", "hook", event, "--format", "text"], {
		input,
		encoding: "utf8",
		timeout: 5000,
		env: { ...process.env, PYTHONPATH: srcDir },
	});
	if (result.status !== 0 || typeof result.stdout !== "string") return "";
	return result.stdout.trim();
}

export default function topologyExtension(pi: ExtensionAPI) {
	let summary = "";
	let summarySent = false;

	pi.on("session_start", async () => {
		summary = runHook("session-start");
		summarySent = false;
	});

	pi.on("before_agent_start", async (event) => {
		if (event.messageType !== "user") return;

		const parts: string[] = [];
		if (!summarySent && summary) {
			parts.push(summary);
			summarySent = true;
		}
		const hint = runHook("prompt", event.prompt);
		if (hint) parts.push(hint);
		if (parts.length === 0) return;

		return {
			message: {
				customType: "topology-context",
				content: parts.join("\n\n"),
				display: Boolean(hint),
			},
		};
	});
}
