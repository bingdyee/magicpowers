from __future__ import annotations

from pathlib import Path
from typing import override
from google.adk.skills import load_skill_from_dir, models
from google.adk.tools import skill_toolset
from google.adk.tools.tool_configs import ToolArgsConfig
from ._paths import resolve_working_path


def load_skills_from_dir(skills_dir: Path | str) -> list[models.Skill]:
    """
    Load all skills from the specified directory and return a list of skill instances.
    """
    skills_base_path = Path(skills_dir).resolve()
    skills = []
    if not skills_base_path.is_dir():
        return skills
    for skill_dir in sorted(skills_base_path.iterdir()):
        if not skill_dir.is_dir():
          continue
        skill = load_skill_from_dir(skill_dir)
        if skill:
            skills.append(skill)
    return skills


class SkillsToolset(skill_toolset.SkillToolset):

    def __init__(self, skills_dir: str | Path | None):
        self.skills_dir = Path(skills_dir).resolve() if skills_dir else Path(resolve_working_path("skills"))
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        skills = load_skills_from_dir(self.skills_dir)
        super().__init__(skills=skills)

    @override
    @classmethod
    def from_config(
        cls: type[SkillsToolset], config: ToolArgsConfig, config_abs_path: str
    ) -> SkillsToolset:
        skills_dir = config.model_dump().pop("skills_dir",  None)
        return cls(skills_dir)

    @override
    def _list_skills(self) -> list[models.Skill]:
        skills = load_skills_from_dir(self.skills_dir)
        self._skills = {skill.name: skill for skill in skills}
        return super()._list_skills()
