# TaskRunner — 单任务 YAML 驱动执行器

从 YAML 配置读取任务，按顺序执行步骤链。每个步骤支持独立的重试策略和成功条件判断。

## 快速开始

```bash
# 查看可用任务
python taskrunner.py example.yaml

# 执行指定任务
python taskrunner.py example.yaml daily_report

# 带参数执行
python taskrunner.py example.yaml daily_report --params '{"date":"2024-01-01"}'
```

## YAML 配置格式

```yaml
defaults:
  timeout: 300        # 默认超时秒数
  retry: 0            # 默认任务级重试次数
  retry_delay: 60     # 重试间隔秒数
  shell: bash         # 默认 shell (bash / powershell)

tasks:
  my_task:
    description: "任务描述"
    timeout: 600       # 覆盖默认超时
    retry: 1           # 任务级重试（整体重跑）
    retry_delay: 30

    params:
      - name: date
        default: "today"

    steps:
      - name: step1
        command: "echo hello {{date}}"
        timeout: 60
        retry: 2           # 步骤级重试（独立于任务级）
        retry_delay: 10

      - name: step2
        command: "python run.py"
        workdir: "./scripts"    # 相对路径基于 yaml 文件位置
        shell: bash
        success_conditions:
          - exit_code: 0
          - exit_code: 1
            output_contains: "OK"
```

## 核心功能

| 功能 | 说明 |
|------|------|
| 步骤链执行 | A→B→C 顺序执行，某步失败则跳过后续 |
| 步骤级重试 | 每个步骤独立配置 retry 次数和 retry_delay |
| 任务级重试 | 整个任务链重跑（从第一步重新开始） |
| 自定义成功条件 | exit_code + output_contains + output_not_contains 组合判断 |
| 参数替换 | 命令中用 `{{key}}`，通过 `--params` 或 config 默认值传入 |
| 日志记录 | 每次运行生成 logs/ 下的日志文件 |
| shell 选择 | 步骤级 shell: bash 或 powershell |
| 退出码 | 成功退出 0，失败退出 1（可直接用于 CI/CD 判断） |

## 成功条件规则

`success_conditions` 是一个规则列表：

- **多个规则之间是 OR 关系** — 任一规则匹配即成功
- **单个规则内部是 AND 关系** — 所有条件都满足才算匹配
- **不指定 success_conditions** — 默认 `exit_code == 0`

条件类型：
- `exit_code: 0` — 退出码等于指定值
- `output_contains: "SUCCESS"` — 标准输出包含该字符串
- `output_not_contains: "ERROR"` — 标准输出不包含该字符串

## 依赖

仅 PyYAML（Python 标准库以外的唯一依赖）：

```bash
pip install pyyaml
```

## 文件结构

```
taskrunner/
├── taskrunner.py     # 单文件执行器（唯一代码文件）
├── example.yaml      # 示例配置
├── README.md         # 本文件
└── logs/             # 运行日志（自动创建）
```
