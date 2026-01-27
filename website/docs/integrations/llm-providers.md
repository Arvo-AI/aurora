---
sidebar_position: 2
---

# LLM Providers

Aurora requires an LLM provider for its AI-powered investigation capabilities.

## Supported Providers

| Provider | Environment Variable | Get API Key |
|----------|---------------------|-------------|
| **OpenRouter** | `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **OpenAI** | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Anthropic** | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
| **Google AI** | `GOOGLE_AI_API_KEY` | [ai.google.dev](https://ai.google.dev/) |

## Recommended: OpenRouter

OpenRouter provides access to multiple models through a single API key:

- Access to GPT-4, Claude, Llama, Mixtral, and more
- Pay-per-token pricing
- No monthly commitment
- Easy model switching

```bash
OPENROUTER_API_KEY=sk-or-v1-...
```

## Configuration

Add your chosen provider's API key to `.env`:

```bash
# Option 1: OpenRouter (recommended)
OPENROUTER_API_KEY=sk-or-v1-...

# Option 2: OpenAI
OPENAI_API_KEY=sk-...

# Option 3: Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Option 4: Google AI
GOOGLE_AI_API_KEY=...
```

Only **one** provider is required.

## Model Selection

Aurora automatically selects appropriate models based on the task. The default models are:

| Task | Default Model |
|------|---------------|
| Investigation | GPT-4 / Claude 3 |
| Summarization | GPT-3.5 / Claude Instant |
| Embeddings | text-embedding-ada-002 |

## Cost Considerations

LLM costs depend on:

- **Tokens processed**: Longer investigations use more tokens
- **Model choice**: GPT-4 costs more than GPT-3.5
- **Frequency**: More investigations = higher costs

### Typical Usage

| Operation | Approximate Tokens |
|-----------|-------------------|
| Simple query | 500-1,000 |
| Investigation | 2,000-5,000 |
| Complex RCA | 5,000-15,000 |

### Cost Optimization

1. Use OpenRouter for flexible model selection
2. Start with faster/cheaper models for simple queries
3. Reserve expensive models for complex investigations

## Troubleshooting

### "Invalid API key"

- Check key is correctly copied (no extra spaces)
- Verify key is active in provider dashboard
- Ensure correct environment variable name

### "Rate limit exceeded"

- Wait and retry
- Consider upgrading your API tier
- Reduce concurrent investigations

### "Model not available"

- Check provider status page
- Try a different model
- Ensure your API key has access to the model
