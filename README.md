# sess

`sess` 在一个终端界面中管理当前目录的 Codex CLI 和 Claude Code 本地交互式会话。它读取两个工具已经写入硬盘的 transcript，并提供统一的列表、恢复、本地命名、分叉和永久删除命令。

`sess` 不保存模型、Provider、base URL、认证令牌或会话历史。恢复和分叉仍由原生 `codex`、`claude` 命令完成，每次启动所需的环境变量和 CLI 参数由用户重新传入。

## 运行条件

- Python 3.10 或更高版本；
- uv；
- 当前系统中已经安装所需的 `codex` 或 `claude` 命令。

Windows 和 WSL 会被视为两个独立环境。Windows 原生 `sess` 读取 Windows 用户目录中的会话，WSL 中的 `sess` 读取该发行版 Linux 用户目录中的会话，不会自动跨环境扫描。

## 安装

在项目根目录执行：

```bash
uv tool install .
```

开发期间可以不安装，直接运行：

```bash
uv run sess
```

上面的安装命令由使用者自行执行；项目不会修改 Shell 配置或 PATH。

## 查看会话

```bash
sess
sess ls
```

两个命令都会列出原生会话元数据中 `cwd` 与当前目录完全相同的交互式会话。父目录、子目录、其他项目、归档会话、`codex exec` 和 `claude -p` 会话不会进入列表。

列表按 `UPDATED` 从新到旧排列，并显示：

| 列 | 内容 |
| --- | --- |
| `AGENT` | `codex` 或 `claude` |
| `NAME` | `sess` 在当前项目中维护的名称 |
| `SESSION_ID` | 原生 session ID |
| `CREATED` | transcript 中的首次创建时间 |
| `UPDATED` | transcript 中最后一条事件的时间 |

交互界面使用以下键位：

| 按键 | 行为 |
| --- | --- |
| `j` / `k` | 选择下一条或上一条，允许跨页移动 |
| `h` / `l` | 上一页或下一页 |
| `Enter` | 使用原生 CLI 恢复选中的会话 |
| `q` | 退出 |

当标准输入或标准输出不是交互式终端时，`sess ls` 会输出制表符分隔的列表，不会自动恢复会话。

## 恢复会话

目标可以是完整 session ID，也可以是当前项目内完全匹配的 `sess` 名称：

```bash
sess resume <ID-or-name>
```

环境变量由启动 `sess` 的 Shell 设置，原生 CLI 参数放在 `--` 后。`sess` 不解析、记录或修改环境变量，也不会保存透传参数。

Claude Code 在 WSL 或 Bash 中可以这样启动：

```bash
ANTHROPIC_BASE_URL="https://provider.example/v1" \
ANTHROPIC_AUTH_TOKEN="<token>" \
ANTHROPIC_MODEL="<model>" \
sess resume <ID-or-name> -- --model "<model>"
```

PowerShell 使用自身的环境变量语法：

```powershell
$env:ANTHROPIC_BASE_URL = "https://provider.example/v1"
$env:ANTHROPIC_AUTH_TOKEN = "<token>"
$env:ANTHROPIC_MODEL = "<model>"
sess resume <ID-or-name> -- --model "<model>"
```

Codex 的一次性配置覆盖同样放在 `--` 后：

```bash
CODEX_CUSTOM_API_KEY="<token>" \
sess resume <ID-or-name> -- \
  -c model='"<model>"' \
  -c model_provider='"<provider>"' \
  -c 'model_providers.<provider>.base_url="https://provider.example/v1"' \
  -c 'model_providers.<provider>.env_key="CODEX_CUSTOM_API_KEY"'
```

`sess` 只会在原生命令中插入已经解析出的 session ID。`--` 后的参数会保持内容和顺序并全部交给 Codex 或 Claude Code，包括原生 CLI 自己用于选择会话的参数；如果这些参数与 `sess` 插入的 ID 冲突，由原生 CLI 决定结果并报告错误。

更换模型或 Provider 后能否继续某个历史会话，由当前版本的 Codex 或 Claude Code 决定。原生 CLI 返回错误时，`sess` 会保留原始退出码，不转换 transcript，也不尝试备用 Provider。

## 设置本地名称

```bash
sess rename <ID-or-name> <new-name>
```

名称只保存在当前目录的 `.agent/sess_metadata.json`，不会修改原生会话名称。该文件在第一次成功执行 `rename` 时创建；`sess` 和 `sess ls` 等只读命令不会为了读取名称而创建文件。

名称在当前项目的 Codex 与 Claude 会话之间保持唯一，并采用完全匹配。名称包含空格时，需要按照当前 Shell 的规则加引号。

## 分叉会话

```bash
sess fork <ID-or-name>
sess fork <ID-or-name> -- <native-args>...
```

Codex 使用 `codex fork`，Claude Code 使用 `claude --resume <ID> --fork-session`。原生 CLI 会创建新的 session ID 并立即进入该会话，原会话保持不变。新会话默认没有 `sess` 名称，退出后可以通过 `sess ls` 找到它并执行 `sess rename`。

## 永久删除会话

```bash
sess del <ID-or-name>
sess del <full-session-id> --yes
```

`del` 会从硬盘永久删除会话，不执行归档，也不进入回收站。默认流程会列出待删除对象并要求确认。`--yes` 只能与完整 session ID 搭配，不能使用名称跳过确认。

Codex 会话通过原生 `codex delete --force` 删除。Claude Code 当前没有单会话删除命令，因此 `sess` 会清理该 ID 对应的 transcript、tool results、file history、session environment、tasks、prompt history 行、遥测或调试 sidecar，以及项目状态中的 `lastSession` 字段。项目级信任设置、MCP 配置和其他 session 的记录不会被删除。

如果 Claude Code 会话仍由一个存活进程使用，`sess del` 会拒绝删除。任何删除步骤失败时，命令都会返回非零退出码，并列出已经删除和仍然存在的对象；只有原生数据全部删除成功后，才会移除 `.agent/sess_metadata.json` 中的名称。

## 原生数据位置

Codex 默认从 `CODEX_HOME` 读取状态；没有设置时使用 `~/.codex`。交互式 transcript 通常位于 `~/.codex/sessions/`。

Claude Code 默认将 transcript 保存为：

```text
~/.claude/projects/<encoded-project>/<session-id>.jsonl
```

设置 `CLAUDE_CONFIG_DIR` 后，`sess` 会从该目录读取 Claude Code 数据。两个 Provider 的内部 JSONL 格式都可能随原生 CLI 版本变化，相关解析代码分别封装在 `CodexProvider` 和 `ClaudeProvider` 中。

## 开发与测试

```bash
uv run pytest
```

测试使用脱敏的临时 transcript，覆盖当前目录过滤、交互式会话识别、名称元数据、参数透传、TUI 分页、原生命令构造和 Claude Code 单会话删除。测试不会删除用户现有的 Codex 或 Claude Code 会话。

更详细的产品边界和实现依据保存在 `.agent/work/` 下的设计文档中。
