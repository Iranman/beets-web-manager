import { describe, expect, it } from 'vitest';
import { aiKeyVariable, aiSetupEnv, effectiveAiBaseUrl } from '../src/lib/aiSetup';

// #523: the probe URL, the saved key variable and the saved AI_BASE_URL agree.
describe('first-run AI settings', () => {
  const cases: Array<[string, string, string]> = [
    ['', 'OPENAI_API_KEY', 'https://api.openai.com/v1'],
    ['   ', 'OPENAI_API_KEY', 'https://api.openai.com/v1'],
    ['https://api.openai.com/v1', 'OPENAI_API_KEY', 'https://api.openai.com/v1'],
    ['https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1'],
    ['https://llm.example/v1', 'AI_API_KEY', 'https://llm.example/v1'],
    ['https://api.openai.com.evil.example/v1', 'AI_API_KEY', 'https://api.openai.com.evil.example/v1'],
    ['not a url', 'AI_API_KEY', 'not a url'],
  ];

  it.each(cases)('base URL %j saves %s with AI_BASE_URL %s', (field, variable, url) => {
    expect(effectiveAiBaseUrl(field)).toBe(url);
    expect(aiKeyVariable(field)).toBe(variable);
    expect(aiSetupEnv('k', field, 'm')).toEqual({ [variable]: 'k', AI_BASE_URL: url, AI_MODEL: 'm' });
  });

  it('omits AI_MODEL when blank', () => {
    expect(aiSetupEnv('k', '', '')).toEqual({ OPENAI_API_KEY: 'k', AI_BASE_URL: 'https://api.openai.com/v1' });
  });
});
