/**
 * topology — pi extension that makes the topology skill get used proactively.
 *
 * All logic lives in Python (`topology hook ...`, `topology notation ...`);
 * this file only wires pi's lifecycle events and editor to it.
 *
 * - session_start: fetch the topology summary once, load the notation key and
 *   its handles (`topology notation complete`), and register autocomplete.
 * - before_agent_start: on the first user prompt of a session, add the summary;
 *   on every user prompt, add the expanded forms of any notations (^top:pond),
 *   or else a hint when the prompt mentions a topology host, model or alias.
 * - autocomplete: the first character of the notation key (^) triggers it.
 *   Typing `^` offers the key (`^top:`); after `^top:` it offers the handles,
 *   filtered in memory, so typing never waits on Python.
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
import {
	type AutocompleteItem,
	type AutocompleteProvider,
	type AutocompleteSuggestions,
	fuzzyFilter,
} from "@earendil-works/pi-tui";

const skillsHome = process.env.SKILLS_HOME || path.join(os.homedir(), ".agents", "skills");
const srcDir = path.join(skillsHome, "topology-skill", "src");
const MAX_SUGGESTIONS = 20;

type Handle = { value: string; description: string };
type Completion = { key: string; skill: string; description: string; handles: Handle[] };

function runTopology(args: string[], input = ""): string {
	const result = spawnSync("python3", ["-m", "topology.cli", ...args], {
		input,
		encoding: "utf8",
		timeout: 5000,
		env: { ...process.env, PYTHONPATH: srcDir },
	});
	if (result.status !== 0 || typeof result.stdout !== "string") return "";
	return result.stdout.trim();
}

function runHook(event: "session-start" | "prompt", input = ""): string {
	return runTopology(["hook", event, "--format", "text"], input);
}

function loadCompletion(): Completion | undefined {
	try {
		const completion = JSON.parse(runTopology(["notation", "complete"])) as Completion;
		return completion.key ? completion : undefined;
	} catch {
		return undefined;
	}
}

/** The whitespace-delimited token ending at the cursor. */
function tokenBeforeCursor(lines: string[], cursorLine: number, cursorCol: number): string {
	const text = (lines[cursorLine] ?? "").slice(0, cursorCol);
	return text.match(/(?:^|\s)(\S*)$/)?.[1] ?? "";
}

function handleItems(completion: Completion, query: string): AutocompleteItem[] {
	const { key, handles } = completion;
	const matches = query
		? [
				...handles.filter((h) => h.value.toLowerCase().startsWith(query.toLowerCase())),
				...fuzzyFilter(handles, query, (h) => h.value),
			]
		: handles;
	const unique = [...new Map(matches.map((h) => [h.value, h])).values()];
	return unique.slice(0, MAX_SUGGESTIONS).map((h) => ({
		value: `${key}:${h.value}`,
		label: h.value,
		description: h.description,
	}));
}

function createNotationProvider(current: AutocompleteProvider, completion: Completion): AutocompleteProvider {
	const { key, skill, description } = completion;
	const trigger = key[0];

	const suggest = (token: string): AutocompleteSuggestions | null => {
		if (!token.startsWith(trigger)) return null;
		if (token.startsWith(`${key}:`)) {
			const items = handleItems(completion, token.slice(key.length + 1));
			return items.length ? { items, prefix: token } : null;
		}
		if (key.startsWith(token)) {
			return { items: [{ value: `${key}:`, label: key, description: `${skill}: ${description}` }], prefix: token };
		}
		return null;
	};

	return {
		triggerCharacters: [...(current.triggerCharacters ?? []), trigger],

		async getSuggestions(lines, cursorLine, cursorCol, options) {
			const ours = suggest(tokenBeforeCursor(lines, cursorLine, cursorCol));
			return ours ?? current.getSuggestions(lines, cursorLine, cursorCol, options);
		},

		applyCompletion(lines, cursorLine, cursorCol, item, prefix) {
			if (!prefix.startsWith(trigger) || !item.value.startsWith(key)) {
				return current.applyCompletion(lines, cursorLine, cursorCol, item, prefix);
			}
			// `^top:` keeps the cursor on the token so handles are offered next;
			// a full notation gets a trailing space.
			const line = lines[cursorLine] ?? "";
			const before = line.slice(0, cursorCol - prefix.length);
			const after = line.slice(cursorCol);
			const insert = item.value.endsWith(":") ? item.value : `${item.value} `;
			const next = [...lines];
			next[cursorLine] = before + insert + after;
			return { lines: next, cursorLine, cursorCol: before.length + insert.length };
		},

		shouldTriggerFileCompletion(lines, cursorLine, cursorCol) {
			return current.shouldTriggerFileCompletion?.(lines, cursorLine, cursorCol) ?? true;
		},
	};
}

export default function topologyExtension(pi: ExtensionAPI) {
	let summary = "";
	let summarySent = false;

	pi.on("session_start", async (_event, ctx) => {
		summary = runHook("session-start");
		summarySent = false;

		const completion = loadCompletion();
		if (completion) {
			ctx.ui?.addAutocompleteProvider?.((current) => createNotationProvider(current, completion));
		}
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
