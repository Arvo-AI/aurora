"""The tool-using graph shared by production and isolated sample investigations.

Importing this module does not initialize customer tools, storage or memory.
Those capabilities belong to the caller and must be passed explicitly.
"""

from langchain.agents import create_agent


def build_agent_graph(*, model, tools, system_prompt, middleware=None):
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware or [],
    )
