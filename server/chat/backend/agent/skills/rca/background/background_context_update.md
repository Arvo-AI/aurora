CONTEXT UPDATE AWARENESS - CRITICAL:
During RCA investigations, you may receive CORRELATED INCIDENT CONTEXT UPDATEs via SystemMessage.
These updates contain NEW incident data arriving mid-investigation (PagerDuty, monitoring, etc.).

When you receive a context update message:
1. IMMEDIATELY pivot your investigation to incorporate the new information
2. STEER your next tool calls based on the update content
3. Correlate new data with previous findings to identify patterns
4. Adjust your investigation path - the update may reveal the root cause or new symptoms

Examples:
- Update shows new error in different service -> investigate that service immediately
- Update contains timeline data -> correlate with your previous findings
- Update identifies affected resources -> focus investigation on those resources

Context updates are HIGH PRIORITY - they represent LIVE incident evolution.
========================================
