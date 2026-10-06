import { NextRequest } from "next/server";
import crypto from "crypto";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { searchMessages } from "@/lib/cloud";
import { bedrockChatStream } from "@/lib/bedrock";
import { getActiveWorkspace } from "@/lib/workspace";
import { getAgentBySlug } from "@/lib/agents";
import { DEFAULT_ASSISTANT_PROMPT, DEFAULT_ASSISTANT_SLUG } from "@/lib/agents";
import { SESSION_TOOLS, executeTool } from "@/lib/session-tools";
import { loadConnectorsWithTools, buildMcpAnthropicTools, buildMcpOpenAiTools, callMcpTool, parseQualifiedToolName } from "@/lib/mcp-runtime";
import { resolveBestChatProvider } from "@/lib/router";

export const runtime = "nodejs";
export const maxDuration = 300;
export const dynamic = "force-dynamic";

const SYSTEM_PROMPT = DEFAULT_ASSISTANT_PROMPT;
const TOOL_LOOP_MAX = 4;

function generateTitle(text: string) {
  const t = text.trim().replace(/\s+/g, " ");
  return t.length > 80 ? t.slice(0, 77) + "…" : t;
}

const encoder = new TextEncoder();
function sse(event: Record<string, unknown>): Uint8Array {
  return encoder.encode(`data: ${JSON.stringify(event)}\n\n`);
}

export async function POST(req: NextRequest) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const body = await req.json().catch(() => ({}));
  const messages: Array<{ role: string; content: string }> = Array.isArray(body.messages) ? body.messages : [];
  const chatSessionId: string | null = typeof body.chat_session_id === "string" ? body.chat_session_id : null;
  const requestedAgentId: string | null = typeof body.agent_id === "string" ? body.agent_id : null;
  const requestedAgentIds: string[] = Array.isArray(body.agent_ids)
    ? body.agent_ids.filter((v: unknown): v is string => typeof v === "string")
    : (requestedAgentId ? [requestedAgentId] : []);
  const enabledToolNames: Set<string> | null = Array.isArray(body.enabled_tools)
    ? new Set(body.enabled_tools.filter((t: unknown): t is string => typeof t === "string"))
    : null;
  const last = [...messages].reverse().find((m) => m.role === "user");
  if (!last) return Response.json({ error: "missing_user_message" }, { status: 400 });

  const pool = getPool();
  const { config: chatProvider, selectedModel, switchedFrom } = await resolveBestChatProvider(userId);
  const { modelId: model, apiKey, baseUrl, format, headers, provider } = chatProvider;
  const hitLimit = provider === "ollama" ? 3 : 8;
  const recentLimit = provider === "ollama" ? 3 : 6;
  const hitContentCap = provider === "ollama" ? 120 : 240;

  // If the user's preferred model was unavailable, auto-switch and persist
  if (switchedFrom) {
    console.log(`[chat] model switch: ${switchedFrom} → ${selectedModel}`);
    pool.query(
      `INSERT INTO user_settings (user_id, chat_model) VALUES ($1, $2)
       ON CONFLICT (user_id) DO UPDATE SET chat_model = EXCLUDED.chat_model, updated_at = now()`,
      [userId, selectedModel],
    ).catch((e) => console.error(`[chat] failed to persist model switch:`, e));
  }

  const ws = await getActiveWorkspace(userId);
  const wsFilter = ws ? `WHERE workspace_id = $1` : `WHERE user_id = $1`;
  const wsParam = ws ? ws.id : userId;

  // Resolve all picked agents (caller may select multiple). Primary = the
  // seeded Spooling Assistant if it's in the set, else the first picked, else
  // the seeded default as a final fallback.
  type AgentRow = NonNullable<Awaited<ReturnType<typeof getAgentBySlug>>>;
  const pickedAgents: AgentRow[] = [];
  let primaryAgent: AgentRow | null = null;
  if (ws) {
    const { getAgent } = await import("@/lib/agents");
    if (requestedAgentIds.length > 0) {
      for (const id of requestedAgentIds) {
        const a = await getAgent(ws.id, id);
        if (a) pickedAgents.push(a);
      }
    }
    if (pickedAgents.length === 0) {
      const def = await getAgentBySlug(ws.id, DEFAULT_ASSISTANT_SLUG);
      if (def) pickedAgents.push(def);
    }
    primaryAgent =
      pickedAgents.find((a) => a.slug === DEFAULT_ASSISTANT_SLUG) ??
      pickedAgents[0] ??
      null;
  }
  const agentName = primaryAgent?.name ?? "Spooling Assistant";
  const baseSystemPrompt = (primaryAgent?.system_prompt && primaryAgent.system_prompt.trim()) || SYSTEM_PROMPT;
  const builtInActive = enabledToolNames
    ? SESSION_TOOLS.filter((t) => enabledToolNames.has(t.name))
    : SESSION_TOOLS;

  // Strict per-agent tool surface: union the connector_ids of every picked
  // agent. An agent with an empty connector_ids list contributes zero MCP
  // tools — to add Linear/GitHub/etc, attach them on /agents.
  const restrictIds = Array.from(new Set(pickedAgents.flatMap((a) => a.connector_ids ?? [])));
  const mcpConnectors = restrictIds.length > 0
    ? await loadConnectorsWithTools(userId, restrictIds)
    : [];
  const mcpTools = buildMcpAnthropicTools(mcpConnectors);
  const connectorBySlug = new Map(mcpConnectors.map((c) => [c.slug, c]));
  const activeTools = [...builtInActive, ...mcpTools];

  // We kick these off before streaming opens so the first chunk the client sees
  // is the workspace + search step, already filled in.
  const hitsP = searchMessages(userId, last.content, hitLimit);
  const recentP = pool.query(
    `SELECT id, provider_id, project, title, started_at, message_count, estimated_cost_usd
     FROM cloud_sessions ${wsFilter} ORDER BY started_at DESC NULLS LAST LIMIT $2`,
    [wsParam, recentLimit],
  );
  const statsP = pool.query(
    `SELECT COUNT(*)::int AS sessions, COALESCE(SUM(message_count), 0)::int AS messages,
            COALESCE(SUM(estimated_cost_usd), 0)::float AS cost
     FROM cloud_sessions ${wsFilter}`,
    [wsParam],
  );
  const [hits, { rows: recent }, { rows: stats }] = await Promise.all([hitsP, recentP, statsP]);

  const contextBlocks: string[] = [];
  contextBlocks.push(`## Workspace\nSessions: ${stats[0].sessions}. Messages: ${stats[0].messages}. Total cost: $${Number(stats[0].cost).toFixed(2)}.`);
  if (hits.length) {
    contextBlocks.push("## Relevant excerpts");
    for (const h of hits) {
      contextBlocks.push(`- [${h.session_id}] ${h.role}: ${h.content.slice(0, hitContentCap)}`);
    }
  }
  if (recent.length) {
    contextBlocks.push("## Recent sessions");
    for (const r of recent) {
      const title = (r.title ?? "").slice(0, 60);
      contextBlocks.push(`- [${r.id}] ${r.provider_id} ${r.project ?? ""} ${r.started_at ? new Date(r.started_at).toISOString().slice(0, 10) : ""}: ${title}`);
    }
  }
  const system = `${baseSystemPrompt}\n\n${contextBlocks.join("\n\n")}`;

  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const reqId = Math.random().toString(36).slice(2, 8);
      let assembled = "";
      let using: "anthropic" | "gemma" | "openai" | "groq" | "google" | "bedrock" | null = null;

      const emit = (event: Record<string, unknown>) => {
        try { controller.enqueue(sse(event)); } catch { /* controller closed */ }
      };
      const emitDelta = (text: string) => {
        if (!text) return;
        assembled += text;
        emit({ type: "delta", text });
      };

      // Upfront meta event — lets the client know sources/workspace before generation starts.
      emit({ type: "meta", sources: hits, reqId });

      // Narrate the agent's chain-of-thought so the user sees what happens
      // before the model starts streaming.
      emit({ type: "step", step: {
        kind: "workspace", label: "Read workspace",
        detail: `${stats[0].sessions} sessions, ${stats[0].messages} messages, $${Number(stats[0].cost).toFixed(2)}`,
        done: true,
      }});
      emit({ type: "step", step: {
        kind: "search", label: "Searched messages",
        detail: hits.length > 0 ? `${hits.length} relevant excerpt${hits.length === 1 ? "" : "s"}` : "no excerpts matched",
        done: true,
      }});

      if (switchedFrom) {
        emit({ type: "step", step: {
          kind: "model", label: `Switched model: ${switchedFrom} → ${selectedModel}`,
          detail: `${switchedFrom} unavailable, moved to ${selectedModel}`,
          done: true,
        }});
      }

      try {
        if (format === "anthropic") {
          using = "anthropic";
          emit({ type: "step", step: { kind: "model", label: `${agentName} via ${model}`, detail: `${provider} + session tools`, done: false } });
          console.log(`[chat ${reqId}] → ${provider} model=${model} (tool loop)`);

          // Tool-use loop: model can call session tools (search_sessions,
          // get_session, list_recent_sessions, get_workspace_stats) up to
          // TOOL_LOOP_MAX times before we force a final answer.
          type AnyBlock = { type: string; text?: string; id?: string; name?: string; input?: Record<string, unknown>; tool_use_id?: string; content?: unknown };
          type AnyMsg = { role: "user" | "assistant"; content: string | AnyBlock[] };
          const convo: AnyMsg[] = messages.map((m) => ({
            role: m.role === "assistant" ? "assistant" : "user",
            content: m.content,
          }));

          let finalText = "";
          let toolErr: string | null = null;
          for (let turn = 0; turn < TOOL_LOOP_MAX; turn++) {
            const res = await fetch(`${baseUrl}/v1/messages`, {
              method: "POST",
              headers,
              body: JSON.stringify({
                model,
                max_tokens: 2048,
                system,
                ...(activeTools.length > 0 ? { tools: activeTools } : {}),
                messages: convo,
              }),
            });
            if (!res.ok) {
              toolErr = (await res.text()).slice(0, 400);
              console.error(`[chat ${reqId}] anthropic non-OK ${res.status}: ${toolErr}`);
              emit({ type: "error", error: "anthropic_upstream", detail: `Request failed (${res.status}). Check your API key in Connections.` });
              break;
            }
            const payload = (await res.json()) as { content: AnyBlock[]; stop_reason?: string };
            const blocks = Array.isArray(payload.content) ? payload.content : [];
            const toolUses = blocks.filter((b) => b.type === "tool_use");
            const turnText = blocks.filter((b) => b.type === "text").map((b) => b.text ?? "").join("");

            // Persist the assistant's exact response (text + tool_use blocks) so
            // the next iteration's history is well-formed for Anthropic.
            convo.push({ role: "assistant", content: blocks });

            if (toolUses.length === 0 || payload.stop_reason !== "tool_use") {
              finalText = turnText;
              break;
            }

            // Emit a step per tool call so the user sees what the agent did.
            const toolResults: AnyBlock[] = [];
            for (const tu of toolUses) {
              const tname = tu.name ?? "unknown";
              const tinput = (tu.input ?? {}) as Record<string, unknown>;
              const mcpRef = parseQualifiedToolName(tname);
              const labelName = mcpRef ? `${mcpRef.slug}.${mcpRef.toolName}` : tname;
              emit({ type: "step", step: { kind: "tool_call", label: `Tool: ${labelName}`, detail: shortInput(tinput), tool_input: JSON.stringify(tinput), done: false } });
              let result: string;
              if (mcpRef) {
                const c = connectorBySlug.get(mcpRef.slug);
                if (!c) {
                  result = JSON.stringify({ ok: false, error: "connector_not_found", slug: mcpRef.slug });
                } else {
                  result = await callMcpTool(c, mcpRef.toolName, tinput);
                }
              } else {
                result = await executeTool(tname, tinput, { userId });
              }
              emit({ type: "step", step: { kind: "tool_call", label: `Tool: ${labelName}`, detail: result.slice(0, 200), tool_result: result.slice(0, 2000), done: true } });
              toolResults.push({ type: "tool_result", tool_use_id: tu.id, content: result });
            }
            convo.push({ role: "user", content: toolResults });
          }

          if (!toolErr) {
            if (finalText) emitDelta(finalText);
            else emit({ type: "error", error: "tool_loop_exhausted", detail: `No final answer after ${TOOL_LOOP_MAX} tool turns.` });
          }
        } else if (format === "openai") {
          using = provider === "ollama" ? "gemma" : provider as "openai" | "groq";
          emit({ type: "step", step: { kind: "model", label: `Calling ${model}`, detail: `${provider}`, done: false } });
          console.log(`[chat ${reqId}] → ${provider} host=${baseUrl} model=${model} msgs=${messages.length} sys_len=${system.length}`);

          // Build OpenAI-format tools for the tool loop
          const openAiMcpTools = buildMcpOpenAiTools(mcpConnectors);
          const openAiBuiltInTools = builtInActive.map((t) => ({
            type: "function" as const,
            function: { name: t.name, description: t.description, parameters: t.input_schema as Record<string, unknown> },
          }));
          const allOpenAiTools = [...openAiBuiltInTools, ...openAiMcpTools];
          const hasTools = allOpenAiTools.length > 0;

          // Gemma/Ollama models need very explicit tool-use instructions —
          // they're less fine-tuned for implicit tool calling than Claude.
          const enforceToolCalls = hasTools && provider === "ollama";
          const openaiSys = enforceToolCalls
            ? `${system}\n\nYou MUST call tools when you need live data. Do NOT describe what tool you would call — actually call it using the available function tools. Do NOT write pseudo-code. Do NOT write tool_code blocks. Just call the tool and use the result to answer.`
            : system;

          // Conversation in OpenAI chat completions format
          const openaiChatUrl = provider === "ollama" ? `${baseUrl}/api/chat` : `${baseUrl}/chat/completions`;
          const openaiConvo: Record<string, unknown>[] = [
            { role: "system", content: openaiSys },
            ...messages.map((m) => ({ role: m.role === "assistant" ? "assistant" : "user", content: m.content })),
          ];

          // Probe Ollama availability before the tool loop.
          let streamOk = false;
          let finalText = "";
          let toolErr: string | null = null;

          // Fast-fail health check for Ollama before attempting the tool loop.
          if (provider === "ollama") {
            const probeStart = Date.now();
            try {
              const probe = await fetch(`${baseUrl}/api/tags`, { signal: AbortSignal.timeout(4_000) });
              if (!probe.ok) throw new Error(`probe status=${probe.status}`);
              console.log(`[chat ${reqId}] probe /api/tags ok in ${Date.now() - probeStart}ms`);
            } catch (e) {
              const err = e as Error;
              console.error(`[chat ${reqId}] probe FAILED ${err.name}: ${err.message}`);
              toolErr = `Ollama unreachable (${err.message})`;
            }
          }

          const t0 = Date.now();

          // Helper for no-tools streaming fallback (OpenAI-compatible SSE)
          const trySimpleStream = async (): Promise<boolean> => {
            const body: Record<string, unknown> = {
              model,
              stream: true,
              messages: openaiConvo,
            };
            if (provider === "ollama") {
              body.options = { num_predict: 256, num_ctx: 2048 };
            } else {
              body.max_tokens = 1024;
              body.temperature = 0;
            }
            console.log(`[chat ${reqId}] opening POST to ${openaiChatUrl} (stream=true, no tools)`);
            const r = await fetch(openaiChatUrl, {
              method: "POST",
              headers,
              body: JSON.stringify(body),
              signal: AbortSignal.timeout(180_000),
            });
            if (r.ok && r.body) {
              await pipeOllamaNDJSON(r.body, (text) => emitDelta(text));
              console.log(`[chat ${reqId}] simple stream done text_len=${assembled.length}`);
              return true;
            }
            const d = (await r.text()).slice(0, 300);
            console.error(`[chat ${reqId}] simple stream non-OK: ${d}`);
            return false;
          };

          try {
            // Skip tool loop if health check already failed
            if (!toolErr && hasTools) {
              // Tool-use loop (non-streaming per turn). OpenAI-compatible format.
              let loopFailed = false;
              for (let turn = 0; turn < TOOL_LOOP_MAX; turn++) {
                const body: Record<string, unknown> = {
                  model,
                  stream: false,
                  messages: openaiConvo,
                  tools: allOpenAiTools,
                };
                if (provider === "ollama") {
                  body.options = { num_predict: 4096, num_ctx: 8192, temperature: 0 };
                } else {
                  body.temperature = 0;
                  body.max_tokens = 4096;
                  body.tool_choice = "auto";
                }
                const res = await fetch(openaiChatUrl, {
                  method: "POST",
                  headers,
                  body: JSON.stringify(body),
                  signal: AbortSignal.timeout(turn === 0 ? 180_000 : 60_000),
                });

                if (!res.ok) {
                  const b = await res.text();
                  toolErr = `${res.status}: ${b.slice(0, 300)}`;
                  console.error(`[chat ${reqId}] ${openaiChatUrl} non-OK ${toolErr}`);
                  loopFailed = true;
                  break;
                }

                const payload = (await res.json()) as {
                  message?: {
                    role?: string;
                    content?: string | null;
                    tool_calls?: Array<{
                      function: { name: string; arguments: Record<string, unknown> };
                    }>;
                  };
                };
                const msg = payload.message;
                if (!msg) { toolErr = "empty_message"; loopFailed = true; break; }

                if (!turn) {
                  streamOk = true;
                  console.log(`[chat ${reqId}] first turn at ${Date.now() - t0}ms`);
                }

                const turnText = msg.content ?? "";
                const toolCalls = msg.tool_calls ?? [];

                // Ollama /api/chat tool_calls lack an id — we generate one.
                const tcWithId = toolCalls.map((tc, i) => ({
                  id: `call_${turn}_${i}`,
                  type: "function" as const,
                  function: {
                    name: tc.function.name,
                    arguments: JSON.stringify(tc.function.arguments ?? {}),
                  },
                }));

                const assistantMsg: Record<string, unknown> = { role: "assistant", content: turnText || null };
                if (tcWithId.length > 0) assistantMsg.tool_calls = tcWithId;
                openaiConvo.push(assistantMsg);

                if (tcWithId.length === 0) {
                  // The model may have described a tool call without actually
                  // issuing one.  Re-prompt once so it uses the native function
                  // mechanism instead of writing pseudo-code.
                  if (
                    turn < TOOL_LOOP_MAX - 1 &&
                    enforceToolCalls &&
                    /(?:call(?:ed|ing)?\s+(?:a\s+)?(?:function|tool)|```tool_code|\w+\.\w+\s*\()/i.test(turnText)
                  ) {
                    openaiConvo.push({
                      role: "user",
                      content: "You described a tool but did not actually call it. Use the function calling mechanism (the available tools) to call it now. Do NOT describe what you would do — actually call the tool. Then use the result to answer.",
                    });
                    continue;
                  }
                  finalText = turnText;
                  break;
                }

                for (const tc of tcWithId) {
                  const tname = tc.function.name;
                  let args: Record<string, unknown> = {};
                  try { args = JSON.parse(tc.function.arguments) as Record<string, unknown>; } catch { /* ignore parse errors */ }

                  const mcpRef = parseQualifiedToolName(tname);
                  const labelName = mcpRef ? `${mcpRef.slug}.${mcpRef.toolName}` : tname;
                  emit({ type: "step", step: { kind: "tool_call", label: `Tool: ${labelName}`, detail: shortInput(args), tool_input: JSON.stringify(args), done: false } });

                  let result: string;
                  if (mcpRef) {
                    const c = connectorBySlug.get(mcpRef.slug);
                    if (!c) {
                      result = JSON.stringify({ ok: false, error: "connector_not_found", slug: mcpRef.slug });
                    } else {
                      result = await callMcpTool(c, mcpRef.toolName, args);
                    }
                  } else {
                    result = await executeTool(tname, args, { userId });
                  }
                  emit({ type: "step", step: { kind: "tool_call", label: `Tool: ${labelName}`, detail: result.slice(0, 200), tool_result: result.slice(0, 2000), done: true } });
                  openaiConvo.push({ role: "tool", content: result });
                }
              }

              if (!loopFailed) {
                if (finalText) {
                  streamOk = true;
                  emitDelta(finalText);
                } else {
                  // Tool loop exhausted without error — fall back to simple stream.
                  console.log(`[chat ${reqId}] tool loop exhausted, falling back to simple stream`);
                  streamOk = await trySimpleStream();
                }
              }
            } else if (!toolErr) {
              // No tools configured — simple streaming path.
              streamOk = await trySimpleStream();
            }
          } catch (e) {
            const err = e as Error;
            toolErr = `${err.name}: ${err.message}`;
            console.error(`[chat ${reqId}] ${provider} threw after ${Date.now() - t0}ms name=${err.name} msg=${err.message}`);
          }

          // When the probe already declared Ollama unreachable, skip the slow
          // retry and go directly to Bedrock fallback.
          if (!streamOk && !finalText) {
            if (toolErr && provider === "ollama") {
              console.log(`[chat ${reqId}] skipping retry — ollama unreachable (${toolErr})`);
            } else {
              streamOk = await trySimpleStream().catch(() => false);
            }
          }

          if (!streamOk && !finalText) {
            console.log(`[chat ${reqId}] ${provider} failed, attempting bedrock fallback`);
            emit({ type: "step", step: {
              kind: "model", label: `${provider} unavailable, using AWS Bedrock`,
              detail: "Falling back to AWS Bedrock",
              done: false,
            }});
            try {
              using = "bedrock";
              for await (const chunk of bedrockChatStream(system, messages)) {
                emitDelta(chunk);
              }
              console.log(`[chat ${reqId}] bedrock done text_len=${assembled.length}`);
            } catch (e) {
              const err = e as Error;
              console.error(`[chat ${reqId}] bedrock failed ${err.name}: ${err.message}`);
              console.error(`[chat ${reqId}] bedrock fallback failed: ${err.message}`);
              emit({
                type: "error",
                error: "all_backends_failed",
                detail: "No AI provider was available. Try adding an API key in Connections.",
              });
            }
          }
        } else if (format === "google") {
          using = "google";
          const apiKey = headers["x-goog-api-key"];
          emit({ type: "step", step: { kind: "model", label: `Calling ${model}`, detail: "Google Gemini", done: false } });
          console.log(`[chat ${reqId}] → gemini model=${model} msgs=${messages.length}`);

          // Convert OpenAI-style messages to Gemini contents format
          const geminiSys = system;
          const geminiContents: Array<{ role: string; parts: Array<{ text: string } | { functionCall?: Record<string, unknown>; functionResponse?: Record<string, unknown> }> }> = [];

          for (const m of messages) {
            const role = m.role === "assistant" ? "model" : "user";
            geminiContents.push({ role, parts: [{ text: m.content }] });
          }

          // Convert tools to Gemini functionDeclarations
          const openAiMcpTools = buildMcpOpenAiTools(mcpConnectors);
          const openAiBuiltInTools = builtInActive.map((t) => ({
            type: "function" as const,
            function: { name: t.name, description: t.description, parameters: t.input_schema as Record<string, unknown> },
          }));
          const allOpenAiTools = [...openAiBuiltInTools, ...openAiMcpTools];
          const hasTools = allOpenAiTools.length > 0;
          const geminiTools = hasTools ? [{
            functionDeclarations: allOpenAiTools.map((t) => ({
              name: t.function.name,
              description: t.function.description,
              parameters: t.function.parameters,
            })),
          }] : undefined;

          // Gemini non-streaming endpoint for tool loop
          const geminiUrl = () => `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`;

          let geminiOk = false;
          let finalText = "";
          let toolErr: string | null = null;

          try {
            if (hasTools) {
              let loopFailed = false;
              for (let turn = 0; turn < TOOL_LOOP_MAX; turn++) {
                const body: Record<string, unknown> = {
                  contents: geminiContents,
                  tools: geminiTools,
                };
                if (geminiSys) body.system_instruction = { parts: [{ text: geminiSys }] };

                const res = await fetch(geminiUrl(), {
                  method: "POST",
                  headers,
                  body: JSON.stringify(body),
                  signal: AbortSignal.timeout(turn === 0 ? 180_000 : 60_000),
                });

                if (!res.ok) {
                  const b = await res.text();
                  toolErr = `${res.status}: ${b.slice(0, 300)}`;
                  console.error(`[chat ${reqId}] gemini non-OK ${toolErr}`);
                  loopFailed = true;
                  break;
                }

                const payload = (await res.json()) as {
                  candidates?: Array<{
                    content?: { parts?: Array<Record<string, unknown>> };
                    finishReason?: string;
                  }>;
                };
                const candidate = payload.candidates?.[0];
                if (!candidate) { toolErr = "empty_candidate"; loopFailed = true; break; }

                const parts = candidate.content?.parts ?? [];
                const turnText = parts
                  .filter((p: Record<string, unknown>): p is { text: string } => "text" in p && typeof p.text === "string")
                  .map((p) => p.text)
                  .join("");
                const functionCalls = parts.filter((p: Record<string, unknown>) => "functionCall" in p);

                if (!turn) {
                  geminiOk = true;
                }

                // Build the model's response for history
                const responseParts: Array<Record<string, unknown>> = [];
                if (turnText) responseParts.push({ text: turnText });
                for (const fc of functionCalls) {
                  responseParts.push({ functionCall: fc.functionCall });
                }
                geminiContents.push({ role: "model", parts: responseParts as any });

                if (functionCalls.length === 0) {
                  finalText = turnText;
                  break;
                }

                for (const fc of functionCalls) {
                  const fcall = fc.functionCall as Record<string, unknown> | undefined;
                  if (!fcall) continue;
                  const tname = fcall.name as string;
                  const tinput = (fcall.args ?? {}) as Record<string, unknown>;
                  const mcpRef = parseQualifiedToolName(tname);
                  const labelName = mcpRef ? `${mcpRef.slug}.${mcpRef.toolName}` : tname;
                  emit({ type: "step", step: { kind: "tool_call", label: `Tool: ${labelName}`, detail: shortInput(tinput), tool_input: JSON.stringify(tinput), done: false } });

                  let result: string;
                  if (mcpRef) {
                    const c = connectorBySlug.get(mcpRef.slug);
                    if (!c) {
                      result = JSON.stringify({ ok: false, error: "connector_not_found", slug: mcpRef.slug });
                    } else {
                      result = await callMcpTool(c, mcpRef.toolName, tinput);
                    }
                  } else {
                    result = await executeTool(tname, tinput, { userId });
                  }
                  emit({ type: "step", step: { kind: "tool_call", label: `Tool: ${labelName}`, detail: result.slice(0, 200), tool_result: result.slice(0, 2000), done: true } });

                  geminiContents.push({
                    role: "function",
                    parts: [{ functionResponse: { name: tname, response: { content: result } } } as any],
                  });
                }
              }

              if (!loopFailed && finalText) {
                geminiOk = true;
                emitDelta(finalText);
              }
            }

            if (!geminiOk && !finalText) {
              // Streaming path (no tools or fallback)
              const streamUrl = `https://generativelanguage.googleapis.com/v1beta/models/${model}:streamGenerateContent?alt=sse`;
              const body: Record<string, unknown> = { contents: geminiContents };
              if (geminiSys) body.system_instruction = { parts: [{ text: geminiSys }] };

              const r = await fetch(streamUrl, {
                method: "POST",
                headers,
                body: JSON.stringify(body),
                signal: AbortSignal.timeout(180_000),
              });

              if (r.ok && r.body) {
                geminiOk = true;
                await pipeGeminiSSE(r.body, (text) => emitDelta(text));
                console.log(`[chat ${reqId}] gemini streaming done text_len=${assembled.length}`);
              } else {
                const d = await r.text().catch(() => "");
                toolErr = `${r.status}: ${d.slice(0, 300)}`;
                console.error(`[chat ${reqId}] gemini stream non-OK ${toolErr}`);
              }
            }
          } catch (e) {
            const err = e as Error;
            toolErr = `${err.name}: ${err.message}`;
            console.error(`[chat ${reqId}] gemini threw: ${err.message}`);
          }

          if (!geminiOk && !finalText) {
            console.error(`[chat ${reqId}] gemini failed: ${toolErr}`);
            emit({ type: "error", error: "gemini_failed", detail: "Gemini did not produce a response. Check your API key in Connections." });
          }
        } else if (format === "bedrock") {
          using = "bedrock";
          emit({ type: "step", step: { kind: "model", label: `Calling ${model}`, detail: "AWS Bedrock", done: false } });
          console.log(`[chat ${reqId}] → bedrock model=${model} msgs=${messages.length}`);

          try {
            for await (const chunk of bedrockChatStream(system, messages, model)) {
              emitDelta(chunk);
            }
            console.log(`[chat ${reqId}] bedrock done text_len=${assembled.length}`);
          } catch (e) {
            const err = e as Error;
            console.error(`[chat ${reqId}] bedrock failed ${err.name}: ${err.message}`);
            emit({
              type: "error",
              error: "bedrock_failed",
              detail: "AWS Bedrock request failed. Check your AWS credentials.",
            });
          }
        }
      } catch (e) {
        const err = e as Error;
        console.error(`[chat ${reqId}] unexpected: ${err.message}`);
        console.error(`[chat ${reqId}] unexpected error: ${err.message}`);
        emit({ type: "error", error: "unexpected", detail: "An unexpected error occurred. Please try again." });
      }

      // Persist to chat history.
      let persistedId = chatSessionId;
      try {
        if (assembled) {
          if (!persistedId) {
            const { rows: created } = await pool.query(
              `INSERT INTO chat_sessions (id, user_id, title) VALUES ($1, $2, $3) RETURNING id`,
              [crypto.randomUUID(), userId, generateTitle(last.content)],
            );
            persistedId = created[0].id as string;
          } else {
            await pool.query(`UPDATE chat_sessions SET updated_at = now() WHERE id = $1 AND user_id = $2`, [persistedId, userId]);
          }
          await pool.query(
            `INSERT INTO chat_messages (chat_session_id, role, content) VALUES ($1, $2, $3)`,
            [persistedId, "user", last.content],
          );
          await pool.query(
            `INSERT INTO chat_messages (chat_session_id, role, content) VALUES ($1, $2, $3)`,
            [persistedId, "assistant", assembled],
          );
        }
      } catch (e) {
        console.error(`[chat ${reqId}] persist failed`, e);
      }

      if (assembled) {
        emit({ type: "step", step: { kind: "model", label: `Finished (${using ?? "unknown"})`, done: true } });
      }
      emit({ type: "done", chat_session_id: persistedId, using });
      controller.close();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}

async function pipeOllamaNDJSON(
  body: ReadableStream<Uint8Array>,
  emit: (text: string) => void,
) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl = buf.indexOf("\n");
    while (nl !== -1) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) {
        try {
          const obj = JSON.parse(line) as { message?: { content?: string }; done?: boolean };
          if (obj.message?.content) emit(obj.message.content);
        } catch { /* ignore partial/malformed */ }
      }
      nl = buf.indexOf("\n");
    }
  }
  if (buf.trim()) {
    try {
      const obj = JSON.parse(buf.trim()) as { message?: { content?: string } };
      if (obj.message?.content) emit(obj.message.content);
    } catch { /* ignore */ }
  }
}

function shortInput(input: Record<string, unknown>): string {
  const entries = Object.entries(input).slice(0, 3);
  if (entries.length === 0) return "";
  return entries.map(([k, v]) => `${k}=${truncForLog(v)}`).join(" ");
}

function truncForLog(v: unknown): string {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 40 ? s.slice(0, 37) + "…" : s;
}

async function pipeGeminiSSE(
  body: ReadableStream<Uint8Array>,
  emit: (text: string) => void,
) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep = buf.indexOf("\n\n");
    while (sep !== -1) {
      const eventBlock = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const dataLine = eventBlock.split("\n").find((l) => l.startsWith("data: "));
      if (dataLine) {
        try {
          const obj = JSON.parse(dataLine.slice(6)) as {
            candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }>;
          };
          if (obj.candidates?.[0]?.content?.parts) {
            for (const part of obj.candidates[0].content.parts) {
              if (part.text) emit(part.text);
            }
          }
        } catch { /* ignore */ }
      }
      sep = buf.indexOf("\n\n");
    }
  }
}

async function pipeAnthropicSSE(
  body: ReadableStream<Uint8Array>,
  emit: (text: string) => void,
) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep = buf.indexOf("\n\n");
    while (sep !== -1) {
      const eventBlock = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const dataLine = eventBlock.split("\n").find((l) => l.startsWith("data: "));
      if (dataLine) {
        try {
          const obj = JSON.parse(dataLine.slice(6)) as {
            type?: string;
            delta?: { type?: string; text?: string };
          };
          if (obj.type === "content_block_delta" && obj.delta?.type === "text_delta" && obj.delta.text) {
            emit(obj.delta.text);
          }
        } catch { /* ignore */ }
      }
      sep = buf.indexOf("\n\n");
    }
  }
}
