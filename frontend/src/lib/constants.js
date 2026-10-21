export const REASONS = [
  ['wrong_answer', 'Wrong answer'],
  ['missing_info', 'Missing information'],
  ['bad_citation', 'Bad citation'],
  ['not_helpful', 'Not helpful'],
  ['other', 'Other'],
];

// Keep in sync with backend/app/guardrails/pii.py KINDS.
export const PII_KINDS = [
  'card',
  'cnic',
  'iban',
  'ssn',
  'email',
  'phone',
  'ip',
  'person',
  'address',
  'location',
];

export const PII_ACTIONS = ['allow', 'mask', 'block'];

export const DOC_ACCEPT = '.pdf,.txt,.md,.docx,.html,.htm,.xlsx';
