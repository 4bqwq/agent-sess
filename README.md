# sess

`sess` 在一个终端界面中管理当前目录的 Codex CLI 和 Claude Code 本地交互式会话。它读取两个工具已经写入硬盘的 transcript，并提供统一的列表、原始文件定位、最终问答查看、轨迹输出、恢复、本地命名、分叉和永久删除命令。

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

已经通过 `uv tool install .` 安装旧版本时，可以在项目根目录执行：

```bash
uv tool uninstall sess-cli
uv tool install .
```

`sess-cli` 是 Python 包名，安装后提供的终端命令仍然是 `sess`。

## 查看会话

```bash
sess
sess ls
sess ls --page 2
sess ls --all
```

`sess` 打开交互式终端界面。`sess ls` 输出按实际显示宽度对齐的空格表格，不进入备用屏幕；空名称、中文名称、完整 session ID 和时间列保持对齐。字段不会被截断。`sess ls` 默认输出第 1 页，每页 20 条；`--page N` 选择页码，`--all` 输出全部会话。

两个入口都只列出原生会话元数据中 `cwd` 指向当前实际目录的交互式会话。WSL 的 Windows 挂载目录即使大小写写法不同，只要文件系统确认它们是同一个目录，也会匹配；父目录、子目录、其他项目、归档会话、`codex exec` 和 `claude -p` 会话不会进入列表。

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

当标准输入或标准输出不是交互式终端时，`sess` 保留普通文本输出的兼容行为，不会自动恢复会话。

## 获取原始会话文件路径

目标可以使用完整 session ID、当前项目内完全匹配的 `sess` 名称，或至少 8 个字符的唯一 session ID 前缀：

```bash
sess path <ID-or-name-or-prefix>
```

命令只向标准输出写入原始 JSONL transcript 的绝对路径，不添加标签或说明文字，便于直接复制或交给其他程序。错误仍写入标准错误。

## 查看最终问答

```bash
sess show <ID-or-name-or-prefix>
sess show <ID-or-name-or-prefix> -n 3
```

`sess show` 输出 UTF-8 Markdown。默认显示最近一轮，`-n N` 或 `--turns N` 显示最近 N 轮；每轮只保留最终用户输入和最终 Agent 输出，不显示进度消息、工具调用、工具结果、reasoning 或控制消息。

Codex transcript 中优先使用明确标记为 `final_answer` 的输出；旧 transcript 没有阶段标记时使用该轮最后一条没有被后续工具调用取代的 assistant 文本。Claude Code 使用该轮最后一条不包含工具调用的普通 assistant 文本。尚未产生最终输出的轮次会显示“尚无最终输出”。

## 输出会话轨迹

目标可以使用完整 session ID、当前项目内完全匹配的 `sess` 名称，或至少 8 个字符的唯一 session ID 前缀：

```bash
sess print <ID-or-name-or-prefix>
sess print <ID-or-name-or-prefix> --all
sess print 019fee45 > transcript.md
```

默认只输出当前会话最近一轮用户请求及其后续执行轨迹；`--all` 输出整个会话。正文是按时间顺序排列的 UTF-8 Markdown 源码，包括用户消息、可见的助手消息、公开的 reasoning summary、工具调用的完整参数、工具返回的完整结果和错误。

系统提示、developer 指令、token 统计、加密 reasoning 和 Claude Code 的 thinking 内容不会进入输出。子 Agent 只按主 transcript 中已有的工具调用和结果显示，不递归读取子 Agent 自己的 transcript。

Markdown 只写入标准输出。读取活跃会话时，`sess print` 会对命令启动时已经存在的完整 JSONL 记录做一次快照；如果文件末尾恰好有一行仍在写入，该行会被跳过，说明写入标准错误，因此重定向文件仍然只包含 Markdown。

`print` 保留详细执行轨迹；只需要人工查看最终问答时使用 `show`，需要原生完整记录时使用 `path` 获取 JSONL 文件。

## Codex 启动权限

`sess new`、`sess resume`、`sess fork`、`sess new --fork`，以及在交互列表中恢复 Codex 会话时，默认使用 `--sandbox danger-full-access --ask-for-approval never`：允许完整访问，不请求执行审批。

默认值只影响本次由 `sess` 启动的 Codex，不修改全局 Codex 配置，不影响直接执行 `codex` 或 Claude Code。需要本次使用其他权限时，在 `--` 后明确指定：

```bash
sess new experiment -- --sandbox workspace-write --ask-for-approval on-request
sess resume <ID-or-name-or-prefix> -- -s read-only -a on-request
```

只指定一项时，另一项仍使用上述默认值。`-c sandbox_mode=...`、`-c approval_policy=...` 也会覆盖对应默认值；显式指定原生权限组合开关时，权限由该开关决定。用户传入的参数仍保持原内容和顺序，`sess` 不会额外添加与这些设置冲突的默认参数。

## 启动新会话

```bash
sess new
sess new <name>
sess new --fork <ID-or-name-or-prefix>
sess new <name> --fork <ID-or-name-or-prefix>
```

不带 `--fork` 时，`sess new` 启动一个新的 Codex 会话；`name` 可以省略。带 `--fork` 时，它与 `sess fork` 使用同一套分叉流程：省略 `name` 会生成默认的 `<parent-prefix>-fork<N>` 名称，提供 `name` 则只使用该名称，并且不占用自动编号。

Codex 在空白聊天界面中尚未写入 JSONL，因此 `sess` 会等到首次 prompt 产生 transcript 后再写入本地名称。命名一旦完成便不再等待原生会话退出，之后使用 Ctrl+C 不会丢失名称。未输入 prompt 就退出时，Codex 不会保存该 session，`sess` 也不会留下名称。

同一项目一次只允许一个尚未生成 transcript 的 `sess new` 或 `sess fork`。首个会话生成 JSONL 后，这项限制立即解除，即使该会话仍在运行，也可以从另一个终端再次启动。等待期间执行第二个相关命令会直接报错；同时直接启动原生 `codex` 或 `claude` 仍可能造成新会话识别歧义，因此应先让当前会话提交首次 prompt 或退出。

这项协调使用 `.agent/.sess-launch.lock`。文件本身不保存名称或会话内容；进程退出或崩溃时，操作系统会释放文件锁，因此不需要手工清理。

原生 CLI 参数仍放在 `--` 后，例如：

```bash
sess new experiment -- --model gpt-6-astra
sess new review --fork 01a04720 -- --model gpt-6-astra
```

名称只写入当前项目的 `.agent/sess_metadata.json`，不会读取或修改 Codex 自带的会话名称。

## 恢复会话

目标可以是完整 session ID、当前项目内完全匹配的 `sess` 名称，或至少 8 个字符的 session ID 前缀：

```bash
sess resume <ID-or-name-or-prefix>
```

ID 前缀按大小写不敏感的方式匹配，只在唯一对应一条当前目录会话时生效；如果存在多个候选，`sess` 会拒绝执行并列出完整候选 ID。解析顺序为完整 ID、完全匹配的名称、唯一 ID 前缀。

环境变量由启动 `sess` 的 Shell 设置，原生 CLI 参数放在 `--` 后。`sess new`、`sess resume` 和 `sess fork` 都支持这种参数透传；`sess` 不解析、记录或修改环境变量，也不会保存透传参数。

Claude Code 在 WSL 或 Bash 中可以这样启动：

```bash
ANTHROPIC_BASE_URL="https://provider.example/v1" \
ANTHROPIC_AUTH_TOKEN="<token>" \
ANTHROPIC_MODEL="<model>" \
sess resume <ID-or-name-or-prefix> -- --model "<model>"
```

PowerShell 使用自身的环境变量语法：

```powershell
$env:ANTHROPIC_BASE_URL = "https://provider.example/v1"
$env:ANTHROPIC_AUTH_TOKEN = "<token>"
$env:ANTHROPIC_MODEL = "<model>"
sess resume <ID-or-name-or-prefix> -- --model "<model>"
```

Codex 的一次性配置覆盖同样放在 `--` 后：

```bash
CODEX_CUSTOM_API_KEY="<token>" \
sess resume <ID-or-name-or-prefix> -- \
  -c model='"<model>"' \
  -c model_provider='"<provider>"' \
  -c 'model_providers.<provider>.base_url="https://provider.example/v1"' \
  -c 'model_providers.<provider>.env_key="CODEX_CUSTOM_API_KEY"'
```

恢复或分叉时，`sess` 会在原生命令中插入已经解析出的 session ID，并为 Codex 补上未被显式覆盖的默认权限参数。`--` 后的参数会保持内容和顺序并全部交给 Codex 或 Claude Code，包括原生 CLI 自己用于选择会话的参数；如果这些参数与 `sess` 插入的 ID 冲突，由原生 CLI 决定结果并报告错误。

更换模型或 Provider 后能否继续某个历史会话，由当前版本的 Codex 或 Claude Code 决定。原生 CLI 返回错误时，`sess` 会保留原始退出码，不转换 transcript，也不尝试备用 Provider。

## 设置本地名称

```bash
sess rename <ID-or-name-or-prefix> <new-name>
```

名称只保存在当前目录的 `.agent/sess_metadata.json`，不会修改原生会话名称。该文件在第一次成功命名时创建，包括 `rename`、带名称的 `sess new` 和自动命名的 fork；`sess` 和 `sess ls` 等只读命令不会为了读取名称而创建文件。

名称在当前项目的 Codex 与 Claude 会话之间保持唯一，并采用完全匹配。名称包含空格时，需要按照当前 Shell 的规则加引号。

## 分叉会话

```bash
sess fork <ID-or-name-or-prefix>
sess fork <ID-or-name-or-prefix> -- <native-args>...
sess new --fork <ID-or-name-or-prefix>
sess new <name> --fork <ID-or-name-or-prefix>
```

`sess fork <target>` 等价于 `sess new --fork <target>`。Codex 使用 `codex fork`，Claude Code 使用 `claude --resume <ID> --fork-session`。原生 CLI 会创建新的 session ID 并立即进入该会话，原会话保持不变。

新会话的 JSONL 一出现，`sess fork` 就会写入默认本地名称，不再等待原生 CLI 正常退出：

```text
<直接父会话的最短唯一 ID 前缀，至少 8 位>-fork<N>
```

例如同一父会话的前两个直接 fork 可以命名为 `01a04720-fork1` 和 `01a04720-fork2`。编号按父会话分别递增，删除或重命名旧 fork 后也不会复用。fork 的 fork 使用其直接父会话的 ID 前缀。`sess new experiment --fork <target>` 只写入 `experiment`；以后第一次自动命名仍从 `fork1` 开始。默认名称只保存在当前项目的 `.agent/sess_metadata.json`，可以继续用 `sess rename` 修改；直接执行原生 Codex 或 Claude Code fork 命令不会触发自动命名。

如果原生 fork 已经完成，但 `sess` 无法唯一识别新会话或写入名称，命令会明确报错并保留原生会话，不会把名称写到猜测出的候选会话上。

## 永久删除会话

```bash
sess del <ID-or-name-or-prefix>
sess del <full-session-id> --yes
```

`del` 会从硬盘永久删除会话，不执行归档，也不进入回收站。默认流程会列出待删除对象并要求确认，此时目标可以使用唯一 ID 前缀。`--yes` 只能与完整 session ID 搭配，不能使用名称或 ID 前缀跳过确认。

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

测试使用脱敏的临时 transcript，覆盖实际目录身份判断、交互式会话识别、唯一 ID 前缀、名称元数据、新会话监听与启动锁、参数透传、TUI 与静态列表分页、Codex 和 Claude Code 轨迹解析、Markdown 输出、原生命令构造和 Claude Code 单会话删除。测试不会删除用户现有的 Codex 或 Claude Code 会话。

更详细的产品边界和实现依据保存在 `.agent/work/` 下的设计文档中。
