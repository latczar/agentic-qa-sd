# Evaluation results

40 cases (32 with a known correct article, 8 deliberately outside the knowledge base)

## Retrieval

- Hit rate (expected article retrieved at all): 100.0%
- Recall@1 (expected article ranked first): 96.9%
- Correctly returned nothing for out-of-scope questions: 100.0%
- Median retrieval+analysis latency: 8820ms

## Answer quality

- Structured output success (valid JSON matching the schema, within the retry limit): 92.5%
- Category accuracy (matched an acceptable keyword): 87.5%
- Escalation decisions matching expectation: 54.1%
- Citations supported by what was actually retrieved: 100.0%
- Unsupported-answer rate (cited a document that was never retrieved): 0.0%

## Failures worth looking at

- **lockout-2**: escalation True, expected False
- **lockout-3**: escalation True, expected False
- **lockout-4**: escalation True, expected False
- **slow-3**: escalation True, expected False
- **slow-4**: escalation True, expected False
- **starter-1**: escalation True, expected False
- **pwreset-2**: escalation True, expected False
- **payroll-1**: escalation False, expected True
- **payroll-2**: escalation False, expected True
- **payroll-3**: escalation False, expected True
- **printer-1**: escalation True, expected False
- **printer-2**: escalation True, expected False
- **printer-3**: escalation True, expected False
- **printer-4**: escalation True, expected False
- **vpn-1**: escalation True, expected False
- **vpn-2**: escalation True, expected False
- **vpn-4**: escalation True, expected False
- **negative-2**: no valid output (model failed to produce a valid analysis after 3 attempts: response didn't match the required schema: 2 validation errors for TicketAnalysis
likely_root_cause
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/string_type
recommended_resolution
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/string_type)
- **negative-5**: no valid output (model failed to produce a valid analysis after 3 attempts: response didn't match the required schema: 2 validation errors for TicketAnalysis
likely_root_cause
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/string_type
recommended_resolution
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/string_type)
- **negative-7**: no valid output (model failed to produce a valid analysis after 3 attempts: response didn't match the required schema: 2 validation errors for TicketAnalysis
likely_root_cause
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/string_type
recommended_resolution
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
    For further information visit https://errors.pydantic.dev/2.13/v/string_type)
