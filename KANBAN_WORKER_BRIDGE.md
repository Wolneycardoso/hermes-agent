# Ponte de worker Kanban — candidata local, sem ativação

## Base e composição

Base85c2ab8d1b7f5d1807613af03fb9625cb155c707, branch remota otto/kanban-natural-runtime-base do fork Wolneycardoso/hermes-agent. É a mesma base do PR1 (head dcdd23627725c6821c3cee746bfba3f3295e7b5a) e corresponde à fonte runtime usada no ensaio anterior. O main remoto2efd6961f78006cb46ad39f3ecb19b21f5bee603 é ancestral1877commits atrás; portar sobre main criaria escopo de atualização desnecessário. Isto não é uma nova leitura de saúde/versão do processo ativo.

Este patch só adiciona módulos e testes. Não altera kanban_db.py, dispatcher, credenciais, configurações, CLI, serviços ou PR1. A alteração local preexistente em unblock_task permanece intacta no checkout original.

## Capacidades e fronteiras

kanban_worker_bridge.Bridge recebe explicitamente módulo/SQLite, queue port e verificador. Correlaciona task/run/job/revision/deadline, persiste intenção/readback, cobra critérios de correção, bloqueia sem progresso ou prazo e confirma efeito terminal. Requer run já adquirido legitimamente e completion_contract=local-only. O construtor cria tabelas/triggers synthetic_bridge somente na conexão fornecida: NÃO chamar em banco ativo. Não é migração de produção. Probe de relógio SQL falha antes de DDL quando indisponível; trigger também bloqueia NULL em vez de permitir conclusão.

kanban_worker_transport.LoopbackQueue é opt-in, só127.0.0.1 e endpoint de teste /fixture; recebe capability em memória, sem leitura de secrets, redirects ou criação de conta. 400/401/403 significam rejeição pré-escrita no contrato desse harness; respostas desconhecidas, falha/timeout ou corpo inválido permanecem incertos. Não constitui rota autenticada de produção.

Sem import automático/wiring no gateway. Sem LLM/publisher e sem permissão para alterar isolamento. Scope de recursos controla consumo, não confina código hostil: VM permanece necessária para CI não confiável.

## Validação

Runner canônico scripts/run_tests.sh, ambiente PM isolado com dev/test travados, HOME fictício, -j1 e sem retries automáticos:

- Ponte inicial:20PASS,14.5s.
- Final sobre85c2:45PASS,35.5s, incluindo21ponte (novo relógioSQL),8transporte e16regressões existentes de claim/conclusão/bloqueio.
- Compatibilidade sobrePR1dcdd236277:32PASS,19FAIL,41.7s antes do guardSQLite adicional. As19falhas são STALE_RUN porque claim_task recusa local-only e não cria run. Não foram ocultadas, puladas ou convertidas em PASS. Testes nativos de segurança/idempotência do PR1 passaram. O guardSQLite posterior não resolve essa ausência de backend.
- scripts/check --staged final:11checksPASS após ajusteSQL, com0blocking/0advisory no code-health. Nenhum TypeScript alterado neste port; typecheck não aplicável.
- Revisão independente encontrou o riscoSQLNULL e o fix/teste foram incluídos. Não houve alteração de guard do PR1.
- Reensaio integrado após mudança funcional do guard: PostgreSQL/PostgREST/API/claimWOOS real + Bridge/LoopbackQueue portados + executor determinístico + aceiteKanban PASS na base85c2. Scope1536MiB/200%CPU/128tasks; journal2.147s wall,2.416sCPU,pico271.8M. Uma execução de revalidação, cleanup/readback confirmou portas/scope/cluster/identidade removidos. Não repetir/interpretar como provaPR1 ou workerLLM produtivo.

A queue dos testes unitários é double explícito; funções/schema/ownership/transações do Kanban são reais. O canário externo usaWOOSPR60 e dados fictícios; o launcher não é parte deste patch. Sem baseline temporal equivalente ou ganho/SLA declarado.

## Bloqueio operacional preservado

O PR1 deliberadamente bloqueia claim de local-only/natural-local-only e host spawn até existir backend isolado. Esta ponte começa DEPOIS do claim e não oferece esse backend. Portanto, ainda não é composicionalmente ativável comPR1. Próxima integração precisa de claim legítimo condicionado a prova de backendVM restrito, owner/run persistido, deadline, cancelamento/readback, preservando os guards de spawn e propagação. Não inserir run manualmente, remover isolation_policy ou trocar completion_contract para forçar teste verde.

Destino de publicação proposto: nova branch codex/kanban-bounded-worker contra otto/kanban-natural-runtime-base, PRdraft dependente/compatibilidade pendente comPR1. Publicação em rascunho autorizada; refs devem ser conferidas antes do push. Nenhuma ativação foi autorizada por essa publicação. Não usar main nem substituirPR1. Ativação exigirá aprovação distinta de schema, endpoint/identidade/perfil, runtime e reversão, e fases separadas para LLM/custo/publisher.

Classificação detalhada e fonte do teste de rejeição: KANBAN_WORKER_PR1_CLASSIFICATION.md; lista individual: KANBAN_WORKER_PR1_FAILURES.json. Os 25 PASS de rejeição não substituem os 19 FAIL positivos da composição.
