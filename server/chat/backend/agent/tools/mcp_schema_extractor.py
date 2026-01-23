"""
MCP Schema Extractor - Properly extracts and converts MCP tool schemas to Pydantic models
This module fixes the syntax issues by properly handling inputSchema from MCP servers.
"""

import json
import logging
from typing import Dict, Any, Type, Optional, List
from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo

logger = logging.getLogger(__name__)

def json_schema_to_pydantic_field(schema: Dict[str, Any], field_name: str) -> tuple[type, FieldInfo]:
    """Convert a JSON Schema property to a Pydantic field definition."""
    
    # Determine the Python type from JSON Schema type
    json_type = schema.get('type', 'string')
    field_type = str  # default
    
    type_mapping = {
        'string': str,
        'integer': int,
        'number': float,
        'boolean': bool,
        'array': list,
        'object': dict
    }
    
    field_type = type_mapping.get(json_type, str)
    
    # Handle array types
    if json_type == 'array' and 'items' in schema:
        items_type = schema['items'].get('type', 'string')
        if items_type in type_mapping:
            field_type = List[type_mapping[items_type]]
    
    # Create field with description
    description = schema.get('description', f'Parameter {field_name}')
    
    # Check if field is required (this should be passed from parent)
    # For now, we'll make all fields optional by providing a default
    if schema.get('default') is not None:
        field_info = Field(default=schema['default'], description=description)
    else:
        # Make field optional if not explicitly required
        field_info = Field(default=None, description=description)
    
    return field_type, field_info

def extract_mcp_tool_schema(tool_def: Dict[str, Any]) -> Optional[Type[BaseModel]]:
    """
    Extract the inputSchema from an MCP tool definition and convert it to a Pydantic model.
    
    Args:
        tool_def: The tool definition from MCP server containing 'name', 'description', and 'inputSchema'
    
    Returns:
        A Pydantic BaseModel class that can be used as args_schema for StructuredTool
    """
    
    tool_name = tool_def.get('name', 'unknown')
    input_schema = tool_def.get('inputSchema', {})
    
    if not input_schema:
        logger.warning(f"Tool {tool_name} has no inputSchema defined")
        return None
    
    # Extract properties and required fields
    properties = input_schema.get('properties', {})
    required_fields = input_schema.get('required', [])
    
    if not properties:
        logger.warning(f"Tool {tool_name} has empty properties in inputSchema")
        return None
    
    # Build field definitions for Pydantic model
    field_definitions = {}
    
    for prop_name, prop_schema in properties.items():
        try:
            field_type, field_info = json_schema_to_pydantic_field(prop_schema, prop_name)
            
            # If field is required, remove the default
            if prop_name in required_fields:
                # Create a new Field without default for required fields
                field_info = Field(description=prop_schema.get('description', f'Parameter {prop_name}'))
                field_definitions[prop_name] = (field_type, field_info)
            else:
                # Optional field with None default
                field_definitions[prop_name] = (Optional[field_type], field_info)
                
        except Exception as e:
            logger.error(f"Error processing field {prop_name} for tool {tool_name}: {e}")
            # Skip this field but continue with others
            continue
    
    if not field_definitions:
        logger.warning(f"No valid fields extracted for tool {tool_name}")
        return None
    
    # Create dynamic Pydantic model
    try:
        # Create a class name based on the tool name
        class_name = f"{tool_name.replace('-', '_').replace(' ', '_').title()}Args"
        
        # Create the model dynamically
        model = create_model(
            class_name,
            **field_definitions,
            __base__=BaseModel
        )
        
        logger.info(f" Created schema model {class_name} for tool {tool_name} with fields: {list(field_definitions.keys())}")
        return model
        
    except Exception as e:
        logger.error(f"Failed to create Pydantic model for tool {tool_name}: {e}")
        return None

def log_tool_schema(tool_def: Dict[str, Any]) -> None:
    """Log the full schema of an MCP tool for debugging."""
    
    tool_name = tool_def.get('name', 'unknown')
    input_schema = tool_def.get('inputSchema', {})
    
    logger.info(f"\n MCP Tool: {tool_name}")
    logger.info(f"   Description: {tool_def.get('description', 'No description')}")
    
    if input_schema:
        logger.info(f"   Input Schema:")
        logger.info(f"   - Type: {input_schema.get('type', 'unknown')}")
        
        properties = input_schema.get('properties', {})
        if properties:
            logger.info(f"   - Properties:")
            for prop_name, prop_schema in properties.items():
                required = prop_name in input_schema.get('required', [])
                req_marker = " (required)" if required else " (optional)"
                logger.info(f"     • {prop_name}{req_marker}: {prop_schema.get('type', 'any')} - {prop_schema.get('description', 'no description')}")
        
        required = input_schema.get('required', [])
        if required:
            logger.info(f"   - Required fields: {', '.join(required)}")
    else:
        logger.info(f"     No input schema defined")

def get_github_tool_schemas() -> Dict[str, Type[BaseModel]]:
    """
    Returns hardcoded schemas for common GitHub MCP tools.
    This is a fallback when dynamic schema extraction fails.
    """
    
    class CreateRepositoryArgs(BaseModel):
        name: str = Field(description="Repository name")
        description: Optional[str] = Field(default=None, description="Repository description")
        private: Optional[bool] = Field(default=False, description="Whether the repository should be private")
        auto_init: Optional[bool] = Field(default=True, description="Initialize with README")
    
    class ListRepositoriesArgs(BaseModel):
        per_page: Optional[int] = Field(default=30, description="Number of results per page")
        page: Optional[int] = Field(default=1, description="Page number")
    
    class SearchRepositoriesArgs(BaseModel):
        query: str = Field(description="Search query (e.g., 'user:username' to list user's repos)")
        per_page: Optional[int] = Field(default=30, description="Number of results per page")
    
    class GetRepositoryArgs(BaseModel):
        owner: str = Field(description="Repository owner")
        name: str = Field(description="Repository name")
    
    class CreateIssueArgs(BaseModel):
        owner: str = Field(description="Repository owner")
        repo: str = Field(description="Repository name")
        title: str = Field(description="Issue title")
        body: Optional[str] = Field(default=None, description="Issue body")
    
    class CreatePullRequestArgs(BaseModel):
        owner: str = Field(description="Repository owner")
        repo: str = Field(description="Repository name")
        title: str = Field(description="PR title")
        body: Optional[str] = Field(default=None, description="PR body")
        head: str = Field(description="Branch containing changes")
        base: str = Field(description="Branch to merge into")
    
    class CreateOrUpdateFileArgs(BaseModel):
        owner: str = Field(description="Repository owner")
        repo: str = Field(description="Repository name")
        path: str = Field(description="File path in repository")
        message: str = Field(description="Commit message")
        content: str = Field(description="File content")
        branch: Optional[str] = Field(default="main", description="Branch to commit to")
    
    return {
        "create_repository": CreateRepositoryArgs,
        "list_repositories": ListRepositoriesArgs,
        "search_repositories": SearchRepositoriesArgs,
        "get_repository": GetRepositoryArgs,
        "create_issue": CreateIssueArgs,
        "create_pull_request": CreatePullRequestArgs,
        "create_or_update_file": CreateOrUpdateFileArgs,
    }
