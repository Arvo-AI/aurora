"""Shared-secret authentication for the inbound ServiceNow webhook, plus the
Business Rule script the customer installs. Kept free of Flask so it can be
unit tested and reused by the routes."""

import hmac
import secrets as pysecrets
from typing import List, Optional

WEBHOOK_SECRET_HEADER = "X-Aurora-Webhook-Secret"


def new_webhook_secret() -> str:
    return pysecrets.token_urlsafe(32)


def secret_matches(stored: Optional[str], header_value: Optional[str], authorization: Optional[str]) -> bool:
    """Constant-time check of the shared secret sent by the Business Rule.

    Accepted either in the X-Aurora-Webhook-Secret header or as a bearer token.
    """
    if not stored:
        return False
    candidates: List[str] = []
    if header_value:
        candidates.append(header_value.strip())
    if authorization and authorization.lower().startswith("bearer "):
        candidates.append(authorization[7:].strip())
    return any(hmac.compare_digest(stored, candidate) for candidate in candidates if candidate)


def business_rule_script() -> str:
    """The After-Insert Business Rule the customer pastes into ServiceNow.

    The URL and secret live in two system properties so the script itself
    holds no secret and survives copy/paste between instances.
    """
    return """(function executeRule(current, previous /*null when async*/) {
  var endpoint = gs.getProperty('x_aurora.webhook_url');
  var secret = gs.getProperty('x_aurora.webhook_secret');
  if (!endpoint || !secret) {
    gs.warn('[Aurora] webhook URL or secret property not set');
    return;
  }
  var body = {
    event: current.operation() == 'insert' ? 'inserted' : 'updated',
    sys_id: current.getUniqueValue(),
    number: current.getValue('number'),
    short_description: current.getValue('short_description'),
    description: current.getValue('description'),
    priority: current.getValue('priority'),
    priority_label: current.getDisplayValue('priority'),
    impact: current.getValue('impact'),
    urgency: current.getValue('urgency'),
    state: current.getValue('state'),
    state_label: current.getDisplayValue('state'),
    category: current.getDisplayValue('category'),
    assignment_group: current.getDisplayValue('assignment_group'),
    cmdb_ci: current.getDisplayValue('cmdb_ci'),
    caller: current.getDisplayValue('caller_id'),
    opened_at: current.getValue('opened_at'),
    sys_updated_on: current.getValue('sys_updated_on')
  };
  try {
    var r = new sn_ws.RESTMessageV2();
    r.setEndpoint(endpoint);
    r.setHttpMethod('POST');
    r.setRequestHeader('Content-Type', 'application/json');
    r.setRequestHeader('X-Aurora-Webhook-Secret', secret);
    r.setRequestBody(JSON.stringify(body));
    var resp = r.execute();
    var code = resp.getStatusCode();
    if (code < 200 || code > 299) {
      gs.error('[Aurora] webhook returned HTTP ' + code + ': ' + resp.getBody());
    }
  } catch (e) {
    gs.error('[Aurora] webhook call failed: ' + e.message);
  }
})(current, previous);"""
