"""Example plugin: require libraries from several *groups* (e.g. training + orchestration).

Enable it in config.toml:

    [run]
    plugins = ["plugins/library_groups.py"]

    [heuristics.training_and_orchestration]
    type = "library_groups"
    min_groups = 2
    [heuristics.training_and_orchestration.groups]
    training = ["scikit-learn", "pytorch", "tensorflow", "xgboost", "lightgbm"]
    orchestration = ["mlflow", "dvc", "kubeflow", "airflow"]
"""

from typing import Any

from ml_repo_sampler.config import ConfigError
from ml_repo_sampler.heuristics import Heuristic, register


@register
class LibraryGroups(Heuristic):
    type_name = "library_groups"
    stage = "aggregate"  # only needs the `libraries` column -> runs before enrichment
    description = "Kept hits must cover libraries from at least `min_groups` of the `groups`"
    defaults = {
        "groups": {
            "training": ["scikit-learn", "pytorch", "tensorflow", "xgboost", "lightgbm"],
            "orchestration": ["mlflow", "dvc", "kubeflow", "airflow"],
        },
        "min_groups": 2,
    }

    def validate(self) -> None:
        if not isinstance(self.params["groups"], dict) or not self.params["groups"]:
            raise ConfigError(f"[heuristics.{self.name}] 'groups' must be a non-empty table")
        if not 1 <= self.params["min_groups"] <= len(self.params["groups"]):
            raise ConfigError(f"[heuristics.{self.name}] 'min_groups' must be between 1 and the number of groups")

    def groups_found(self, record: dict[str, Any]) -> list[str]:
        libs = set(record.get("libraries") or [])
        return [g for g, members in self.params["groups"].items() if libs & set(members)]

    def check(self, record: dict[str, Any]) -> bool:
        return len(self.groups_found(record)) >= self.params["min_groups"]

    # Optional: an extra output column showing which groups were found.
    def extra_columns(self) -> list[str]:
        return [f"{self.name}_groups"]

    def annotate(self, record: dict[str, Any]) -> dict[str, Any]:
        return {f"{self.name}_groups": self.groups_found(record)}
