import test from 'node:test';
import assert from 'node:assert/strict';
import { gateway } from 'ai';
import { handle, normalizeResult } from './jev-worker.mjs';

const request = { model: 'typesafe-ai/jev', state: { files: [] }, questions: {
  risk: { type: 'choice', instructions: 'Assess risk', criteria: { low: 'low', medium: 'medium', high: 'high' } },
}, providerOptions: {} };
const response = { answers: { risk: { type: 'choice', choice: 'low', probabilities: { low: 0.8, medium: 0.1, high: 0.1 } } },
  usage: { inputTokens: 100, outputTokens: 3 }, providerMetadata: { gateway: { cost: '0.0000042' } }, response: { modelId: 'typesafe-ai/jev' } };

let savedKey;
test.beforeEach(() => {
  savedKey = process.env.AI_GATEWAY_API_KEY;
  process.env.AI_GATEWAY_API_KEY = 'fake-test-only';
});
test.afterEach(() => {
  if (savedKey === undefined) delete process.env.AI_GATEWAY_API_KEY;
  else process.env.AI_GATEWAY_API_KEY = savedKey;
});

function mockProvider(t, doEvaluate) {
  return t.mock.method(gateway, 'evaluationModel', id => {
    assert.equal(id, 'typesafe-ai/jev');
    return { specificationVersion: 'v4', modelId: id, provider: 'test',
      supportedQuestionTypes: ['choice'], doEvaluate };
  });
}

test('worker calls the installed evaluation contract once', async t => {
  let calls = 0;
  mockProvider(t, async options => {
    calls += 1;
    assert.deepEqual(options.state, request.state);
    assert.deepEqual(options.questions, request.questions);
    return { ...response, warnings: [] };
  });
  const result = await handle({ request });
  assert.equal(calls, 1);
  assert.equal(result.status, 'ok');
  assert.equal(result.reported_cost_usd, 0.0000042);
  assert.equal(result.provider_confidence, null);
});
test('TypeSafe confidence metadata is preserved independently of the selected probability', async t => {
  mockProvider(t, async () => ({ ...response, warnings: [],
    providerMetadata: { ...response.providerMetadata, typesafe: { confidence: { risk: .67 } } } }));
  const result = await handle({ request });
  assert.equal(result.provider_confidence, .67);
  assert.equal(result.probabilities.low, .8);
});
test('rounded probabilities and absent accounting are retained without invented zeros', () => {
  const result = normalizeResult({ answers: { risk: { type: 'choice', choice: 'low', probabilities: { low: .33, medium: .33, high: .33 } } }, rounding: { probabilityDecimals: 2 } });
  assert.equal(result.probabilities.low, .33);
  assert.equal(result.usage.input_tokens, null);
  assert.equal(result.reported_cost_usd, null);
});
test('real SDK rejects malformed probabilities without dropping billed usage or cost', async t => {
  let calls = 0;
  mockProvider(t, async () => {
    calls += 1;
    return { ...response, warnings: [], answers: { risk: { type: 'choice', choice: 'low', probabilities: { low: .1, medium: .1, high: .1 } } } };
  });
  const result = await handle({ request });
  assert.equal(result.status, 'invalid_response');
  assert.equal(result.error.name, 'AI_InvalidResponseDataError');
  assert.equal(result.retryable, false);
  assert.equal(calls, 1);
  assert.equal(result.usage.input_tokens, 100);
  assert.equal(result.usage.output_tokens, 3);
  assert.equal(result.reported_cost_usd, .0000042);
});
test('missing key never makes an API call', async t => {
  delete process.env.AI_GATEWAY_API_KEY;
  mockProvider(t, () => assert.fail());
  assert.equal((await handle({ request })).status, 'missing_credentials');
});
test('429 is retryable but worker makes only one call and omits sensitive error messages', async t => {
  let calls = 0;
  mockProvider(t, async () => { calls += 1; throw Object.assign(new Error('SECRET'), { statusCode: 429 }); });
  const result = await handle({ request });
  assert.equal(calls, 1);
  assert.equal(result.retryable, true);
  assert.equal(JSON.stringify(result).includes('SECRET'), false);
});
test('Jev token-limit 400 is an explicit context rejection without retrying', async t => {
  let calls = 0;
  mockProvider(t, async () => {
    calls += 1;
    throw Object.assign(new Error('{"error_type":"max_tokens_exceeded"}'), {
      name: 'GatewayInternalServerError', statusCode: 400,
    });
  });
  const result = await handle({ request });
  assert.equal(result.status, 'context_rejected');
  assert.equal(result.retryable, false);
  assert.equal(calls, 1);
  assert.equal(result.usage.input_tokens, null);
  assert.equal(result.reported_cost_usd, null);
  assert.equal('risk_label' in result, false);
});
test('other 400 responses remain provider errors without exposing messages', async t => {
  mockProvider(t, async () => { throw Object.assign(new Error('SECRET bad request'), { statusCode: 400 }); });
  const result = await handle({ request });
  assert.equal(result.status, 'provider_error');
  assert.equal(result.retryable, false);
  assert.equal(JSON.stringify(result).includes('SECRET'), false);
});
