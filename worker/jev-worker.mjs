/**
 * JSONL boundary between Python's persisted requests and Gateway evaluation.
 * Each input makes at most one provider attempt; Python owns retries and storage.
 * Preserve available billing metadata even when answer validation rejects it.
 */
import { experimental_evaluate as evaluate, gateway } from 'ai';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const labels = ['low', 'medium', 'high'];
const finite = value => typeof value === 'number' && Number.isFinite(value);
const numeric = value => {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
};

function contextRejected(error, code) {
  if (code === 413) return true;
  if (code !== 400) return false;
  // Gateway wraps Jev's context limit as a JSON message on a 400 error.
  // Inspect the fixed error type without persisting source-bearing messages.
  try {
    return JSON.parse(error.message)?.error_type === 'max_tokens_exceeded';
  } catch {
    return false;
  }
}

/** Validate a risk answer and retain bounded metadata, never headers or bodies. */
export function normalizeResult(result) {
  const answer = result.answers?.risk;
  const probabilities = answer?.probabilities;
  const decimals = result.rounding?.probabilityDecimals;
  const tolerance = 1e-6 + (Number.isInteger(decimals) && decimals >= 0 ? 1.5 * 10 ** -decimals : 0);
  if (answer?.type !== 'choice' || !labels.includes(answer.choice)
      || !probabilities || Object.keys(probabilities).sort().join() !== [...labels].sort().join()
      || labels.some(label => !finite(probabilities[label]) || probabilities[label] < 0 || probabilities[label] > 1)
      || Math.abs(labels.reduce((sum, label) => sum + probabilities[label], 0) - 1) > tolerance
      || labels.some(label => probabilities[label] > probabilities[answer.choice] + 1e-6)) {
    throw Object.assign(new Error('Invalid probability distribution'), { name: 'InvalidEvaluationResponse' });
  }
  const gateway = result.providerMetadata?.gateway ?? {};
  const rawConfidence = result.providerMetadata?.typesafe?.confidence?.risk;
  return {
    status: 'ok', risk_label: answer.choice, probabilities,
    probability_decimals: Number.isInteger(decimals) ? decimals : null,
    provider_confidence: finite(rawConfidence) ? rawConfidence : null,
    usage: {
      input_tokens: numeric(result.usage?.inputTokens),
      output_tokens: numeric(result.usage?.outputTokens),
    },
    reported_cost_usd: numeric(gateway.cost),
    model_response: {
      model_id: result.response?.modelId ?? null,
      model_version: typeof gateway.modelVersion === 'string' ? gateway.modelVersion : null,
      provider: typeof gateway.provider === 'string' ? gateway.provider : null,
      generation_id: typeof gateway.generationId === 'string' ? gateway.generationId : null,
      timestamp: result.response?.timestamp?.toISOString?.() ?? null,
    },
  };
}

/** Execute one JSONL request and return an answer or an accounted failure. */
export async function handle(message) {
  const started = performance.now();
  let result;
  let rawResponse;
  try {
    if (!process.env.AI_GATEWAY_API_KEY) {
      return { status: 'missing_credentials', retryable: false, latency_ms: 0 };
    }
    const request = message.request;
    if (request?.model !== 'typesafe-ai/jev' || Object.keys(request.questions ?? {}).join() !== 'risk'
        || request.questions.risk.type !== 'choice') {
      return { status: 'invalid_request', retryable: false, latency_ms: 0 };
    }
    const provider = gateway.evaluationModel(request.model);
    const model = {
      specificationVersion: provider.specificationVersion,
      provider: provider.provider,
      modelId: provider.modelId,
      supportedQuestionTypes: provider.supportedQuestionTypes,
      async doEvaluate(options) {
        rawResponse = await provider.doEvaluate(options);
        return rawResponse;
      },
    };
    // Python owns retries so every network attempt has an accounting record.
    result = await evaluate({ ...request, model, maxRetries: 0,
      abortSignal: AbortSignal.timeout(message.timeout_ms ?? 60000) });
    return { ...normalizeResult(result), latency_ms: performance.now() - started };
  } catch (error) {
    const code = Number(error.statusCode ?? error.cause?.statusCode) || null;
    const timeout = error.name === 'TimeoutError' || error.name === 'AbortError';
    const invalid = error.name === 'InvalidEvaluationResponse' || error.name === 'AI_InvalidResponseDataError';
    const status = timeout ? 'timeout' : contextRejected(error, code) ? 'context_rejected'
      : invalid ? 'invalid_response' : 'provider_error';
    // SDK validation can reject after the provider has returned a billed response.
    const accounting = result ?? rawResponse;
    return {
      status, retryable: !invalid && (timeout || code === 429 || (code !== null && code >= 500)),
      error: { name: error.name ?? 'Error', status_code: code },
      usage: { input_tokens: numeric(accounting?.usage?.inputTokens), output_tokens: numeric(accounting?.usage?.outputTokens) },
      reported_cost_usd: numeric(accounting?.providerMetadata?.gateway?.cost),
      latency_ms: performance.now() - started,
    };
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (Number(process.versions.node.split('.')[0]) < 22) {
    process.stderr.write('The Jev worker requires Node >=22.\n');
    process.exit(1);
  }
  for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
    if (!line.trim()) continue;
    let response;
    try { response = await handle(JSON.parse(line)); }
    catch { response = { status: 'invalid_request', retryable: false }; }
    process.stdout.write(`${JSON.stringify(response)}\n`);
  }
}
