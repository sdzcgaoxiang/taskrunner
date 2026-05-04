"""Tests for TaskRunner — 单任务 YAML 驱动执行器"""
import json
import os
import sys
import tempfile
import time

import pytest
import yaml

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from taskrunner import (
    check_success,
    describe_matched_rule,
    replace_params,
    load_config,
    run_step,
    run_task,
    Logger,
)


# ─── Fixtures ───

@pytest.fixture
def tmp_dir(tmp_path):
    """Temp directory with a basic config YAML"""
    return tmp_path


@pytest.fixture
def make_config(tmp_path):
    """Helper: write a tasks config dict to a temp YAML, return path"""
    def _make(config_dict):
        path = tmp_path / "tasks.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config_dict, f)
        return str(path)
    return _make


@pytest.fixture
def simple_config(make_config):
    """Minimal single-task config"""
    return make_config({
        "tasks": {
            "hello": {
                "steps": [
                    {"name": "greet", "command": "echo hello"},
                ]
            }
        }
    })


# ─── check_success ───

class TestCheckSuccess:
    def test_empty_conditions_exit0(self):
        assert check_success([], 0, "") is True

    def test_empty_conditions_exit1(self):
        assert check_success([], 1, "") is False

    def test_exit_code_match(self):
        assert check_success([{"exit_code": 0}], 0, "") is True

    def test_exit_code_no_match(self):
        assert check_success([{"exit_code": 0}], 1, "") is False

    def test_output_contains_match(self):
        assert check_success([{"output_contains": "OK"}], 0, "everything OK") is True

    def test_output_contains_no_match(self):
        assert check_success([{"output_contains": "OK"}], 0, "nope") is False

    def test_output_not_contains_match(self):
        assert check_success([{"output_not_contains": "ERROR"}], 0, "all good") is True

    def test_output_not_contains_no_match(self):
        assert check_success([{"output_not_contains": "ERROR"}], 0, "got ERROR here") is False

    def test_and_within_rule_all_match(self):
        assert check_success(
            [{"exit_code": 0, "output_contains": "OK"}],
            0, "OK"
        ) is True

    def test_and_within_rule_partial_fail(self):
        assert check_success(
            [{"exit_code": 0, "output_contains": "OK"}],
            0, "nope"
        ) is False

    def test_or_between_rules_first_match(self):
        assert check_success(
            [{"exit_code": 0}, {"exit_code": 1}],
            0, ""
        ) is True

    def test_or_between_rules_second_match(self):
        assert check_success(
            [{"exit_code": 99}, {"exit_code": 0}],
            0, ""
        ) is True

    def test_or_between_rules_none_match(self):
        assert check_success(
            [{"exit_code": 99}, {"exit_code": 1}],
            0, ""
        ) is False

    def test_complex_multi_rule(self):
        conditions = [
            {"exit_code": 0, "output_contains": "SYNC COMPLETE"},
            {"output_contains": "ALREADY UP TO DATE"},
        ]
        assert check_success(conditions, 0, "SYNC COMPLETE") is True
        assert check_success(conditions, 1, "ALREADY UP TO DATE") is True
        assert check_success(conditions, 0, "PARTIAL") is False

    def test_exit_code_with_output_not_contains(self):
        assert check_success(
            [{"exit_code": 0, "output_not_contains": "ERROR"}],
            0, "all good"
        ) is True

    def test_only_output_not_contains(self):
        assert check_success(
            [{"output_not_contains": "ERROR"}],
            5, "some output"
        ) is True

    def test_only_output_contains(self):
        assert check_success(
            [{"output_contains": "DONE"}],
            99, "TASK DONE"
        ) is True


# ─── describe_matched_rule ───

class TestDescribeMatchedRule:
    def test_exit_code_rule(self):
        result = describe_matched_rule([{"exit_code": 0}], 0, "")
        assert "exit_code=0" in result

    def test_output_contains_rule(self):
        result = describe_matched_rule([{"output_contains": "OK"}], 0, "OK")
        assert 'output_contains "OK"' in result

    def test_no_match(self):
        assert describe_matched_rule([{"exit_code": 99}], 0, "") is None

    def test_combined_rule(self):
        result = describe_matched_rule(
            [{"exit_code": 0, "output_contains": "OK"}],
            0, "OK"
        )
        assert "exit_code=0" in result
        assert "output_contains" in result

    def test_default_empty_conditions(self):
        # Empty conditions list means no rule to match
        assert describe_matched_rule([], 0, "") is None


# ─── replace_params ───

class TestReplaceParams:
    def test_single_param(self):
        assert replace_params("hello {{name}}", {"name": "world"}) == "hello world"

    def test_multiple_params(self):
        assert replace_params("{{a}} {{b}}", {"a": "1", "b": "2"}) == "1 2"

    def test_missing_param_unchanged(self):
        assert replace_params("{{x}}", {}) == "{{x}}"

    def test_partial_match(self):
        assert replace_params("{{a}} and {{b}}", {"a": "X"}) == "X and {{b}}"

    def test_numeric_param(self):
        assert replace_params("count={{n}}", {"n": 42}) == "count=42"

    def test_no_params(self):
        assert replace_params("no placeholders", {"key": "val"}) == "no placeholders"


# ─── load_config ───

class TestLoadConfig:
    def test_basic_loading(self, simple_config):
        tasks, base_dir = load_config(simple_config)
        assert "hello" in tasks
        assert len(tasks["hello"]["steps"]) == 1

    def test_defaults_merged(self, make_config):
        path = make_config({
            "defaults": {"timeout": 600, "retry": 2, "retry_delay": 30},
            "tasks": {
                "t1": {"steps": [{"name": "s1", "command": "echo hi"}]}
            }
        })
        tasks, _ = load_config(path)
        assert tasks["t1"]["timeout"] == 600
        assert tasks["t1"]["retry"] == 2

    def test_task_overrides_defaults(self, make_config):
        path = make_config({
            "defaults": {"timeout": 600},
            "tasks": {
                "t1": {"timeout": 100, "steps": [{"name": "s1", "command": "echo"}]}
            }
        })
        tasks, _ = load_config(path)
        assert tasks["t1"]["timeout"] == 100

    def test_step_inherits_task_timeout(self, make_config):
        path = make_config({
            "tasks": {
                "t1": {"timeout": 42, "steps": [{"name": "s1", "command": "echo"}]}
            }
        })
        tasks, _ = load_config(path)
        assert tasks["t1"]["steps"][0]["timeout"] == 42

    def test_step_defaults_filled(self, simple_config):
        tasks, _ = load_config(simple_config)
        step = tasks["hello"]["steps"][0]
        assert step["retry"] == 0
        assert step["retry_delay"] == 60
        assert step["success_conditions"] == [{"exit_code": 0}]
        assert step["shell"] == "bash"

    def test_base_dir_set(self, simple_config):
        _, base_dir = load_config(simple_config)
        assert os.path.isabs(base_dir)
        assert os.path.isdir(base_dir)

    def test_relative_workdir_resolved(self, make_config):
        path = make_config({
            "tasks": {
                "t1": {"steps": [{"name": "s1", "command": "pwd", "workdir": "subdir"}]}
            }
        })
        tasks, base_dir = load_config(path)
        expected = os.path.join(base_dir, "subdir")
        assert tasks["t1"]["steps"][0]["workdir"] == expected

    def test_absolute_workdir_unchanged(self, make_config):
        path = make_config({
            "tasks": {
                "t1": {"steps": [{"name": "s1", "command": "pwd", "workdir": "/tmp"}]}
            }
        })
        tasks, _ = load_config(path)
        assert tasks["t1"]["steps"][0]["workdir"] == "/tmp"

    def test_shell_default_bash(self, make_config):
        path = make_config({
            "tasks": {"t1": {"steps": [{"name": "s1", "command": "echo"}]}}
        })
        tasks, _ = load_config(path)
        assert tasks["t1"]["shell"] == "bash"
        assert tasks["t1"]["steps"][0]["shell"] == "bash"

    def test_shell_overrides(self, make_config):
        path = make_config({
            "defaults": {"shell": "bash"},
            "tasks": {
                "t1": {
                    "shell": "powershell",
                    "steps": [{"name": "s1", "command": "echo"}]
                }
            }
        })
        tasks, _ = load_config(path)
        assert tasks["t1"]["shell"] == "powershell"
        assert tasks["t1"]["steps"][0]["shell"] == "powershell"

    def test_step_shell_overrides_task(self, make_config):
        path = make_config({
            "tasks": {
                "t1": {
                    "shell": "powershell",
                    "steps": [{"name": "s1", "command": "echo", "shell": "bash"}]
                }
            }
        })
        tasks, _ = load_config(path)
        assert tasks["t1"]["steps"][0]["shell"] == "bash"

    def test_no_config_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path.yaml")


# ─── Logger ───

class TestLogger:
    def test_creates_log_dir(self, tmp_path):
        log_dir = str(tmp_path / "new_logs")
        logger = Logger(log_dir)
        assert os.path.isdir(log_dir)

    def test_log_writes_file(self, tmp_path):
        log_dir = str(tmp_path / "logs")
        logger = Logger(log_dir)
        logger.open("test_task", "001")
        logger.log("test message")
        logger.close()
        files = os.listdir(log_dir)
        assert len(files) == 1
        content = open(os.path.join(log_dir, files[0])).read()
        assert "test message" in content


# ─── run_step ───

class TestRunStep:
    def test_success_simple(self, tmp_path):
        logger = Logger(str(tmp_path / "logs"))
        step = {"name": "s1", "command": "echo hello", "shell": "bash",
                "timeout": 10, "retry": 0, "retry_delay": 0,
                "success_conditions": [{"exit_code": 0}]}
        result = run_step(step, {}, logger)
        assert result["success"] is True
        assert result["exit_code"] == 0
        assert "hello" in result["output"]
        assert result["name"] == "s1"

    def test_failure_wrong_exit_code(self, tmp_path):
        logger = Logger(str(tmp_path / "logs"))
        step = {"name": "s1", "command": "exit 1", "shell": "bash",
                "timeout": 10, "retry": 0, "retry_delay": 0,
                "success_conditions": [{"exit_code": 0}]}
        result = run_step(step, {}, logger)
        assert result["success"] is False
        assert result["exit_code"] == 1

    def test_custom_success_condition(self, tmp_path):
        logger = Logger(str(tmp_path / "logs"))
        step = {"name": "s1", "command": "echo DONE", "shell": "bash",
                "timeout": 10, "retry": 0, "retry_delay": 0,
                "success_conditions": [{"output_contains": "DONE"}]}
        result = run_step(step, {}, logger)
        assert result["success"] is True

    def test_param_substitution(self, tmp_path):
        logger = Logger(str(tmp_path / "logs"))
        step = {"name": "s1", "command": "echo {{msg}}", "shell": "bash",
                "timeout": 10, "retry": 0, "retry_delay": 0,
                "success_conditions": [{"exit_code": 0}]}
        result = run_step(step, {"msg": "hello"}, logger)
        assert result["success"] is True
        assert "hello" in result["output"]

    def test_timeout(self, tmp_path):
        logger = Logger(str(tmp_path / "logs"))
        step = {"name": "s1", "command": "sleep 10", "shell": "bash",
                "timeout": 1, "retry": 0, "retry_delay": 0,
                "success_conditions": [{"exit_code": 0}]}
        result = run_step(step, {}, logger)
        assert result["success"] is False
        assert result["fail_reason"] == "timeout"

    def test_retry_eventually_succeeds(self, tmp_path):
        """Step fails first, succeeds on retry via a counter file"""
        logger = Logger(str(tmp_path / "logs"))
        counter = str(tmp_path / "counter.txt")
        cmd = (
            f"bash -c 'if [ ! -f {counter} ]; then echo 1 > {counter}; exit 1; "
            f"else rm {counter}; echo OK; exit 0; fi'"
        )
        step = {"name": "s1", "command": cmd, "shell": "bash",
                "timeout": 10, "retry": 2, "retry_delay": 0,
                "success_conditions": [{"exit_code": 0}]}
        result = run_step(step, {}, logger)
        assert result["success"] is True
        assert result["retry_count"] == 1

    def test_retry_exhausted(self, tmp_path):
        logger = Logger(str(tmp_path / "logs"))
        step = {"name": "s1", "command": "exit 1", "shell": "bash",
                "timeout": 10, "retry": 2, "retry_delay": 0,
                "success_conditions": [{"exit_code": 0}]}
        result = run_step(step, {}, logger)
        assert result["success"] is False
        assert result["retry_count"] == 2

    def test_workdir(self, tmp_path):
        logger = Logger(str(tmp_path / "logs"))
        step = {"name": "s1", "command": "pwd", "shell": "bash",
                "timeout": 10, "retry": 0, "retry_delay": 0,
                "workdir": str(tmp_path),
                "success_conditions": [{"exit_code": 0}]}
        result = run_step(step, {}, logger)
        assert result["success"] is True
        assert str(tmp_path) in result["output"]

    def test_duration_recorded(self, tmp_path):
        logger = Logger(str(tmp_path / "logs"))
        step = {"name": "s1", "command": "echo hi", "shell": "bash",
                "timeout": 10, "retry": 0, "retry_delay": 0,
                "success_conditions": [{"exit_code": 0}]}
        result = run_step(step, {}, logger)
        assert "duration" in result
        assert result["duration"] >= 0


# ─── run_task ───

class TestRunTask:
    def test_single_step_success(self, tmp_path):
        task = {
            "steps": [{"name": "s1", "command": "echo ok"}],
            "shell": "bash", "timeout": 10, "retry": 0, "retry_delay": 0,
        }
        result = run_task(task, "test_task", {}, str(tmp_path), log_dir=str(tmp_path / "logs"))
        assert result["success"] is True
        assert len(result["steps"]) == 1

    def test_multi_step_success(self, tmp_path):
        task = {
            "steps": [
                {"name": "s1", "command": "echo step1"},
                {"name": "s2", "command": "echo step2"},
                {"name": "s3", "command": "echo step3"},
            ],
            "shell": "bash", "timeout": 30, "retry": 0, "retry_delay": 0,
        }
        result = run_task(task, "multi", {}, str(tmp_path), log_dir=str(tmp_path / "logs"))
        assert result["success"] is True
        assert len(result["steps"]) == 3

    def test_failure_stops_later_steps(self, tmp_path):
        task = {
            "steps": [
                {"name": "s1", "command": "echo ok"},
                {"name": "s2", "command": "exit 1"},
                {"name": "s3", "command": "echo should_not_run"},
            ],
            "shell": "bash", "timeout": 10, "retry": 0, "retry_delay": 0,
        }
        result = run_task(task, "fail_chain", {}, str(tmp_path), log_dir=str(tmp_path / "logs"))
        assert result["success"] is False
        assert result["steps"][0]["success"] is True
        assert result["steps"][1]["success"] is False
        # s3 should not have run (only 2 results)
        assert len(result["steps"]) == 2

    def test_params_passed_to_steps(self, tmp_path):
        task = {
            "steps": [{"name": "s1", "command": "echo {{greeting}}"}],
            "shell": "bash", "timeout": 10, "retry": 0, "retry_delay": 0,
            "params": [{"name": "greeting", "default": "hello"}],
        }
        result = run_task(task, "params_test", {}, str(tmp_path), log_dir=str(tmp_path / "logs"))
        assert result["success"] is True
        assert "hello" in result["steps"][0]["output"]

    def test_params_override_default(self, tmp_path):
        task = {
            "steps": [{"name": "s1", "command": "echo {{val}}"}],
            "shell": "bash", "timeout": 10, "retry": 0, "retry_delay": 0,
            "params": [{"name": "val", "default": "default_val"}],
        }
        result = run_task(task, "override", {"val": "custom"},
                         str(tmp_path), log_dir=str(tmp_path / "logs"))
        assert result["success"] is True
        assert "custom" in result["steps"][0]["output"]

    def test_base_dir_injected(self, tmp_path):
        task = {
            "steps": [{"name": "s1", "command": "echo {{base_dir}}"}],
            "shell": "bash", "timeout": 10, "retry": 0, "retry_delay": 0,
        }
        result = run_task(task, "basedir", {}, "/fake/path", log_dir=str(tmp_path / "logs"))
        assert result["success"] is True
        assert "/fake/path" in result["steps"][0]["output"]

    def test_task_level_retry(self, tmp_path):
        """Task-level retry: whole chain re-runs. Use a file counter to succeed on 2nd attempt."""
        counter = str(tmp_path / "task_counter.txt")
        cmd = f"bash -c 'if [ ! -f {counter} ]; then echo 1 > {counter}; exit 1; else echo OK; exit 0; fi'"
        task = {
            "steps": [{"name": "s1", "command": cmd}],
            "shell": "bash", "timeout": 10, "retry": 1, "retry_delay": 0,
        }
        result = run_task(task, "task_retry", {}, str(tmp_path), log_dir=str(tmp_path / "logs"))
        assert result["success"] is True

    def test_task_level_retry_exhausted(self, tmp_path):
        task = {
            "steps": [{"name": "s1", "command": "exit 1"}],
            "shell": "bash", "timeout": 10, "retry": 2, "retry_delay": 0,
        }
        result = run_task(task, "exhausted", {}, str(tmp_path), log_dir=str(tmp_path / "logs"))
        assert result["success"] is False

    def test_duration_recorded(self, tmp_path):
        task = {
            "steps": [{"name": "s1", "command": "echo hi"}],
            "shell": "bash", "timeout": 10, "retry": 0, "retry_delay": 0,
        }
        result = run_task(task, "duration", {}, str(tmp_path), log_dir=str(tmp_path / "logs"))
        assert "duration" in result
        assert result["duration"] >= 0

    def test_log_file_created(self, tmp_path):
        log_dir = str(tmp_path / "run_logs")
        task = {
            "steps": [{"name": "s1", "command": "echo logged"}],
            "shell": "bash", "timeout": 10, "retry": 0, "retry_delay": 0,
        }
        run_task(task, "logged_task", {}, str(tmp_path), log_dir=log_dir)
        files = os.listdir(log_dir)
        assert len(files) == 1
        assert "logged_task" in files[0]
