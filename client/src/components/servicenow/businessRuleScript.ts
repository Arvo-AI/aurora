// The Business Rule the customer pastes into ServiceNow (System Definition > Business Rules).
// It reads the Aurora webhook URL and secret from two system properties, so the script itself
// carries no secret and survives copy/paste between instances. It is the same for every user,
// so it ships with the client instead of coming back from the API.
export const SERVICENOW_BUSINESS_RULE_SCRIPT = `(function executeRule(current, previous /*null when async*/) {
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
    subcategory: current.getDisplayValue('subcategory'),
    assignment_group: current.getDisplayValue('assignment_group'),
    assigned_to: current.getDisplayValue('assigned_to'),
    cmdb_ci: current.getDisplayValue('cmdb_ci'),
    caller: current.getDisplayValue('caller_id'),
    opened_at: current.getValue('opened_at'),
    resolved_at: current.getValue('resolved_at'),
    close_code: current.getValue('close_code'),
    close_notes: current.getValue('close_notes'),
    sys_updated_on: current.getValue('sys_updated_on')
  };
  try {
    var r = new sn_ws.RESTMessageV2();
    r.setEndpoint(endpoint);
    r.setHttpMethod('POST');
    r.setHttpTimeout(10000); // never hold the incident save on a slow Aurora
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
})(current, previous);`;
