// First-run AI settings (#523). The probe, the saved key variable and the
// saved AI_BASE_URL must all use one URL, so a blank field means OpenAI
// everywhere instead of whatever the backend would infer from other keys.
export const DEFAULT_AI_BASE_URL = 'https://api.openai.com/v1';

export function effectiveAiBaseUrl(baseUrl: string): string {
  return baseUrl.trim() || DEFAULT_AI_BASE_URL;
}

// The backend only sends a key to the provider it belongs to, so save it
// under that provider's variable.
export function aiKeyVariable(baseUrl: string): string {
  let host = '';
  try {
    host = new URL(effectiveAiBaseUrl(baseUrl)).hostname.toLowerCase();
  } catch {
    // not a URL: a custom endpoint
  }
  const on = (domain: string) => host === domain || host.endsWith(`.${domain}`);
  if (on('api.openai.com')) return 'OPENAI_API_KEY';
  if (on('openrouter.ai')) return 'OPENROUTER_API_KEY';
  return 'AI_API_KEY';
}

export function aiSetupEnv(apiKey: string, baseUrl: string, model: string): Record<string, string> {
  const url = effectiveAiBaseUrl(baseUrl);
  const env: Record<string, string> = { [aiKeyVariable(url)]: apiKey, AI_BASE_URL: url };
  if (model) env.AI_MODEL = model;
  return env;
}
