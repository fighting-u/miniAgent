from pathlib import Path

import yaml


# 技能目录，每个技能使用一个子目录，并把完整说明写在子目录的 SKILL.md 中。
SKILLS_DIR = Path(__file__).resolve().parent / "skills"


class SkillLoader:
    """发现技能目录，并按名称提供技能目录摘要和完整说明。"""

    def __init__(self, skills_dir: Path) -> None:
        """保存技能目录，并在启动时发现其中的技能。"""
        self.skills_dir = skills_dir
        self.skills = self._discover()

    def _discover(self) -> dict[str, dict[str, str | Path]]:
        """扫描所有 SKILL.md，并从 YAML 头部读取名称和描述。"""
        skills: dict[str, dict[str, str | Path]] = {}
        if not self.skills_dir.exists():
            return skills

        for skill_file in sorted(self.skills_dir.glob("*/SKILL.md")):
            content = skill_file.read_text(encoding="utf-8")
            metadata = self._read_metadata(content)
            name = str(metadata.get("name") or skill_file.parent.name)
            description = str(metadata.get("description") or "暂无描述。")
            skills[name] = {"description": description, "path": skill_file}
        return skills

    @staticmethod
    def _read_metadata(content: str) -> dict[str, object]:
        """读取 SKILL.md 开头的 YAML 元数据；缺失或无效时返回空字典。"""
        if not content.startswith("---"):
            return {}

        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}

        try:
            metadata = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return {}
        return metadata if isinstance(metadata, dict) else {}

    def catalog(self) -> str:
        """返回适合放入系统提示词的技能名称和简要描述。"""
        if not self.skills:
            return "（暂无可用技能）"
        return "\n".join(f"- {name}: {skill['description']}" for name, skill in self.skills.items())

    def load(self, name: str) -> str:
        """按名称读取技能完整说明，名称不存在时返回可用技能列表。"""
        skill = self.skills.get(name)
        if skill is not None:
            return Path(skill["path"]).read_text(encoding="utf-8")

        available = ", ".join(self.skills) or "(none)"
        return f"Error: Unknown skill '{name}'. Available: {available}"


# 当前 Agent 共享的技能加载器。
SKILL_LOADER = SkillLoader(SKILLS_DIR)
