from pydantic import BaseModel


class AgentDefinition(BaseModel):
    name: str
    area: str
    max_steps: int = 40
    max_failures: int = 3
    prompt_template: str
    use_vision: bool = True
    timeout_seconds: int = 600
    priority: int = 1

    def render_prompt(self, base_url: str, email: str, password: str) -> str:
        return self.prompt_template.format(
            base_url=base_url,
            email=email,
            password=password,
        )
