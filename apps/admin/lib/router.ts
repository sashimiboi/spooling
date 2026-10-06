import { getPool } from "./db";
import { MODELS, JUDGE_MODELS, modelById, type ModelDef, type CostTier } from "./models";
import { bedrockChat } from "./bedrock";
import { decryptIfEncrypted } from "./crypto";

export interface UserModelConfig {
  model: string;
  evalModel: string | null;
  hasAnthropicKey: boolean;
  byokProviders: string[];
}

export interface ResolvedProvider {
  provider: string;
  model: ModelDef;
  apiKey: string | null;
  chat: (system: string, messages: Array<{ role: string; content: string }>) => Promise<string>;
}

export interface ChatProviderConfig {
  provider: string;
  modelId: string;          // resolved model ID for the upstream API
  apiKey: string | null;
  baseUrl: string;          // API base URL (empty for Bedrock)
  format: "anthropic" | "openai" | "bedrock" | "google";
  headers: Record<string, string>;
}

export async function resolveChatProvider(userId: string, modelId?: string): Promise<ChatProviderConfig> {
  const config = await getUserModelConfig(userId);
  const mid = modelId || config.model;
  const def = modelById(mid);

  // Anthropic BYOK
  if (def && def.provider === "anthropic") {
    const pool = getPool();
    const { rows } = await pool.query(`SELECT anthropic_api_key FROM user_settings WHERE user_id = $1`, [userId]);
    const raw = (rows[0] as { anthropic_api_key?: string } | undefined)?.anthropic_api_key;
    const key = decryptIfEncrypted(raw ?? null);
    if (key) {
      return {
        provider: "anthropic",
        modelId: mid,
        apiKey: key,
        baseUrl: "https://api.anthropic.com",
        format: "anthropic",
        headers: { "Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01" },
      };
    }
  }

  // Google Gemini BYOK (different schema — handled separately in chat route)
  if (def && def.provider === "google") {
    const key = await getByokKey(userId, "google");
    if (key) {
      return {
        provider: "google",
        modelId: mid === "gemini-1.5-pro" ? "gemini-1.5-pro" : mid,
        apiKey: key,
        baseUrl: "https://generativelanguage.googleapis.com/v1beta",
        format: "google",
        headers: { "Content-Type": "application/json", "x-goog-api-key": key },
      };
    }
  }

  // OpenAI / Groq BYOK (both use OpenAI-compatible chat completions API)
  if (def && (def.provider === "openai" || def.provider === "groq")) {
    const key = await getByokKey(userId, def.provider);
    if (key) {
      const baseUrl = def.provider === "groq" ? "https://api.groq.com/openai/v1" : "https://api.openai.com/v1";
      const openaiModelId = mid.replace(/^groq-/, "");
      return {
        provider: def.provider,
        modelId: openaiModelId,
        apiKey: key,
        baseUrl,
        format: "openai",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      };
    }
  }

  // Bedrock models (always available in our AWS account)
  if (def && def.provider === "bedrock") {
    return {
      provider: "bedrock",
      modelId: mid,
      apiKey: null,
      baseUrl: "",
      format: "bedrock",
      headers: {},
    };
  }

  // No provider resolved — fallback to default Bedrock Claude
  return {
    provider: "bedrock",
    modelId: "anthropic.claude-3-5-haiku-20241022-v1:0",
    apiKey: null,
    baseUrl: "",
    format: "bedrock",
    headers: {},
  };
}

// ── Judge responses ─────────────────────────────────────────────

export interface JudgeResult {
  score: number | null;
  passed: boolean | null;
  label: string;
  rationale: string;
}

// ── Fetch user config from DB ───────────────────────────────────

export async function getUserModelConfig(userId: string): Promise<UserModelConfig> {
  const pool = getPool();
  const [{ rows: settings }, { rows: modelKeys }] = await Promise.all([
    pool.query(`SELECT anthropic_api_key, chat_model, eval_model FROM user_settings WHERE user_id = $1`, [userId]),
    pool.query(`SELECT provider FROM user_model_keys WHERE user_id = $1 AND api_key IS NOT NULL AND api_key <> ''`, [userId]),
  ]);

  const byokProviders = modelKeys.map((r) => r.provider as string);
  const hasAnthropicKey = !!(settings[0] as { anthropic_api_key?: string } | undefined)?.anthropic_api_key;
  const selectedModel = (settings[0] as { chat_model?: string } | undefined)?.chat_model ?? "claude-haiku-4-5-20251001";
  const evalModel = (settings[0] as { eval_model?: string | null } | undefined)?.eval_model ?? null;

  return { model: selectedModel, evalModel, hasAnthropicKey, byokProviders };
}

// ── Model availability ──────────────────────────────────────────

export function getAvailableModels(config: UserModelConfig): ModelDef[] {
  return MODELS.filter((m) => {
    if (m.tier === "aws") return true; // Bedrock always available in our account
    if (m.provider === "anthropic") return config.hasAnthropicKey;
    return config.byokProviders.includes(m.provider);
  });
}

export function getAvailableModelIds(config: UserModelConfig): string[] {
  return getAvailableModels(config).map((m) => m.id);
}

// ── Resolve best chat provider (auto-fallback when model unavailable) ──

export async function resolveBestChatProvider(userId: string): Promise<{
  config: ChatProviderConfig;
  selectedModel: string;
  switchedFrom?: string;
}> {
  const userConfig = await getUserModelConfig(userId);
  const available = getAvailableModels(userConfig);
  const preferred = userConfig.model;

  // User's preferred model is available — use it
  if (available.find((m) => m.id === preferred)) {
    const config = await resolveChatProvider(userId, preferred);
    return { config, selectedModel: preferred };
  }

  // Pick the first available model (preference order: free → byok → aws)
  if (available.length > 0) {
    const best = available[0];
    const config = await resolveChatProvider(userId, best.id);
    return { config, selectedModel: best.id, switchedFrom: preferred };
  }

  // Nothing available — last resort Bedrock
  const bedrockId = "anthropic.claude-3-5-haiku-20241022-v1:0";
  const config = await resolveChatProvider(userId, bedrockId);
  return { config, selectedModel: bedrockId, switchedFrom: preferred };
}

// ── Resolve a judge provider for evals ───────────────────────────

async function tryBuildCandidate(
  mid: string,
  userId: string,
  config: UserModelConfig,
): Promise<{ provider: string; modelId: string; fn: (system: string, prompt: string) => Promise<string> } | null> {
  const md = modelById(mid);
  if (!md) return null;
  if (!getAvailableModels(config).find((m) => m.id === mid)) return null;

  if (md.provider === "anthropic") {
    const pool = getPool();
    const { rows } = await pool.query(`SELECT anthropic_api_key FROM user_settings WHERE user_id = $1`, [userId]);
    const raw = (rows[0] as { anthropic_api_key?: string } | undefined)?.anthropic_api_key;
    const key = decryptIfEncrypted(raw ?? null);
    if (key) return { provider: "anthropic", modelId: md.id, fn: anthropicJudge(key) };
  } else if (md.provider === "openai") {
    const key = await getByokKey(userId, "openai");
    if (key) return { provider: "openai", modelId: md.id, fn: openaiJudge(key, md.id) };
  } else if (md.provider === "groq") {
    const key = await getByokKey(userId, "groq");
    if (key) return { provider: "groq", modelId: md.id, fn: openaiJudge(key, md.id.replace(/^groq-/, ""), "https://api.groq.com/openai/v1") };
  } else if (md.provider === "bedrock") {
    return { provider: "bedrock", modelId: md.id, fn: await bedrockJudge(md.id) };
  }
  return null;
}

export async function resolveJudge(userId: string): Promise<{
  provider: string;
  modelId: string;
  judge: (system: string, prompt: string) => Promise<string>;
}> {
  const config = await getUserModelConfig(userId);

  // Collect ALL available judge functions. The user's preferred eval model
  // comes first (if set and available), then the JUDGE_MODELS preference
  // order (free → byok → aws). Bedrock is always the last resort.
  const seen = new Set<string>();
  const candidates: Array<{
    provider: string;
    modelId: string;
    fn: (system: string, prompt: string) => Promise<string>;
  }> = [];

  const add = async (mid: string) => {
    if (seen.has(mid)) return;
    seen.add(mid);
    const c = await tryBuildCandidate(mid, userId, config);
    if (c) candidates.push(c);
  };

  // User's preferred eval model (if any)
  if (config.evalModel) await add(config.evalModel);

  // Default preference order
  for (const mid of JUDGE_MODELS) {
    if (!config.evalModel || mid !== config.evalModel) await add(mid);
  }

  // Always ensure Bedrock is available as last resort
  if (!candidates.find((c) => c.provider === "bedrock")) {
    candidates.push({ provider: "bedrock", modelId: "anthropic.claude-3-5-haiku-20241022-v1:0", fn: await bedrockJudge("anthropic.claude-3-5-haiku-20241022-v1:0") });
  }

  // Return a fallback judge that tries each candidate in order,
  // catching failures (low credits, rate limits, etc.) and moving to the next.
  const primary = candidates[0];
  const judge = async (system: string, prompt: string): Promise<string> => {
    const errors: string[] = [];
    for (const c of candidates) {
      try {
        return await c.fn(system, prompt);
      } catch (e) {
        errors.push(`${c.provider}(${c.modelId}): ${(e as Error).message?.slice(0, 100)}`);
      }
    }
    throw new Error(`All judges failed: ${errors.join("; ")}`);
  };

  return { provider: primary.provider, modelId: primary.modelId, judge };
}

async function getByokKey(userId: string, provider: string): Promise<string | null> {
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT api_key FROM user_model_keys WHERE user_id = $1 AND provider = $2 AND api_key IS NOT NULL AND api_key <> ''`,
    [userId, provider],
  );
  const raw = (rows[0] as { api_key?: string } | undefined)?.api_key ?? null;
  return raw ? decryptIfEncrypted(raw) : null;
}

// ── Judge implementations ──────────────────────────────────────

function anthropicJudge(apiKey: string) {
  return async (system: string, prompt: string): Promise<string> => {
    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: "claude-3-5-haiku-latest",
        max_tokens: 512,
        system,
        messages: [{ role: "user", content: prompt }],
      }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`Anthropic judge ${res.status}: ${text.slice(0, 200)}`);
    }
    const payload = (await res.json()) as { content: Array<{ type: string; text?: string }> };
    return payload.content?.find((b) => b.type === "text")?.text ?? "";
  };
}

function openaiJudge(apiKey: string, model: string, baseUrl = "https://api.openai.com/v1") {
  return async (system: string, prompt: string): Promise<string> => {
    const res = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        max_tokens: 512,
        messages: [
          { role: "system", content: system },
          { role: "user", content: prompt },
        ],
      }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`OpenAI judge ${res.status}: ${text.slice(0, 200)}`);
    }
    const payload = (await res.json()) as { choices: Array<{ message: { content: string } }> };
    return payload.choices?.[0]?.message?.content ?? "";
  };
}

async function bedrockJudge(modelId: string): Promise<(system: string, prompt: string) => Promise<string>> {
  return async (system: string, prompt: string): Promise<string> => {
    return bedrockChat(system, [{ role: "user", content: prompt }], modelId);
  };
}

// ── Parse judge JSON response ──────────────────────────────────

export function parseJudgeResponse(text: string): JudgeResult {
  const defaultResult: JudgeResult = { score: null, passed: null, label: "parse-error", rationale: "Could not parse judge response." };

  const jsonMatch = text.match(/\{[\s\S]*"score"[\s\S]*\}/);
  if (!jsonMatch) return defaultResult;

  try {
    const parsed = JSON.parse(jsonMatch[0]);
    return {
      score: typeof parsed.score === "number" ? Math.max(0, Math.min(1, parsed.score)) : null,
      passed: typeof parsed.passed === "boolean" ? parsed.passed : parsed.score != null ? parsed.score >= 0.7 : null,
      label: typeof parsed.label === "string" ? parsed.label.slice(0, 60) : "judged",
      rationale: typeof parsed.rationale === "string" ? parsed.rationale.slice(0, 500) : "",
    };
  } catch {
    return defaultResult;
  }
}
