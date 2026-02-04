#!/bin/bash
# Simple Bedrock Credit Usage Tracker
# Run this anytime to see your credit burn
# Usage: ./bedrock-usage.sh [hours]
# Example: ./bedrock-usage.sh 48  (for last 48 hours)
# Default: 24 hours if no argument provided

# Get hours from command line argument, default to 24
HOURS=${1:-24}

echo "Fetching Bedrock API logs from last ${HOURS} hours..."
echo ""

# Calculate timestamp for N hours ago (in milliseconds)
# Use BSD date syntax for macOS compatibility
START_TIME=$(($(date -v-${HOURS}H +%s) * 1000))

# Fetch logs and save to temp file
# Note: Removed --max-items limit to fetch all events in the time range
# AWS CLI will automatically paginate and fetch all results
aws logs filter-log-events \
    --log-group-name /aws/bedrock/modelinvocations \
    --start-time $START_TIME \
    --region us-east-1 \
    --output json > /tmp/bedrock_raw.json

# Check if we got logs
if [ ! -s /tmp/bedrock_raw.json ]; then
    echo "No logs found. Either:"
    echo "   1. No API calls in last ${HOURS} hours"
    echo "   2. Logging just enabled (wait a few minutes)"
    exit 1
fi

# Parse and calculate credits
python3 - "$HOURS" << 'PYEOF'
import json
import sys
from collections import defaultdict

# Get hours from command line argument
hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24

# Pricing per 1M tokens (AWS Bedrock - Standard Tier, US East N. Virginia)
# Updated: January 2026
PRICING = {
    'input': {
        'claude-3-5-sonnet-20241022': 3.00,
        'claude-3-5-haiku-20241022': 0.80,
        'claude-haiku-4-5-20251001': 1.00,
        'claude-opus-4-20250514': 15.00,
        'claude-opus-4-5-20251101': 5.00,
        'claude-sonnet-4-5-20250929': 3.00,
        'claude-3-5-sonnet-v2': 6.00,
    },
    'output': {
        'claude-3-5-sonnet-20241022': 15.00,
        'claude-3-5-haiku-20241022': 4.00,
        'claude-haiku-4-5-20251001': 5.00,
        'claude-opus-4-20250514': 75.00,
        'claude-opus-4-5-20251101': 25.00,
        'claude-sonnet-4-5-20250929': 15.00,
        'claude-3-5-sonnet-v2': 3.00,
    },
    'cache_write': {
        'claude-3-5-sonnet-20241022': 3.75,
        'claude-3-5-haiku-20241022': 1.00,
        'claude-haiku-4-5-20251001': 1.25,
        'claude-opus-4-20250514': 18.75,
        'claude-opus-4-5-20251101': 6.25,
        'claude-sonnet-4-5-20250929': 3.75,
        'claude-3-5-sonnet-v2': 7.50,
    },
    'cache_read': {
        'claude-3-5-sonnet-20241022': 0.30,
        'claude-3-5-haiku-20241022': 0.08,
        'claude-haiku-4-5-20251001': 0.10,
        'claude-opus-4-20250514': 1.50,
        'claude-opus-4-5-20251101': 0.50,
        'claude-sonnet-4-5-20250929': 0.30,
        'claude-3-5-sonnet-v2': 0.60,
    }
}

user_stats = defaultdict(lambda: {
    'total_credits': 0,
    'input_tokens': 0,
    'output_tokens': 0,
    'cache_read': 0,
    'cache_write': 0,
    'calls': 0,
    'models': defaultdict(lambda: {'calls': 0, 'credits': 0})
})

# Read logs
with open('/tmp/bedrock_raw.json') as f:
    data = json.load(f)

for event in data.get('events', []):
    try:
        msg = json.loads(event['message'])
        
        if 'modelId' not in msg:
            continue
        
        # Extract model name from full Bedrock model ID
        # e.g., "us.anthropic.claude-haiku-4-5-20251001-v1:0" -> "claude-haiku-4-5-20251001"
        full_model_id = msg['modelId']
        # Remove region prefix and version suffix
        model_parts = full_model_id.split('.')
        if len(model_parts) >= 3:
            model_id = '.'.join(model_parts[1:]).split('-v')[0]
            model_id = model_id.replace('anthropic.', '')
        else:
            model_id = full_model_id.split('/')[-1]
        
        user_arn = msg.get('identity', {}).get('arn', 'unknown')
        user = user_arn.split('/')[-1]
        
        input_tokens = msg.get('input', {}).get('inputTokenCount', 0)
        output_tokens = msg.get('output', {}).get('outputTokenCount', 0)
        cache_read = msg.get('input', {}).get('cacheReadInputTokenCount', 0)
        cache_write = msg.get('input', {}).get('cacheWriteInputTokenCount', 0)
        
        # Calculate credits
        input_price = PRICING['input'].get(model_id, 0)
        output_price = PRICING['output'].get(model_id, 0)
        cache_read_price = PRICING['cache_read'].get(model_id, 0)
        cache_write_price = PRICING['cache_write'].get(model_id, 0)
        
        credits = (
            (input_tokens / 1_000_000) * input_price +
            (output_tokens / 1_000_000) * output_price +
            (cache_read / 1_000_000) * cache_read_price +
            (cache_write / 1_000_000) * cache_write_price
        )
        
        # Aggregate
        user_stats[user]['total_credits'] += credits
        user_stats[user]['input_tokens'] += input_tokens
        user_stats[user]['output_tokens'] += output_tokens
        user_stats[user]['cache_read'] += cache_read
        user_stats[user]['cache_write'] += cache_write
        user_stats[user]['calls'] += 1
        user_stats[user]['models'][model_id]['calls'] += 1
        user_stats[user]['models'][model_id]['credits'] += credits
        
    except Exception as e:
        continue

# Print results
print("\n" + "="*80)
print(f"BEDROCK CREDIT USAGE (Last {hours} Hours)")
print("="*80 + "\n")

total_credits = 0
for user in sorted(user_stats.keys(), key=lambda u: user_stats[u]['total_credits'], reverse=True):
    stats = user_stats[user]
    print(f"User: {user}")
    print(f"   Total Cost: ${stats['total_credits']:.4f}")
    print(f"   API Calls: {stats['calls']}")
    print(f"   Input Tokens: {stats['input_tokens']:,} (${(stats['input_tokens'] / 1_000_000) * 3.00:.4f} avg)")
    print(f"   Output Tokens: {stats['output_tokens']:,} (${(stats['output_tokens'] / 1_000_000) * 15.00:.4f} avg)")
    print(f"   Cache Write: {stats['cache_write']:,} tokens (${(stats['cache_write'] / 1_000_000) * 3.75:.4f} avg)")
    print(f"   Cache Read: {stats['cache_read']:,} tokens (${(stats['cache_read'] / 1_000_000) * 0.30:.4f} avg)")
    
    if len(stats['models']) > 0:
        print(f"   Cost by Model:")
        for model, mstats in sorted(stats['models'].items(), key=lambda x: x[1]['credits'], reverse=True):
            print(f"      - {model}: {mstats['calls']} calls, ${mstats['credits']:.4f}")
    
    print()
    total_credits += stats['total_credits']

print("="*80)
print(f"TOTAL COST ({hours}h): ${total_credits:.4f}")
print(f"Daily Average: ${total_credits / (hours / 24):.4f}")
print(f"Monthly Estimate (30 days): ${(total_credits / hours) * 24 * 30:.2f}")
print(f"Remaining from $5,000/month: ${5000 - ((total_credits / hours) * 24 * 30):.2f}")
print("="*80)
print()
print("TOTAL BY MODEL (All Users):")
# Aggregate costs by model across all users
model_totals = defaultdict(lambda: {'calls': 0, 'credits': 0})
for user in user_stats.values():
    for model, mstats in user['models'].items():
        model_totals[model]['calls'] += mstats['calls']
        model_totals[model]['credits'] += mstats['credits']

if model_totals:
    for model, mstats in sorted(model_totals.items(), key=lambda x: x[1]['credits'], reverse=True):
        print(f"   - {model}: {mstats['calls']} calls, ${mstats['credits']:.4f}")
else:
    print("   No model data available")
print("="*80)
print()
PYEOF

# Cleanup
rm -f /tmp/bedrock_raw.json
