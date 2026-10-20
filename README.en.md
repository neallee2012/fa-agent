# 🤖 FA Agent 🤖

<!-- Maintainers: README.md (Traditional Chinese) is the source. Change both files in the same PR. -->

[繁體中文](README.md) | English

> A hosted team of AI assistants on Microsoft Foundry. Each assistant has its own identity, private memory, and a Linux VM with a desktop and a browser.

![FA Agent demo: assistant A opens a browser on its own desktop to find a route, then hands a weather check to assistant B](docs/assets/fa-demo.gif)

<sub>The workbench UI is in Traditional Chinese.</sub>

## What is FA Agent?

FA (Foundry Agents) is a hosted AI assistant platform for one organization. A Team belongs to one user, and a Team can have several assistants. Each assistant has its own role, memory, and tools. The data model can hold many Teams, but today one deployment sets up one Team with one Team owner. Running several Teams independently is designed and partly implemented, not deployed yet.

Each assistant gets its own VM on [Microsoft Foundry Hosted Agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents). A pinned version of the [GitHub Copilot SDK](https://github.com/github/copilot-sdk) runs each turn inside that VM. It reads your message, calls tools, drives Chromium on the desktop, and writes the result back to the conversation.

The platform reclaims an idle VM after a timeout. FA sets 3600 seconds by default, and you can change it in the environment settings. Conversation history and checkpoints live in the Foundry State store. When the next message arrives, a new VM resumes the conversation from the last valid checkpoint. Work files on the VM do not move to the new VM yet.

**How it differs from a plain Foundry agent or a self-hosted OpenClaw.** When you build an agent directly in Foundry, you wire up the desktop, private memory, hand-offs, and permission boundaries yourself; FA gives each assistant its own Hosted VM (with a desktop and Chromium), private memory, approval cards, and hand-offs between assistants in the same Team. A self-hosted OpenClaw runs on a machine you manage; FA runs entirely on managed services in your Azure subscription, with the Team as the trust boundary: the entry point checks the Team owner with Entra, and data relay and hand-offs stay inside one Team. After a VM is reclaimed or upgraded, the conversation resumes from the last valid checkpoint instead of silently starting a new one. "10 times faster than OpenClaw" is the project's goal; it has not been measured yet.

You talk to the assistants in a web workbench. The workbench signs you in with Entra ID, and only the Team owner can get in.

```text
You: Give me a walking route map from the Microsoft Taipei office to Daan Forest Park
A  : (opens Google Maps in Chromium on its desktop, finds the route, takes a screenshot) replies with distance, time, and the route screenshot
You: Ask B for the current weather in Xinyi District, I want to exercise outside
A  : (hands the task to B and ends this turn)
B  : (searches the weather and reports back to A)
A  : (wakes on B's report and sums up the weather and exercise advice in the same conversation)
```

**What you need.** An Azure subscription where you can create resources and role assignments (Owner is simplest). A region that has Foundry Hosted Agents, and quota for the models you want. An Entra tenant where you can register an app. Delegated permission grants for the Team owner are also needed: the hook creates them when you hold Cloud Application Administrator or higher; otherwise it writes files for an admin to apply. You do not need to be an admin to deploy. An Entra account for the Team owner. Assistants use the models you deploy in Foundry (BYOK), so you do not need a GitHub account or a GitHub Copilot subscription. The full requirements and steps are in "Deploy to your Azure" below.

**Cost.** Each assistant's VM costs about US$0.25 per hour while it runs, and nothing after it is reclaimed. The always-on workbench costs about US$5.83 per month, and the API and other resources cost extra. See "Cost reference" below for the author's estimates. Your bill is what counts.

## Key features

- **Its own desktop and browser.** Each VM has an Xvfb desktop, [Playwright MCP](https://github.com/microsoft/playwright-mcp), and a pinned Chromium. On the same VM, tabs and cookies persist across turns. In the workbench's live desktop panel (「即時桌面」) you can watch the assistant over noVNC and take over the mouse and keyboard.
- **Assistants hand work to each other.** Assistant A calls `send_agent_message` to give a task to B in the same Team, then ends its turn. B queues the task, and when B finishes it wakes A, which replies to you in the original conversation. The relay only checks that both assistants belong to the same Team.
- **Private memory.** Each assistant's memory lives in the Foundry Memory Store and is available across conversations. A separate Team note is shared by the assistants in the same Team. It stores only what a human said verbatim in a conversation. Assistant replies, tool results, and web content never go into it.
- **Human approval cards.** Actions that need approval, such as running a shell command or calling a Team MCP server, show a card in the conversation. "Allow this time" (「允許這次」) approves one call only. You set the rules in the Agent Team settings (「Agent Team 設定」).
- **Web search and Team tools.** [Foundry Toolbox](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox) connects WebIQ search and your own MCP servers. FA also ships a read-only Feed reader MCP that lets assistants read your private RSS or Atom feeds. The feed URL stays in the Foundry connection settings, and the assistant never sees it.
- **Schedules.** Ask an assistant in the conversation to do something on a schedule. The assistant proposes a schedule, and after you approve it, the API's clock triggers it on time.
- **Switchable models.** The menu next to the input box switches the model. The list of choices is in [`model-catalog.json`](src/fa-shared-context-poc/model-catalog.json). In an environment built with `azd up` you can also switch in the middle of a conversation (the turn that switches is slower once); to turn it off, see [deploy/azd](deploy/azd/README.md#model-switch).
- **Add assistants from the workbench.** Press "+" (「＋」) and describe the role to create a new assistant. You can only add it to your own Team.

All of these features have run with real models on the author's test environment (dev) or staging environment (stage), except schedules. Schedules are turned on in stage, but no human has created the first one yet. The full acceptance record for maintainers is in [CURRENT-STATE](docs/status/CURRENT-STATE.md) and [progress](docs/progress.md). Both are in Traditional Chinese and use many internal code names.

## Where it runs

Every resource lives in your own Azure subscription. Turns and model calls run inside each assistant's VM. The API only handles the entry point (identity check and dedupe), relays Team data, passes hand-offs between VMs, records usage, keeps the organization's connector secrets, adds assistants, writes the SOPs, skills, and roles the Team owner approved, and triggers approved schedules on time.

```text
Browser
  │  Entra ID sign-in (Container Apps Easy Auth)
  ▼
Workbench ───────► FA API ─────────────────► Foundry Hosted Agents
Container App      Container App              one VM per assistant
                   · identity check, dedupe   · Copilot SDK runs turns
                   · Team data relay          · Xvfb desktop + Chromium
                   · cross-VM hand-offs       │
                   · usage ledger, schedule   ├─► Foundry model deployments
                     clock                    ├─► Foundry State store (checkpoints)
                   │                          ├─► Foundry Memory Store (memory)
                   ▼                          └─► Foundry Toolbox (WebIQ, MCP)
              Storage Table / Blob
```

| Component | Azure service | Source |
|---|---|---|
| Assistant VM | Foundry Hosted Agents | [`Dockerfile.behavior-assistant-desktop`](src/fa-shared-context-poc/Dockerfile.behavior-assistant-desktop) |
| API | Azure Container Apps (at least 1 replica) | [`Dockerfile.behavior-api`](src/fa-shared-context-poc/Dockerfile.behavior-api), [`behavior-api.bicep`](infra/shared-context/behavior-api.bicep) |
| Workbench | Azure Container Apps + Easy Auth | [`web_test_console/`](src/fa-shared-context-poc/web_test_console). Created by `azd up`'s postdeploy hook ([deploy/azd](deploy/azd/README.md)) |
| Feed reader MCP | Azure Container Apps | [`feed_mcp/`](src/fa-shared-context-poc/feed_mcp), [`feed-mcp.bicep`](infra/feed-mcp/feed-mcp.bicep) |
| Work state and results | Storage Table, Blob | [`foundation.bicep`](infra/shared-context/foundation.bicep) |
| Realtime push | Web PubSub (Entra only) | [`realtime.bicep`](infra/shared-context/realtime.bicep) |
| Monitoring | Log Analytics, Application Insights | [`stage-monitoring.bicep`](infra/shared-context/stage-monitoring.bicep) |
| Images | Azure Container Registry | [`foundation.bicep`](infra/shared-context/foundation.bicep) |

The main pinned versions are `github-copilot-sdk==1.0.13`, Copilot runtime 1.0.83 (the executable the SDK starts inside the VM, downloaded and pinned when the image is built), `azure-ai-agentserver-core==2.2.0`, and `azure-ai-projects==2.3.0`. Versions for the assistant VM are in [`requirements-hosted.txt`](src/fa-shared-context-poc/requirements-hosted.txt), and API versions are in [`requirements.txt`](src/fa-shared-context-poc/requirements.txt).

## Deploy to your Azure

Use `azd up` to deploy to your own subscription. Bicep creates every resource, and the hooks call the same scripts as the author's stage CI. Running `azd deploy` later is the same upgrade flow. The full guide is in [deploy/azd/README.md](deploy/azd/README.md), and the design trade-offs are in [docs/azd-new-environment.md](docs/azd-new-environment.md), both in Traditional Chinese.

**Prerequisites, by who does what.** See [prerequisites](deploy/azd/README.md#prerequisites) for the details of each item.

- **You (the person who deploys):** azd 1.32.0 or later, the Azure CLI, Python 3.11 or later, git, and a running Docker. On Windows you also need PowerShell 7. Sign in to `az` and `azd` with the same user account. You should be the Team owner. If you are not, the owner signs in and takes over the second half.
- **Subscription Owner:** you need Owner on the subscription, or Contributor plus User Access Administrator. The subscription needs 9 resource providers and 1 feature registered.
- **Entra permissions:** you must be allowed to register apps. The 3 delegated grants for the Team owner only are created by the hook when you can grant them (Cloud Application Administrator, Application Administrator, or Global Administrator). When you cannot, the hook writes the requests to admin-consent files (`.azure/<env>/fa/admin-consent/`), and you continue after an administrator applies them.

You do not need Microsoft Teams, Work IQ or GitHub. Connectors are off when `azd up` finishes. To add tools later, see [Extending](#extending).

```powershell
git clone https://github.com/neallee2012/fa-agent.git; cd fa-agent   # use git clone, not a ZIP download: the deploy reads the git commit (git rev-parse HEAD)
$env:AZURE_CONFIG_DIR = "$HOME\.azure-fa"   # separate Azure CLI config folder, so you do not use the wrong subscription
$env:PYTHONUTF8 = "1"
az login --tenant <your-tenant-id>
azd auth login --tenant-id <your-tenant-id>
azd env new <name> --subscription <subscription-id> --location <region>
azd env set FA_TEAM_OWNER_OID <owner-object-id>   # the Team owner's object ID (how to find it: see below); skip only when you deploy for yourself
python .\deploy\azd\fa_azd.py prereq     # read-only: lists what you have, what is missing, and who to ask
azd up
```

`<name>` is the environment name: 2 to 20 lowercase letters, digits or hyphens, used as the prefix of resource names. For `<region>`, pick a region that has Foundry Hosted Agents; the list is in the [official docs](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#region-availability). If you have not decided yet, drop `--location` and the `prereq` line, and `azd up` asks you with a menu.

If you do not know the subscription ID or tenant ID, run `az account list --query "[].{name:name, id:id, tenant:tenantId}" -o table` after `az login`. The subscription is fixed per azd environment by `azd env new --subscription`; the az CLI default subscription is not used, and the hooks stop if az and azd are signed in to different tenants.

The first `azd up` takes about 1.5 to 2 hours; later runs take about 15 minutes. If `azd up` stops at once with "interactive prompts disabled … missing location" (for example in a terminal tab inside the Claude desktop app), run `$env:AZD_NON_INTERACTIVE = 'false'` first. On Windows, if the account picker of `az login` does not offer the account you need, use `az login --tenant <your-tenant-id> --use-device-code`. Details are in [First deployment](deploy/azd/README.md#first-deploy).

Get the owner's object ID with `az ad user show --id <owner UPN> --query id -o tsv` (the owner can also run `az ad signed-in-user show --query id -o tsv` after signing in). Without `FA_TEAM_OWNER_OID`, the person who deploys becomes the Team owner, anyone else cannot sign in to the workbench, and only `azd down --purge` lets you start over. So preprovision prints "Team owner = the signed-in deployer" and, in a terminal, asks you to type `y`; any other answer stops it and nothing is written. `azd up` runs the same check again at the start, and stops before it creates any billable resource if something is missing.

**Where it asks you.**

- **Region and models.** azd's own menu asks for the region and lists only regions with Foundry Hosted Agents. If you set `--location` above, it does not ask again. The author uses `westus3`. For models, the hook lists whether this region offers each one and whether quota is enough. You type the numbers, then pick the default model for new conversations. No model is required.
- **Plan hash.** Before the first install writes to the table, the terminal shows a plan hash. It writes only after the Team owner types the first 12 characters. Anything else stops it and nothing is written. Without a terminal (the code checks `sys.stdin.isatty()`, not azd's `--no-prompt`) it stops.
- **WebIQ key (optional).** It asks only after `azd env set FA_ENABLE_WEBIQ true`. The key is a hidden prompt. It is not written to a file or to the azd env.

**What still needs a human.** At the end, the hook picks the new-conversation template for both assistants as the Team owner: the Responses lane, with every capability the server offers. You can change it later on the assistant's ⚙ settings page in the workbench. What remains is for the owner to sign in at the workbench URL the hook prints (its messages call the workbench "Portal"; later, `azd env get-value FA_CONSOLE_URL` also shows it), open a new conversation, and send a short message. It passes only when the receipt is `completed` with `failure_code=null`. The steps are in [The first 10 minutes](#first-10-minutes) below. If the person who deploys is not the owner, the owner signs in and runs `azd hooks run postdeploy` for the first session preparation and the template choice. The owner's takeover starts at the plan-hash approval. A rerun redoes the role assignments and Bicep, so the owner also needs subscription Owner (or Contributor plus User Access Administrator) and Foundry User, and must run it on the same machine with the same `.azure/<env>/fa` folder.

**When it stops or you need to troubleshoot.** Every write records an intent first. Read back as the screen tells you, then continue with `azd hooks run postprovision` or `azd hooks run postdeploy`. Do not delete the intents and start over. The step-by-step troubleshooting reference is the 17 gates of the [new environment standard flow](deploy/README.md#new-environment-standard-flow), in Traditional Chinese, and both paths call the same scripts. `azd down --purge` deletes the tables, backups, and memory with everything else, and cannot be undone. While the backup lock is still there and `FA_CONFIRM_DELETE` is not set, it first asks you to type the environment name; azd also asks its own `y/N`. It takes about 30 minutes.

**Current limits.** The author ran `azd up` from scratch once in their own subscription and used the workbench for real. The region and model choice at the start, added afterwards, also ran successfully in eastus2 (one model chosen, deployments and settings read back correctly), but the workbench check for that run has not been done yet.

**Cost reference.** These are estimates from the author's environment. Your bill is what counts. Each assistant's VM costs about US$0.25 per hour while it runs (2 vCPU, 4 GiB), and nothing after it is reclaimed. The always-on workbench costs about US$5.83 per month. Memory is on by default, and the Memory Store (preview) is billed by usage on top. The author's stage environment was estimated at US$70 to 190 per month. Since 2026-10-07 the workbench and the feed app stay on, which adds about US$11.66 per month.

<a id="first-10-minutes"></a>
## The first 10 minutes

After the deploy finishes, the Team owner walks through this once to check that the whole path works. The UI labels are in Traditional Chinese, shown in 「」.

1. Run `azd env get-value FA_CONSOLE_URL` to get the workbench URL, open it in a browser, and sign in with the Team owner's Entra account.
2. In the assistant list on the left, pick `fa-<name>-a` (assistant A).
3. Press "⋯" above the conversation → 「新對話 · New session」.
4. In the message box (「傳送訊息…」), type a message that needs the shell, for example "use the shell to run `date` and tell me the result", and press Send (「送出」).
5. If an approval card appears in the conversation, press "Allow this time" (「允許這次」); the assistant finishes and replies. A freshly deployed Team has no approval rules, and Auto Review lets a read-only command you asked for run without asking, so a command like `date` may show no card; the assistant simply replying is normal and does not mean the deploy is broken. To see a card, open the assistant's ⚙ settings page → 「批准規則」 → 「新增一條規則」, write "shell commands" as the rule, choose 「先問我」 under 「遇到符合的動作時」, press 「確認並新增規則」, and send the message again.
6. Press "⋯" → support details (「支援明細」) and expand 「識別碼、歷史與回條」. It passes when `native_receipt` has `status` `completed` and `failure_code` `null`. Check this whether or not a card appeared; a reply with a `status` other than `completed` does not pass.

## Quick start

You can run offline tests and view the workbench UI locally without Azure.

```powershell
# Create a virtual environment and install test dependencies
python -m venv .\src\fa-shared-context-poc\.venv
.\src\fa-shared-context-poc\.venv\Scripts\python.exe -m pip install -r .\tests\shared_context\requirements.txt -r .\src\fa-shared-context-poc\requirements-behavior.txt

# Run one offline test file, no Azure credentials needed
.\src\fa-shared-context-poc\.venv\Scripts\python.exe -m pytest .\tests\shared_context\test_native_toolbox.py -q -p no:cacheprovider --basetemp "C:\t\fa-$(Get-Random)"

# Preview the workbench locally at http://127.0.0.1:8765/
cd .\src\fa-shared-context-poc
.\.venv\Scripts\python.exe -B -m web_test_console preview --port 8765
```

Preview mode gets no token and does not connect to a backend. Every action is disabled, so it only shows the UI. `--basetemp` points to a short path that changes on every run. The path is short to stay under the Windows path length limit, and it changes because pytest clears that folder. The full `tests\shared_context` suite takes over an hour on the author's Windows machine. See [local commands](src/fa-shared-context-poc/README.md#本機命令) for how to run it.

## Tune your assistants

| What to change | Where to change it |
|---|---|
| The assistant's role | Fill it in when you press "+" in the workbench, up to 2048 bytes |
| Capabilities, skills, MCP | ⚙ on the assistant row → apply to new conversations (「套用到新對話」). Affects new conversations only |
| Team skills | ⚙ at the bottom left, Agent Team settings → Team Skills. A draft takes effect only after the Team owner approves that version |
| Approval rules | Agent Team settings → approval rules (「批准規則」) |
| Team MCP servers | Agent Team settings → Team MCP, written to the official Toolbox |
| Default and available models | `FA_INSTRUCTION_DEFAULT_MODEL` in the environment settings, and [`model-catalog.json`](src/fa-shared-context-poc/model-catalog.json) |
| System prompt version | `conversation_defaults.prompt_profile` in the environment settings. The full text is in [`behavior_instruction_prompt.py`](src/fa-shared-context-poc/shared_context/behavior_instruction_prompt.py) |
| Tool definitions | [`tool_catalog/`](src/fa-shared-context-poc/tool_catalog) |
| Environment switches | The environment profile JSON, chosen with `--config` or `FA_DEPLOY_CONFIG`. Environment variables of the assistant VM must ship with a new Hosted Agents version |

<a id="extending"></a>
## Extending: MCP, skills and connectors

The full steps and code locations are in [docs/extending.md](docs/extending.md), in Traditional Chinese.

- **MCP servers.** For a remote MCP server that authenticates with a key in a header, the Team owner adds it under Agent Team settings → Team MCP. No code change is needed. Then turn it on for each assistant under ⚙ → MCP. To run a local (stdio) MCP server inside the assistant VM, add a JSON file to [`tool_catalog/`](src/fa-shared-context-poc/tool_catalog), install the executable in the Hosted image, and run `azd deploy`.
- **Skills.** Write a draft under Agent Team settings → Team Skills. It takes effect only after the Team owner approves that version. To change it, write a new version; the old one is kept. Then pick it for each assistant under ⚙ → Skills. The workbench has no place to write an SOP, so write the procedure as a skill. You set an assistant's role when you add it with "+". A new azd environment sets up the permissions "+" needs by default; if you opted out with `azd env set FA_ENABLE_AGENT_ONBOARDING false`, a new assistant lacks the setup and permissions it needs to run.
- **Plugins.** FA has no plugin mechanism. The Plugins tab under an assistant's ⚙ turns FA's built-in capabilities (desktop, shell, delegation and so on) on or off. You cannot install new ones there.
- **Connectors are optional.** `azd up` needs no Teams, Work IQ or GitHub. When it finishes, connectors are off and Team MCP is on. Microsoft Teams (through Work IQ) is the only connector today. It needs your own Entra app and `FA_TEAM_CONNECTORS_ENABLED=1` on the API. An azd environment has no setting for it yet, and this is not verified.
- **Adding your own connector.** For a service that uses a key, use Team MCP. For a service that uses Entra OAuth, add a `ConnectorRef` to [`connector_registry.py`](src/fa-shared-context-poc/shared_context/connector_registry.py); configuration alone cannot add one. Nobody has done this for real yet.

<a id="in-development"></a>
## In development

The features below have code, but they are not accepted in every environment yet, and `azd up` does not turn them on. The linked documents are in Traditional Chinese.

- **Microsoft Teams connector (through Work IQ MCP).** Assistants can read Teams chat and channel messages and post messages for you; by default, every post needs you to press allow on an approval card. A person has gone through it on the author's test environment, and environments created by `azd up` have it off. The steps are in [turning on the Teams connector](docs/extending.md#開啟-teams-connector), and how far acceptance has gone is in the [acceptance record](docs/extending.md#connector-acceptance).
- **Chat with an assistant in Teams (the Teams relay).** Send a message to an assistant directly in Teams; the assistant runs the turn on its own VM and sends the reply back to Teams. When it needs approval, reply `允許 code` or `拒絕 code` (you must type the Chinese words 允許 for allow or 拒絕 for deny; English words are treated as a normal message), and type `/foundry_new_preview` to start a new conversation; both were tested from Teams on the author's test environment. A reply that finishes after about 110 seconds is added to the reply to your next message (under 「先前的回覆（剛完成）：」), even after you started a new conversation. It is normally added once; when the relay's receipt cannot be confirmed it is added again later rather than lost, so a rare repeat is possible. This part is code and offline tests only, not deployed yet. It is off on the author's staging environment and there is no deploy script yet; the design, limits and steps are in [Teams chat design](docs/teams-chat-design.md) and the [relay notes](src/fa-teams-relay/README.md), and the test record is in [progress](docs/progress.md#teams-relay-observed-1011).
- **GitHub connector.** Still in design; there is no code for it. See [connector design](docs/connector-design.md#分階段).

## Documentation

📖 If you are new to FA, start with the [glossary](CONTEXT.md) and [requirements](docs/requirements.md). The full index is [docs/README.md](docs/README.md). If you want to contribute or maintain FA, read [AGENTS.md](AGENTS.md) and [CURRENT-STATE](docs/status/CURRENT-STATE.md) next. Those two are written for maintainers and use many internal code names. Most project documents are in Traditional Chinese.

- **Concepts**: [glossary](CONTEXT.md) · [requirements](docs/requirements.md) · [specification](docs/specification.md)
- **Architecture decisions**: [Team trust boundary](docs/adr/0006-team-trust-boundary-grokbot-mode.md) · [Foundry Hosted native features first](docs/adr/0007-foundry-hosted-native-first.md) · [Environment tiers and CI/CD](docs/adr/0008-environment-tiers-staging-cicd.md)
- **Implementation**: [API and Hosted](src/fa-shared-context-poc/README.md) · [workbench](src/fa-shared-context-poc/web_test_console/README.md) · [behavior control spec](docs/FA-AGENT-BEHAVIOR-SPEC.md)
- **Deployment**: [deployment flow](deploy/README.md) · [connector design](docs/connector-design.md)

## Contributing

Before you start, read the architecture boundaries in [AGENTS.md](AGENTS.md) (written for contributors and maintainers) and the matching sections of [CONVENTIONS](docs/CONVENTIONS.md). Make changes on a feature branch and open a PR. Do not push to `main` directly.

```powershell
git switch -c <your-branch>
.\src\fa-shared-context-poc\.venv\Scripts\python.exe -m pytest .\tests\shared_context\<changed-test-file> -q -p no:cacheprovider --basetemp "C:\t\fa-$(Get-Random)"
```

## Telemetry

By default, the assistant VM sends a trace of every Copilot SDK turn to the Application Insights resource linked to your Foundry project, including message text. Set `FA_SDK_TELEMETRY=0` to turn it off. External tools an assistant uses, such as WebIQ search, Work IQ, or MCP servers you add, send requests to their own services. The implementation is in [`sdk_telemetry.py`](src/fa-shared-context-poc/shared_context/sdk_telemetry.py).

## Community

Questions and suggestions are welcome. Open an issue or send a PR.

## License

FA is licensed under the [MIT License](LICENSE).

---
