"""Prompts package for triagepilot."""

from pathlib import Path


def get_prompts_directory() -> Path:
    """Get the path to the prompts directory."""
    return Path(__file__).parent


def load_prompt(name: str) -> str:
    """
    Load a prompt file by name.

    Args:
        name: The prompt name (without .prompt.md extension)

    Returns:
        The content of the prompt file
    """
    prompts_dir = get_prompts_directory()
    prompt_path = prompts_dir / f"{name}.prompt.md"

    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    return prompt_path.read_text(encoding="utf-8")


def get_available_prompts() -> list[str]:
    """Get a list of available prompt names."""
    prompts_dir = get_prompts_directory()
    prompt_files = prompts_dir.glob("*.prompt.md")
    return [f.stem.replace(".prompt", "") for f in prompt_files]
