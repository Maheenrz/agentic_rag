import { useState } from 'react';
import { api } from '../../lib/api';
import { PII_ACTIONS, PII_KINDS } from '../../lib/constants';
import { guard, useAsync } from '../../lib/hooks';
import { Button, Card, Check, Input, Label, Loading, Select, Textarea } from '../../components/ui';

export default function GuardrailsPage() {
  const policy = useAsync(() => api('/org/guardrails'));
  if (!policy.data) return <Loading error={policy.error} />;
  return (
    <GuardrailForm
      key={JSON.stringify(policy.data)}
      policy={policy.data}
      onSaved={policy.reload}
    />
  );
}

function GuardrailForm({ policy, onSaved }) {
  const [pii, setPii] = useState(policy.pii);
  const [topics, setTopics] = useState(policy.blocked_topics.join('\n'));
  const [injection, setInjection] = useState(policy.injection_query_action);
  const [maxChars, setMaxChars] = useState(policy.max_question_chars);
  const [blockExtraction, setBlockExtraction] = useState(policy.block_prompt_extraction);
  const [llmCheck, setLlmCheck] = useState(policy.llm_second_opinion);

  async function save() {
    await api('/org/guardrails', {
      method: 'PUT',
      body: {
        pii,
        blocked_topics: topics.split('\n').map((s) => s.trim()).filter(Boolean),
        injection_query_action: injection,
        max_question_chars: Number(maxChars),
        block_prompt_extraction: blockExtraction,
        llm_second_opinion: llmCheck,
      },
    });
    alert('Guardrails saved. Cached answers made under the old rules will no longer be reused.');
    onSaved();
  }

  return (
    <div className="max-w-3xl space-y-6">
      <Card>
        <h2 className="text-lg font-semibold">Personal data</h2>
        <p className="mb-4 text-sm text-muted">
          allow = let it through · mask = replace with [TYPE] · block = refuse the message
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          {PII_KINDS.map((kind) => (
            <Label key={kind} className="flex items-center justify-between gap-3">
              <span className="text-sm uppercase text-ink">{kind}</span>
              <Select
                value={pii[kind]}
                onChange={(e) => setPii({ ...pii, [kind]: e.target.value })}
              >
                {PII_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
              </Select>
            </Label>
          ))}
        </div>
      </Card>

      <Card>
        <h2 className="mb-2 text-lg font-semibold">Blocked topics</h2>
        <p className="mb-3 text-sm text-muted">One phrase per line. Matches whole phrases, not partial words.</p>
        <Textarea rows={4} value={topics} onChange={(e) => setTopics(e.target.value)} placeholder="salary band" />
      </Card>

      <Card className="space-y-4">
        <h2 className="text-lg font-semibold">Other rules</h2>

        <Label>
          Prompt injection found in retrieved text
          <Select className="mt-1 max-w-xs" value={injection} onChange={(e) => setInjection(e.target.value)}>
            <option value="drop">drop</option>
            <option value="flag">flag</option>
            <option value="off">off</option>
          </Select>
        </Label>

        <Label>
          Maximum question length (characters)
          <Input
            className="mt-1 max-w-xs"
            type="number"
            min={200}
            max={8000}
            value={maxChars}
            onChange={(e) => setMaxChars(e.target.value)}
          />
        </Label>

        <Check
          label="Block attempts to reveal the system prompt"
          checked={blockExtraction}
          onChange={(e) => setBlockExtraction(e.target.checked)}
        />
        <Check
          label="Ask a small model to double-check suspicious questions (extra cost)"
          checked={llmCheck}
          onChange={(e) => setLlmCheck(e.target.checked)}
        />
      </Card>

      <Button variant="primary" onClick={guard(save)}>Save guardrails</Button>
    </div>
  );
}