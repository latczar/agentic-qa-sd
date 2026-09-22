# Evaluation results

88 cases (80 with a known correct article, 8 deliberately outside the knowledge base)

## Retrieval

- Hit rate (expected article retrieved at all): 95.0%
- Recall@1 (expected article ranked first): 92.5%
- Correctly returned nothing for out-of-scope questions: 100.0%
- Median retrieval+analysis latency: 3253ms

## Answer quality

- Structured output success (valid JSON matching the schema, within the retry limit): 100.0%
- Category accuracy (matched an acceptable keyword): 70.0%
- Escalation decisions matching expectation: 72.7%
- Citations supported by what was actually retrieved: 100.0%
- Unsupported-answer rate (cited a document that was never retrieved): 0.0%

## Confidence

- Threshold for automatic resolution: 0.85
- At or above the threshold: 62.5% (55/88)
- Mean: 0.830   median: 0.90
- Unwanted escalations explained by confidence alone: 24

Distribution (the model emits only these values, which is why a swing in escalation correctness is often quantisation rather than a regression):

- `0.00` <  threshold    1  #
- `0.05` <  threshold    1  #
- `0.10` <  threshold    1  #
- `0.20` <  threshold    1  #
- `0.30` <  threshold    5  #####
- `0.50` <  threshold    2  ##
- `0.70` <  threshold    2  ##
- `0.75` <  threshold    3  ###
- `0.80` <  threshold   17  #################
- `0.85` >= threshold    7  #######
- `0.90` >= threshold   10  ##########
- `1.00` >= threshold   38  ######################################

## Failures worth looking at

- **lockout-3**: escalation True, expected False
- **slow-3**: escalation True, expected False
- **slow-4**: escalation True, expected False
- **starter-1**: escalation True, expected False
- **starter-3**: expected `new-starter-access-missing`, got ['screen-sharing-not-working', 'shared-drive-access-request', 'payroll-portal-access-denied']
- **vpn-2**: escalation True, expected False
- **disk-4**: escalation True, expected False
- **guest-4**: escalation True, expected False
- **monitor-1**: escalation True, expected False
- **monitor-2**: escalation True, expected False
- **monitor-3**: escalation True, expected False
- **monitor-4**: expected `external-monitor-not-detected`, got nothing; escalation True, expected False
- **charge-1**: escalation True, expected False
- **charge-2**: escalation True, expected False
- **charge-3**: escalation True, expected False
- **charge-4**: escalation True, expected False
- **cert-2**: escalation True, expected False
- **filerec-1**: escalation True, expected False
- **filerec-4**: expected `deleted-file-recovery`, got nothing; escalation True, expected False
- **drive-3**: escalation True, expected False
- **install-1**: escalation True, expected False
- **install-3**: escalation True, expected False
- **install-4**: expected `software-install-request`, got nothing; escalation True, expected False
- **timeout-1**: escalation True, expected False
- **timeout-3**: escalation True, expected False
