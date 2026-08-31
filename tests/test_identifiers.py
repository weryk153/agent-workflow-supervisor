import pytest

from agent_workflow_supervisor.config import AppConfig, ProjectConfig, TrackerConfig
from agent_workflow_supervisor.identifiers import canonical_github_issue_id
from agent_workflow_supervisor.runtime import workflow_thread_id


@pytest.mark.parametrize(
    "value",
    [
        "194",
        "#194",
        "github:owner/repo#194",
        "OWNER/REPO#194",
        "https://github.com/owner/repo/issues/194",
    ],
)
def test_equivalent_github_issue_references_have_one_canonical_id(value: str) -> None:
    assert canonical_github_issue_id(value, "owner/repo", strict=True) == "194"


def test_other_repository_reference_never_aliases_local_issue() -> None:
    assert canonical_github_issue_id("github:other/repo#194", "owner/repo") == (
        "github:other/repo#194"
    )
    with pytest.raises(ValueError, match="belongs to"):
        canonical_github_issue_id("github:other/repo#194", "owner/repo", strict=True)


def test_checkpoint_thread_uses_canonical_issue_id() -> None:
    config = AppConfig(
        project=ProjectConfig(id="demo"),
        tracker=TrackerConfig(repository="owner/repo"),
    )

    assert workflow_thread_id(config, "194") == workflow_thread_id(config, "github:owner/repo#194")
