# BraFurries-Discord

A feature-rich Discord bot designed to enhance your furry community experience.

## Table of content

* ### [**About The Bot**](#about-the-bot)
* [**Requirements**](#requirements)
* [**Getting started**](#getting-started)
* [**Features & Commands**](#features--commands)
    * [VIP Member Customization](#vip-member-customization)
    * [Member Registration & Info](#member-registration-and-info)
    * [Bot Interactions](#bot-interactions)
    * [XP System](#xp-system)
    * [Roleplay Commands](#roleplay-commands)
    * [Moderation Tools](#moderation-tools)
    * [Utility Commands](#utility-commands)
* [**Common errors**](#common-errors)
* [**Database migrations**](#database-migrations)
* [**Contributing**](#contributing)
* [**Author**](#author)
* [**License**](#license)

## About The Bot

BraFurries-Discord is a comprehensive bot packed with features to streamline community management, foster engagement, and create a fun and interactive environment for your furry members. With its intuitive commands and intuitive interface, the bot empowers you to:

* Personalize VIP member experiences with color and icon customization.
* Facilitate member registration and information tracking.
* Organize and manage events with creation, scheduling, approval workflows, and state-based filtering.
* Implement a dynamic XP system to add a gamified layer to your community.
* Enhance roleplay interactions with dedicated commands for actions like bathing, working, dueling, drawing, and writing.
* Maintain order with effective moderation tools like warnings and role management.
* Empower members with helpful utility commands.

**Please Note:** Some commands may require moderator or administrator permissions that need to be set up.

## Requirements

* **Python:** Version 3.12, matching CI and the Docker runtime (check with `python --version`) - [Download](https://www.python.org/downloads/)
* **Pip:** Python package manager (usually comes bundled with Python installation) - [More Info](https://pip.pypa.io/en/stable/installation/)

## Bot status endpoint

You can expose a small HTTP status API for external services or monitoring tools. When enabled, it serves JSON with runtime and Discord metrics.

Set these environment variables in your `.env` file:

```env
BOT_STATUS_API_PORT=8088
BOT_STATUS_API_HOST=0.0.0.0
BOT_STATUS_API_TOKEN=your-shared-secret
BOT_API_BASE_URL=http://127.0.0.1:18080
```

`BOT_STATUS_API_TOKEN` também autentica a consulta interna do Coddy à API para
os agregados de identidade do `/perfil`. `BOT_API_BASE_URL` aponta para a API
BraFurries e usa `http://127.0.0.1:18080` por padrão.

The API exposes:

- `GET /status` returns the current payload with bot, Discord, and runtime metrics.
- `GET /health` returns the same payload with `200` when the bot is ready and `503` otherwise.

If you set `BOT_STATUS_API_TOKEN`, send `Authorization: Bearer <token>` in the request.

Typical payload fields include bot name, readiness, connected state, guild count, cached user count, command count, cogs loaded, latency, uptime, start time, Python version, discord.py version, and process id.

## Community synchronization

Coddy uses the same internal API configuration documented above to synchronize Community lifecycle and membership state. The API must be deployed with the matching Community lifecycle contract before this runtime version.

- guild join/remove/update events update integration state through the API;
- member join/remove/update events update Community membership through the API;
- Portaria approval changes are sent as explicit approval observations;
- startup and periodic reconciliation use a complete Discord member snapshot;
- manual sync requests from the platform are consumed from the API-owned queue.

A partial Discord member view is never submitted as complete and therefore cannot mark other members absent.

## Getting Started

**1. Installation**

   - Ensure you have Python and Pip installed on your system.
   - Clone this repository using Git:

     ```bash
     git clone https://github.com/BraFurries/BraFurries-Discord.git
     ```

   - Navigate to the project directory:

     ```bash
     cd BraFurries-Discord
     ```

   - Create a virtual environment (recommended for isolation):

     ```bash
     python -m venv venv  # Replace "venv" with your desired virtual environment name
     source venv/bin/activate  # Activate the virtual environment (Linux/macOS)
     venv\Scripts\activate.bat  # Activate the virtual environment (Windows)
     ```

**2. Configuration**

   - Create a file named `.env` in the project root directory. This file will store your Discord bot token.
   - Add a line like `DISCORD_TOKEN=YOUR_BOT_TOKEN` to the `.env` file, replacing `YOUR_BOT_TOKEN` with your actual Discord bot token (obtained from the Discord Developer Portal).
     - Optionally add `BOT_STATUS_API_PORT`, `BOT_STATUS_API_HOST`, and `BOT_STATUS_API_TOKEN` if you want to expose the status API.
   - **Important:** Keep the `.env` file excluded from version control (e.g., using a `.gitignore` file).

**3. Required Permissions**

   - Enable the `applications.commands` application scope in the `OAuth2` tab of your Discord Developer Portal.
   - Enable the `Server Members Intent` and `Message Content Intent` in the `Bot` tab of your Discord Developer Portal.

**4. Run the Bot**

   - Install project dependencies:

     ```bash
     pip install -r requirements.txt
     ```

   - Start the bot using the appropriate script (check project files for the specific script name):

     ```bash
     python main.py  # Only with isolated development credentials and database
     ```


# Features & Commands

> Todos os comandos usam **slash commands** (ex.: `/xp`).
> Catálogo revisado para cobrir comandos e grupos atualmente presentes no projeto.

## Catálogo completo de comandos

### Ajuda
- `/ajuda` — mostra um guia geral com os principais comandos para membros.
- `/ajuda-staff` **(staff)** — exibe os comandos operacionais usados pela equipe de staff.
- `/ajuda-admin` **(staff/admin)** — mostra comandos avançados de administração e configuração global.

### Registro e informações
- `/registrar local` — registra sua localidade para recursos da comunidade.
- `/registrar aniversario` — registra/atualiza sua data de aniversário.
- `/furros_na_area` — lista membros registrados por região/localidade.
- `/aniversarios` — consulta próximos aniversariantes.
- `/perfil` — mostra informações de perfil e progresso do membro.
- `/registrar_usuario` **(staff)** — registra manualmente um usuário quando o autoatendimento não for possível.

### Eventos
- `/eventos` — lista eventos disponíveis e próximos eventos.
- `/evento` — consulta detalhes de um evento específico.
- O gerenciamento administrativo de eventos não é mais realizado pelo Coddy.

### XP e roleplay
- `/xp` — mostra seu XP atual e progresso.
- `/xp_ranking` — exibe ranking de XP do servidor.
- `/daily` — resgata recompensa diária.
- `/admin-xp ajustar` **(staff)** — ajusta XP de membros manualmente.
- `/admin-xp config` **(staff)** — configura regras/sistema de XP.
- `/admin-xp simular` **(staff)** — simula ganhos de XP para validação.
- `/admin-xp voice-inspect` **(staff)** — inspeciona contagem de XP por atividade em voz.
- `/rp banho` — ação de roleplay temática.
- `/rp trabalhar` — ação de roleplay para ganho/progressão.
- `/rp duelo` — inicia duelo de roleplay.
- `/rp desenhar` — ação de roleplay de criação artística.
- `/rp escrever` — ação de roleplay de escrita.

### Economia
- `/eco apostar` — permite apostar moedas do sistema.
- `/eco saldo` — consulta saldo da carteira.
- `/eco transferir` — transfere saldo para outro membro.
- `/eco loja` — lista itens disponíveis na loja.
- `/eco comprar` — compra item da loja com saldo.
- `/eco inventario` — mostra itens possuídos.
- `/eco chuva` — distribui recompensa coletiva no chat.
- `/admin-eco set-saldo` **(staff)** — define/ajusta saldo de usuário.
- `/admin-eco loja-adicionar` **(staff)** — adiciona item à loja.
- `/admin-eco loja-editar` **(staff)** — edita item existente da loja.

### Play e coop
- `/play boop` — interação social rápida entre membros.
- `/play minigames` — abre minigames disponíveis.
- `/play duelo` — duelo de gameplay entre usuários.
- `/play coop boss` — inicia encontro cooperativo contra boss.
- `/play coop ritual` — inicia atividade cooperativa de ritual.
- `/play coop raid` — inicia raid cooperativa.
- `/play coop expedition` — inicia expedição cooperativa.

### VIP
- `/vip customizar` — permite personalizar recursos de VIP do próprio usuário.
- `/admin vip setar_cargos` **(staff)** — configura cargos vinculados ao sistema VIP.
- `/admin vip setar_intervalo` **(staff)** — define intervalo/frequência de benefícios VIP.
- `/admin vip setar_prefixo` **(staff)** — configura prefixo visual para usuários VIP.

### Moderação
- `/warn` **(staff)** — aplica advertência disciplinar a membro.
- `/ban` **(staff)** — bane membro do servidor.
- `/notas_add` **(staff)** — adiciona nota interna de moderação em usuário.
- `/portaria_aprovar` **(staff)** — aprova entrada de membro na portaria.
- `/portaria_liberar_conta` **(staff)** — libera conta travada na portaria.
- `/chat_analisar_membro` **(staff)** — analisa histórico de mensagens de um membro.
- `/chat_resumir` **(staff)** — gera resumo de conversa/canal para suporte da moderação.

### Segurança
- `/security whitelist_add` **(staff)** — adiciona item à whitelist de segurança.
- `/security whitelist_remove` **(staff)** — remove item da whitelist.
- `/security whitelist_list` **(staff)** — lista itens atualmente permitidos na whitelist.
- `/security pendencias` **(staff)** — mostra pendências/incidentes de segurança para tratamento.

### Utilitários
- `/call_titio` — aciona utilitário rápido do bot para suporte.
- `/temp_role` **(staff)** — atribui cargo temporário para um membro.
- `/recordes` — exibe recordes/rankings registrados.
- `/recorde_adicionar_blacklist` **(staff)** — adiciona item/usuário à blacklist de recordes.
- `/coddy falar` **(staff)** — envia mensagem pelo bot em canal alvo.
- `/coddy status` **(staff)** — altera/consulta status operacional do bot.

### Relatórios
- `/relatorios atividades_em_alta` **(staff)** — mostra atividades/canais com maior movimento.
- `/relatorios relatorio_atividades` **(staff)** — gera relatório consolidado de atividades.
- `/relatorios portaria` **(staff)** — gera relatório específico do fluxo de portaria.

### Formulários
- `/formulario publicar` **(staff)** — publica formulário para coleta de respostas.
- `/formulario criar` **(staff)** — cria novo formulário.
- `/formulario listar` **(staff)** — lista formulários cadastrados.
- `/formulario configurar_portaria` **(staff)** — vincula e configura formulário da portaria.
- `/formulario editar` **(staff)** — edita formulário existente.
- `/formulario auditar_ficha_portaria` **(staff)** — audita fichas/respostas da portaria.

### Backup, migração e recuperação
- `/backup gerar` **(staff)** — gera snapshot de backup dos dados suportados.
- `/backup listar` **(staff)** — lista backups disponíveis.
- `/backup sincronizar` **(staff)** — sincroniza backups entre origens/destinos configurados.
- `/migrate permissoes canais` **(staff)** — migra permissões de canais para padrão atual.
- `/importar warns` **(staff)** — importa advertências de base legada.
- `/recover cargos` **(staff)** — recupera cargos de membros após incidentes/migração.

### Administração geral (`/admin`)
- `/admin conectar_conta` **(staff/admin)** — vincula conta externa/serviço ao bot.
- `/admin mensagens_servidor` **(staff/admin)** — configura mensagens institucionais do servidor.
- `/admin configurar-servidor` **(staff/admin)** — aplica configurações gerais do servidor no bot.
- `/admin logs` **(staff/admin)** — configura/consulta canais e tipos de logs.
- `/admin config_ia` **(staff/admin)** — ajusta parâmetros de recursos de IA.
- `/admin set_token_ia` **(staff/admin)** — define token/chave de integração de IA.
- `/admin set_admins_ia` **(staff/admin)** — define administradores autorizados para comandos de IA.
- `/admin cores_staff` **(staff/admin)** — configura cores/cargos visuais da staff.
- `/admin adicionar-cargo-todos` **(staff/admin)** — adiciona um cargo para todos os membros elegíveis.
- `/admin atualizar-comandos` **(staff/admin)** — força atualização/sincronização dos slash commands.

#### Subcomandos administrativos
- `/admin canais setar_ia` **(staff/admin)** — define canais usados pelos recursos de IA.
- `/admin canais setar_aniversario` **(staff/admin)** — define canal de avisos de aniversário.
- `/admin cargos setar` **(staff/admin)** — configura cargos operacionais usados pelo bot.
- `/admin portaria setar` **(staff/admin)** — configura canais/cargos/regras da portaria.
- `/admin bump setar_warn` **(staff/admin)** — configura aviso de cooldown/uso de bump.
- `/admin bump setar_reward` **(staff/admin)** — configura recompensa por bump.
- `/admin bump setar_mensal` **(staff/admin)** — configura meta/reward mensal de bump.
- `/admin staff registrar` **(staff/admin)** — registra membro como staff no sistema.
- `/admin staff remover` **(staff/admin)** — remove membro do cadastro de staff.
- `/admin auto-cargos configurar` **(staff/admin)** — configura distribuição automática de cargos.
- `/admin moderacao_colaborativa setar` **(staff/admin)** — configura sistema de moderação colaborativa.
- `/admin hashtags status` **(staff/admin)** — mostra status do sistema de hashtags.
- `/admin hashtags ativar` **(staff/admin)** — ativa/desativa sistema de hashtags.
- `/admin hashtags setar_canais` **(staff/admin)** — define canais monitorados por hashtags.
- `/admin hashtags setar_cargo_autor` **(staff/admin)** — define cargo automático para autores por hashtag.
- `/admin hashtags limpar_cargo_autor` **(staff/admin)** — remove configuração de cargo automático por hashtag.
- `/admin hashtags mapear` **(staff/admin)** — mapeia hashtag para ação/categoria.
- `/admin hashtags desmapear` **(staff/admin)** — remove mapeamento de hashtag.
- `/admin threads status_restricao_autor` **(staff/admin)** — mostra estado da restrição de autor em threads.
- `/admin threads ativar_restricao_autor` **(staff/admin)** — ativa/desativa restrição de criação por autor.
- `/admin threads setar_foruns_restritos` **(staff/admin)** — define fóruns com restrições especiais.
- `/admin threads limpar_foruns_restritos` **(staff/admin)** — limpa lista de fóruns restritos.
- `/admin configuracoes listar` **(staff/admin)** — lista configuração persistida por categoria.

### Categorias aceitas em `/admin configuracoes listar`
- `portaria`
- `vip`
- `bump`
- `aniversário`
- `IA`
- `auto-cargos`
- `moderação colaborativa`
- `cargos de staff`
- `XP`

### Observações
- Alguns comandos exigem permissões de staff/admin.
- Use o autocomplete do Discord para ver os parâmetros disponíveis de cada comando.

## Common Errors

Here are some common errors and solutions:

* **Dependencies aren't up to date:** Regularly update packages using `pip install -r requirements.txt`.
* **Unable to register users with special characters:** The bot now normalizes usernames by removing accents and symbols before saving them in the database.

## Schema ownership and deployment

**BraFurries-Database** is the single source of reviewed SQL schema changes for the shared MariaDB database. The Coddy runtime must not manage schema migrations, and a Coddy deploy **does not run SQL migrations**. The old `scripts/run_migrations.py` and `migrations/` tree have been retired; the historical `schema_migrations` table, if present, must not be dropped automatically.

The official Database repository stores versioned migrations under `flyway/sql/versioned/`. Flyway baseline/operational adoption in production is a **separate rollout**; do not infer that an existing database has a Flyway schema history. Apply schema changes only through the separately approved Database rollout process, with validated preconditions, and **before** deploying Coddy code that depends on them.

### Production deployment

[`Deploy em Produção`](.github/workflows/prod-deploy.yaml) supports **manual deployment** via `workflow_dispatch` on the protected `main` branch and **opt-in automatic deployment** on `push` to `main`.

To enable automatic deployment, create the repository Actions variable `CODDY_AUTO_DEPLOY_ENABLED` with the exact value `true` (Settings → Secrets and variables → Actions → Variables). With the variable absent or different from `true`, pushes to `main` do **not** build or deploy production; manual dispatch continues to work. Setting it to `false` pauses future automatic deploys without changing the workflow. **Merging this workflow alone does not enable automatic deployment.**

For an enabled push, the production workflow runs a pinned Gitleaks history scan, executes the Python tests, builds and publishes an immutable GHCR image, and deploys via the restricted `coddy-production` runner group and the `Produção` environment. It validates liveness and readiness and refuses to deploy a stale commit if `main` has advanced in the meantime. Pull request events never access the production runner or production secrets. The separate `PR Validation` workflow continues to check PRs and pushes.

**Database and cross-repo ordering:** before merging Coddy changes that depend on a new schema or API contract, ensure the corresponding reviewed Database migration and compatible API version are already deployed. There is **no automatic schema readiness check** in this workflow, and the container rollback cannot revert migrations. If rollout ordering cannot be guaranteed, set `CODDY_AUTO_DEPLOY_ENABLED=false`, deploy the dependencies first and use manual dispatch for the coordinated release. Do not treat a passing CI as proof of production schema compatibility.

The host-side deploy wrapper must remain configured **without PM2 or the retired migration runner**. It may stage/promote the configured environment and roll back the previous Docker image if readiness fails; never run historical migration SQL from this repository as part of deployment.

### Legacy runtime schema mutations

Some legacy features still contain DDL in `core/database.py` and `cogs/xp.py`. Their phased retirement, including Database-owned replacements and compatibility checks, is tracked in [issue #3](https://github.com/BraFurries/BraFurries-Discord/issues/3). This cleanup removes only the standalone migration executor and obsolete automatic-deploy gate, **not** those runtime mutations.

If startup reports a missing column or table (for example `config_server_settings.trending_presence_enabled`), inspect the actual schema and coordinate a reviewed Database migration; do not patch the shared production schema through Coddy.

## Contributing

We welcome contributions to improve BraFurries Discord Bot! Please see the CONTRIBUTING.md file for guidelines.

## Author

[Fernando FR](https://github.com/ferlicio)

<!-- ## Support me

<a href="https://www.buymeacoffee.com/" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a> -->

## License

This project is licensed under the ***Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International Public License*** - see the [LICENSE.md](LICENSE) file for details
