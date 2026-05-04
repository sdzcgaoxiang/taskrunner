#!/usr/bin/env python3
"""TaskRunner — 单任务 YAML 驱动执行器

从 YAML 配置文件读取任务定义，按顺序执行步骤链。
每个步骤支持独立的重试策略和成功条件判断。

用法:
    python taskrunner.py <config.yaml> <task_name> [--params '{"key":"val"}']
    python taskrunner.py example.yaml daily_report
    python taskrunner.py example.yaml daily_report --params '{"date":"2024-01-01"}'
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

import yaml


# ─── 成功条件判断 ───

def check_success(conditions, exit_code, output):
    """判断步骤是否成功。

    多个 rule 之间是 OR 关系；单个 rule 内部条件是 AND 关系。
    conditions 为空时默认 exit_code == 0 即成功。
    """
    if not conditions:
        return exit_code == 0

    for rule in conditions:
        exit_ok = True
        if "exit_code" in rule:
            exit_ok = (exit_code == rule["exit_code"])

        contains_ok = True
        if "output_contains" in rule:
            contains_ok = (rule["output_contains"] in output)

        not_contains_ok = True
        if "output_not_contains" in rule:
            not_contains_ok = (rule["output_not_contains"] not in output)

        if exit_ok and contains_ok and not_contains_ok:
            return True

    return False


def describe_matched_rule(conditions, exit_code, output):
    """描述匹配到的成功规则（用于日志）"""
    for rule in conditions:
        exit_ok = rule.get("exit_code") is None or exit_code == rule["exit_code"]
        contains_ok = rule.get("output_contains") is None or rule["output_contains"] in output
        not_contains_ok = rule.get("output_not_contains") is None or rule["output_not_contains"] not in output
        if exit_ok and contains_ok and not_contains_ok:
            parts = []
            if "exit_code" in rule:
                parts.append(f"exit_code={rule['exit_code']}")
            if "output_contains" in rule:
                parts.append(f"output_contains \"{rule['output_contains']}\"")
            if "output_not_contains" in rule:
                parts.append(f"output_not_contains \"{rule['output_not_contains']}\"")
            return " AND ".join(parts) if parts else "default"
    return None


# ─── 参数替换 ───

def replace_params(command, params):
    """将命令中的 {{key}} 替换为 params[key]，未匹配的保留原样"""
    for key, value in params.items():
        command = command.replace("{{" + key + "}}", str(value))
    return command


# ─── 配置加载 ───

def load_config(config_path):
    """加载 YAML 配置，合并默认值，返回 (tasks_dict, base_dir)"""
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or "tasks" not in raw:
        print(f"[ERROR] 配置文件中未找到 tasks 定义: {config_path}")
        sys.exit(1)

    base_dir = os.path.dirname(os.path.abspath(config_path))
    defaults = raw.get("defaults", {})
    tasks = raw["tasks"]

    for task_name, task in tasks.items():
        # 合并任务级默认值
        for key in ("timeout", "retry", "retry_delay", "shell"):
            if key not in task and key in defaults:
                task[key] = defaults[key]

        if "timeout" not in task:
            task["timeout"] = 300
        if "shell" not in task:
            task["shell"] = "bash"

        # 合并步骤级默认值 + 解析相对 workdir
        for step in task.get("steps", []):
            if "timeout" not in step:
                step["timeout"] = task["timeout"]
            if "retry" not in step:
                step["retry"] = 0
            if "retry_delay" not in step:
                step["retry_delay"] = 60
            if "success_conditions" not in step:
                step["success_conditions"] = [{"exit_code": 0}]
            if "shell" not in step:
                step["shell"] = task["shell"]

            workdir = step.get("workdir")
            if workdir and not os.path.isabs(workdir):
                step["workdir"] = os.path.join(base_dir, workdir)

    return tasks, base_dir


# ─── 日志 ───

class Logger:
    """同时输出到终端和日志文件"""

    def __init__(self, log_dir):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._file = None

    def open(self, task_name, run_id):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.log_dir, f"{task_name}_{ts}_{run_id}.log")
        self._file = open(path, "a", encoding="utf-8")
        self._path = path

    def log(self, msg):
        ts = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        line = f"{ts} {msg}"
        print(line)
        if self._file:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


# ─── 步骤执行 ───

def run_step(step, params, logger):
    """执行单个步骤，处理超时、重试和成功条件判断

    返回: dict {success, exit_code, output, fail_reason, retry_count, duration, name}
    """
    command = replace_params(step["command"], params)
    timeout = step.get("timeout", 300)
    max_retry = step.get("retry", 0)
    retry_delay = step.get("retry_delay", 60)
    conditions = step.get("success_conditions", [{"exit_code": 0}])
    workdir = step.get("workdir")
    shell_type = step.get("shell", "bash")

    logger.log(f"  ▶ 步骤 [{step['name']}] 开始")
    logger.log(f"    命令: {command}")

    if shell_type == "bash":
        cmd_args = ["bash", "-c", command]
    else:
        cmd_args = ["powershell", "-Command", command]

    retry_count = 0
    step_start = time.time()

    for attempt in range(1 + max_retry):
        if attempt > 0:
            logger.log(f"    ↻ 重试第 {attempt}/{max_retry} 次，等待 {retry_delay}s ...")
            time.sleep(retry_delay)

        try:
            proc = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=workdir,
            )
            try:
                output, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                if attempt < max_retry:
                    retry_count += 1
                    continue
                logger.log(f"    ✗ 超时 ({timeout}s)")
                return {
                    "success": False, "exit_code": -1, "output": "",
                    "fail_reason": "timeout", "retry_count": retry_count,
                    "duration": round(time.time() - step_start, 1), "name": step["name"],
                }

            exit_code = proc.returncode
            success = check_success(conditions, exit_code, output)

            if success:
                matched = describe_matched_rule(conditions, exit_code, output)
                snippet = output[-200:] if len(output) > 200 else output
                if snippet.strip():
                    for line in snippet.strip().split("\n"):
                        logger.log(f"    | {line}")
                logger.log(f"    ✓ 成功 (exit_code={exit_code}, matched: {matched})")
                return {
                    "success": True, "exit_code": exit_code, "output": output,
                    "retry_count": retry_count,
                    "duration": round(time.time() - step_start, 1), "name": step["name"],
                }
            else:
                if attempt < max_retry:
                    retry_count += 1
                    continue
                snippet = output[-200:] if len(output) > 200 else output
                if snippet.strip():
                    for line in snippet.strip().split("\n"):
                        logger.log(f"    | {line}")
                logger.log(f"    ✗ 失败 (exit_code={exit_code}, 无规则匹配)")
                return {
                    "success": False, "exit_code": exit_code, "output": output,
                    "fail_reason": f"exit_code={exit_code}, no rule matched",
                    "retry_count": retry_count,
                    "duration": round(time.time() - step_start, 1), "name": step["name"],
                }

        except Exception as e:
            if attempt < max_retry:
                retry_count += 1
                continue
            logger.log(f"    ✗ 异常: {e}")
            return {
                "success": False, "exit_code": -1, "output": "",
                "fail_reason": str(e), "retry_count": retry_count,
                "duration": round(time.time() - step_start, 1), "name": step["name"],
            }


# ─── 任务执行 ───

def run_task(task, task_name, params, base_dir, log_dir="logs"):
    """执行整个任务（步骤链 A→B→C），支持任务级重试

    Args:
        task: 任务配置 dict
        task_name: 任务名
        params: 运行时参数覆盖
        base_dir: 配置文件所在目录
        log_dir: 日志目录
    """
    steps = task["steps"]
    task_retry = task.get("retry", 0)
    task_retry_delay = task.get("retry_delay", 60)

    run_id = datetime.now().strftime("%H%M%S")
    logger = Logger(log_dir)
    logger.open(task_name, run_id)

    # 解析参数：先填默认值，再覆盖运行时参数
    resolved = {}
    for p in task.get("params", []):
        resolved[p["name"]] = p.get("default", "")
    resolved.update(params)
    resolved.setdefault("base_dir", base_dir)

    start_time = time.time()
    logger.log(f"任务 [{task_name}] 开始 (共 {len(steps)} 个步骤, 任务级重试: {task_retry})")

    for task_attempt in range(1 + task_retry):
        if task_attempt > 0:
            logger.log(f"── 任务级重试 {task_attempt}/{task_retry}，等待 {task_retry_delay}s ──")
            time.sleep(task_retry_delay)

        step_results = []
        failed = False

        for step in steps:
            result = run_step(step, resolved, logger)
            step_results.append(result)

            if not result["success"]:
                failed = True
                # 后续步骤跳过
                remaining = [s for s in steps if steps.index(s) > steps.index(step)]
                for s in remaining:
                    logger.log(f"  ⊘ 步骤 [{s['name']}] 跳过（前置步骤失败）")
                break

        if not failed:
            duration = time.time() - start_time
            logger.log(f"任务 [{task_name}] 成功 ✓ ({duration:.1f}s)")
            logger.close()
            return {"success": True, "steps": step_results, "duration": duration}

    # 所有重试用尽
    duration = time.time() - start_time
    logger.log(f"任务 [{task_name}] 失败 ✗ ({duration:.1f}s, 重试 {task_retry} 次已耗尽)")
    logger.close()
    return {"success": False, "steps": step_results, "duration": duration}


# ─── CLI ───

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    config_path = args[0]
    if not os.path.exists(config_path):
        print(f"[ERROR] 配置文件不存在: {config_path}")
        sys.exit(1)

    task_name = args[1] if len(args) > 1 else None
    params = {}

    # 解析 --params
    for i, a in enumerate(args):
        if a == "--params" and i + 1 < len(args):
            params = json.loads(args[i + 1])

    if not task_name:
        # 未指定任务名，列出可用任务
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        tasks = raw.get("tasks", {})
        print(f"可用任务 ({config_path}):\n")
        for name, t in tasks.items():
            desc = t.get("description", "")
            step_count = len(t.get("steps", []))
            print(f"  {name:20s}  {step_count} 步骤  {desc}")
        print(f"\n用法: python {sys.argv[0]} {config_path} <task_name>")
        sys.exit(0)

    # 加载配置
    tasks, base_dir = load_config(config_path)

    if task_name not in tasks:
        print(f"[ERROR] 任务不存在: {task_name}")
        print(f"可用任务: {', '.join(tasks.keys())}")
        sys.exit(1)

    # 执行
    result = run_task(tasks[task_name], task_name, params, base_dir)
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
