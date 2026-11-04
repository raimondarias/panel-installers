# Pterodactyl Installer · Guia rápido em português brasileiro

Instale pelo repositório publicado:

```bash
curl -fsSL https://raw.githubusercontent.com/raimondarias/pterodactyl-installer/main/install.sh -o ptero-setup.sh && sudo bash ptero-setup.sh
```

A primeira pergunta será em inglês: **Which language would you like to use?**. Depois apenas o pacote do idioma escolhido será baixado e verificado, e o assistente começará sem repetir a pergunta. O repositório publicado inclui os três pacotes de idioma.

Ao iniciar você verá **1. English · 2. Español · 3. Português (Brasil)**. Selecione 3 para usar todo o assistente em português brasileiro. Também é possível alterar o idioma pelo menu.

Selecione **Simples (Recomendado)** ou **Avançado** e escolha:

- **Standalone nativo (recomendado), primeiro**, ou Docker.
- MariaDB (recomendado) ou MySQL.
- IPv4 público da máquina, com HTTP e sem SSL; ou domínio/subdomínio, com HTTPS recomendado.
- E-mail, usuário e senha oculta do primeiro administrador. Deixe a senha vazia para gerar uma; ela será exibida para você salvar.
- SMTP opcional, que também pode ser configurado depois.

O modo avançado permite escolher versão, PHP 8.3/8.4 no standalone, porta HTTP, IP de escuta e fuso horário. Um resumo aparece antes da instalação.

O mecanismo é escolhido uma única vez para o Painel e os jogos. O assistente também oferece preparar um host SQL local isolado, nativo (recomendado) ou no Docker, registrar um existente ou configurar depois (padrão). A opção nativa separa usuário do sistema, dados, porta e serviço do banco do Painel. O Docker continua necessário para os jogos e sua rede privada. O host é registrado automaticamente no Painel e exibe host, porta, usuário e senha na tela e em um arquivo privado. O modo avançado permite personalizar as credenciais e associar um node existente; 0 deixa o host global. Cada servidor de jogo precisa de um limite de bancos de dados maior que zero.

As opções 20 e 21 configuram o host SQL e exibem as credenciais. O idioma muda na opção 23. Os bancos dos jogos e suas credenciais são mantidos ao excluir o Painel. Você pode preparar SQL privado em outra máquina pela ação remota via SSH; rotas privadas e chaves verificadas precisam estar prontas. O instalador prepara e registra o host; backups e restaurações dos dados dos jogos pertencem ao Pterodactyl ou ao seu próprio sistema de backup. Veja os detalhes de rede e firewall no [guia principal](OPERATIONS.md#game-database-hosts).

Prévia sem root e sem instalar:

```bash
bash scripts/run_local.sh --language pt-BR --preview
```

São aceitos Ubuntu 22.04/24.04/26.04 e Debian 12/13 com systemd. São necessários root, Python 3.10+, pelo menos 4 GiB livres e memória suficiente. O inicializador instala o Python se necessário. Nenhum pacote pip é exigido. MySQL nativo no Debian exige amd64; no Debian ARM64 use MariaDB ou Docker.

O Wings precisa do Docker para executar os jogos, mesmo com o Painel standalone. Você pode usar o Wings como serviço systemd (recomendado) ou contêiner oficial. Use a criação automática de localização, node e allocations, ou crie-os no Painel e vincule o Wings com o token Auto-Deploy. A digitação do token é oculta e ele não é enviado por argumentos de processo.

O menu permite instalar/desinstalar os componentes juntos ou separados, atualizar, emitir SSL para Wings, fazer backups, restaurar, consultar diagnóstico/logs e configurar e-mail/administradores. Cada atualização do Painel salva um ponto de recuperação verificado: se ela falhar, o instalador oferece reverter na hora, e **Reverter atualização do Painel** (35) volta para a versão anterior com o banco de dados compatível a qualquer momento.

A desinstalação mantém os dados por padrão. Excluí-los exige confirmação e um backup correto. Os backups do Painel incluem seu próprio banco SQL e as chaves originais. Os únicos backups oferecidos são **Painel e configuração do Wings**; nunca incluem arquivos, volumes, mundos ou bancos de dados dos servidores hospedados. Guarde os arquivos `.tar.gz` e `.sha256` fora do servidor.

O Painel v2 é beta. Os testes automatizados e as verificações reais de arquivos/configurações não substituem um teste completo em um VPS limpo. Consulte [o relatório](../test-results/REPORT.md), [o guia principal](../README.md) e [os testes de integração](TESTING.md).

A interface abre uma TUI de tela cheia em um terminal SSH compatível. Use ↑/↓ para navegar, Tab ou ←/→ para mudar de grupo, Enter para escolher, / para buscar, F5 para atualizar os estados e Esc/Q para voltar ou sair. As operações estão implementadas. Ações que dependem de um componente ainda não instalado explicam o que falta e como prepará-lo antes da execução. Em uma máquina nova, Diagnóstico mostra informações úteis e o próximo passo. Teste sem instalar com `bash ptero-setup.sh --preview`; use `--plain` para menu numérico e `--no-animation` para reduzir o movimento. [Detalhes e capturas](TUI.md).

Você pode selecionar cada opção com um clique do mouse em terminais compatíveis, incluindo a seleção inicial do idioma. As setas e Enter também continuam disponíveis.

Inclui criação automática de nodes e allocations, SQL de jogos nativo isolado, backups criptografados agendados do Painel e/ou configuração do Wings, restauração do Painel, retomada por etapas e preparação remota por SSH com chaves verificadas. As opções 28, 29 e 30 agendam esses backups, executam o agendamento e baixam uma cópia verificada. Uma máquina somente com Wings também pode agendar o backup da sua configuração. Veja [as operações avançadas](ADVANCED.md).

O Ubuntu 26.04 é testado com Docker. Standalone exige um repositório PHP 8.3/8.4 preparado; o PPA testado ainda não publica resolute. Sem esse requisito, o instalador para antes de alterar pacotes.
