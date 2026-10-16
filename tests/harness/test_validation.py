import pytest

from minisweagent.harness.contracts import PublicContract
from minisweagent.harness.validation import classify


def report(outcome="passed", *, collection=None):
    return {
        "collected": collection if collection is not None else ["test_a.py::test_a"],
        "exitstatus": 1 if outcome == "failed" else 0,
        "reports": [
            {
                "nodeid": "test_a.py::test_a",
                "when": phase,
                "outcome": outcome if phase == "call" else "passed",
                "detail": "公开断言",
            }
            for phase in ("setup", "call", "teardown")
        ],
    }


def contract():
    return PublicContract(
        task_id="unit",
        image="test",
        initial_tree_hash="test",
        command=["python", "-m", "pytest"],
        expected_nodeids=["test_a.py::test_a"],
        protected_hashes={},
        source="unit",
        runner_hash="test",
    )


@pytest.mark.parametrize(
    ("data", "code", "status", "reason"),
    [
        (report(), 0, "passed", "verified_by_dev_tests"),
        (report("failed"), 1, "code_failure", "tests_failed"),
        (report("failed"), 0, "code_failure", "tests_failed"),
        (report(collection=[]), 0, "validation_error", "incomplete_collection"),
        (report(collection=["test_b.py::test_b"]), 0, "validation_error", "incomplete_collection"),
        (report(collection=["test_a.py::test_a"] * 2), 0, "validation_error", "incomplete_collection"),
        (report("skipped"), 0, "validation_error", "unexpected_skip"),
        (report(), 2, "validation_error", "unexpected_exit"),
    ],
)
def test_report_requires_complete_matching_evidence(data, code, status, reason):
    assert classify(data, contract(), code)[:2] == (status, reason)


def test_all_skipped_or_missing_teardown_never_passes():
    spec = contract().model_copy(update={"allowed_skips": ["test_a.py::test_a"]})
    assert classify(report("skipped"), spec, 0)[1] == "zero_executed"
    partial = report()
    partial["reports"].pop()
    assert classify(partial, spec, 0)[1] == "incomplete_report"
