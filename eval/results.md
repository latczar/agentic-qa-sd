# Evaluation results

40 cases (32 with a known correct article, 8 deliberately outside the knowledge base)

## Retrieval

- Hit rate (expected article retrieved at all): 96.9%
- Recall@1 (expected article ranked first): 90.6%
- Correctly returned nothing for out-of-scope questions: 100.0%
- Median retrieval+analysis latency: 3838ms

## Answer quality

- Structured output success (valid JSON matching the schema, within the retry limit): 100.0%
- Category accuracy (matched an acceptable keyword): 84.4%
- Escalation decisions matching expectation: 75.0%
- Citations supported by what was actually retrieved: 100.0%
- Unsupported-answer rate (cited a document that was never retrieved): 0.0%

## Failures worth looking at

- **lockout-2**: escalation True, expected False
- **lockout-3**: escalation True, expected False
- **lockout-4**: escalation True, expected False
- **slow-1**: escalation True, expected False
- **slow-2**: escalation True, expected False
- **slow-3**: escalation True, expected False
- **slow-4**: escalation True, expected False
- **starter-1**: escalation True, expected False
- **starter-3**: expected `new-starter-access-missing`, got ['screen-sharing-not-working', 'shared-drive-access-request', 'payroll-portal-access-denied']
- **pwreset-2**: escalation True, expected False
- **vpn-2**: escalation True, expected False
