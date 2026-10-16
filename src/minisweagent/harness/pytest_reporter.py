"""验证容器专用 pytest 插件，兼容旧 Python，不读取裁判材料。"""

import json
import os

collected = []
reports = []
collection_errors = []


def pytest_collection_finish(session):
    collected.extend(item.nodeid for item in session.items)


def pytest_collectreport(report):
    if report.failed:
        collection_errors.append({"nodeid": report.nodeid, "detail": str(report.longrepr)})


def pytest_runtest_logreport(report):
    reports.append(
        {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "wasxfail": str(getattr(report, "wasxfail", "")),
            "detail": str(report.longrepr) if report.longrepr else "",
        }
    )


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["MINI_PUBLIC_REPORT"], "w") as stream:
        json.dump(
            {
                "collected": collected,
                "reports": reports,
                "collection_errors": collection_errors,
                "exitstatus": int(exitstatus),
            },
            stream,
        )
