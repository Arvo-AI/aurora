from pydantic import BaseModel


class AgentDefinition(BaseModel):
    name: str
    area: str
    max_steps: int = 40
    max_failures: int = 5
    prompt_template: str
    use_vision: bool = True
    timeout_seconds: int = 0  # 0 = auto-calculate from max_steps
    priority: int = 1
    requires_pr_description: bool = False  # If True, only runs when PR description is available

    def render_prompt(
        self,
        base_url: str,
        email: str,
        password: str,
        pr_description: str | None = None,
    ) -> str:
        try:
            prompt = self.prompt_template.format(
                base_url=base_url,
                email=email,
                password=password,
            )
        except KeyError as exc:
            raise ValueError(
                f"Agent '{self.name}': prompt_template contains unknown placeholder {exc}. "
                "Use double-braces {{...}} to include literal curly-brace text."
            ) from exc
        # Replace {pr_description} placeholder (double-braced in template to survive .format())
        if pr_description and "{pr_description}" in self.prompt_template:
            # Template uses literal {pr_description} escaped as {{pr_description}}
            prompt = prompt.replace("{pr_description}", pr_description)
        return prompt
