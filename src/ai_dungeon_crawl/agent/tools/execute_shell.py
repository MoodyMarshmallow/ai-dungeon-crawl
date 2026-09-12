import json

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai import ToolOutput
from pydantic_ai.tools import GenerateToolJsonSchema

from ...contracts import validate_source


EXECUTE_SHELL_NAME = "execute_shell"


class ShellScript(BaseModel):
    """Shell source for the harness to execute, not in this model runtime."""

    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=65536,
                      description="Bash script, at most 64 KiB UTF-8.")
    timeout_ms: int | None = Field(default=None, ge=1, le=180000,
                                  description="Execution timeout in milliseconds; null uses the configured default (normally 5000). Maximum 180000.")

    @field_validator("code")
    @classmethod
    def check_source_size(cls, value: str) -> str:
        validate_source(value)
        return value


def execution_feedback(execution) -> str:
    """Only explicit shell feedback crosses into the model conversation."""
    return json.dumps({"output": execution.output, "error": execution.error,
                       "status": execution.status,
                       "output_truncated": execution.output_truncated}, ensure_ascii=False)


def execute_shell_tool(description: str) -> ToolOutput:
    """Build the validated output tool used for one shell submission."""
    return ToolOutput(ShellScript, name=EXECUTE_SHELL_NAME, description=description)


def execute_shell_reference(description: str) -> dict:
    """Return the provider-independent tool schema included in episode references."""
    schema = ShellScript.model_json_schema(schema_generator=GenerateToolJsonSchema)
    fixed_description = schema.pop('description')
    return {"name": EXECUTE_SHELL_NAME, "description": description + '. ' + fixed_description,
            "parameters_json_schema": schema}
