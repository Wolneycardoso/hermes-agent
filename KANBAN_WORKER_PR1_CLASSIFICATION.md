# Classificação das 19 falhas contra PR1

Base positiva testada:85c2ab8d1b7f5d1807613af03fb9625cb155c707. PR1:dcdd23627725c6821c3cee746bfba3f3295e7b5a. Ponte local:387b14f4fdaa1ee17e47ae64c70310567f02adca. Nada publicado/ativado nesta etapa.

## Diagnóstico comprovado

O log preservado hermes-port-pr1-tests.log contém19falhas e19errosSTALE_RUN. Todas derivam da mesma premissa inválida da fixture: setUp chama claim_task para completion_contract=local-only, ignora retornoNone e usa current_run_id ausente. O PR1 chama reject_restricted_claim dentro de write_txn antes de _claim_and_open_run. claim_is_restricted retorna true para local-only OU isolation_policy=natural-local-only. O evento gravado é claim_rejected com reason=local_only_completion_contract. Não existe argumento/callback/lease implementado que permita a esse caminho reconhecer um backend isolado. Dispatcher e host spawn também recusam tarefas restritas.

Classificação por camada:

- 19fixtures positivas incompatíveis com o guard novo: exigem run adquirido pelo comportamento legado.
- Uma capacidade de integração ausente: claim legítimo respaldado por backend isolado, com autoridade/lease e correlação.
- Zero regressões de lógica pós-claim DEMONSTRADAS por essas19falhas. Isso NÃO prova ausência de bugs: os cenários de prazo, correção, concorrência e aceite nem foram alcançados noPR1.
- Não são falhas a corrigir removendo guard ou fabricando run. Não alteramos isolamento, contrato da tarefa, status ou claim para produzirPASS.

Lista individual em PR1-FAILURE-CLASSIFICATION.json. Os19FAIL anteriores continuam válidos e visíveis; nenhumxfail/skip e nenhuma conversão de expectativa para maquiar a suíte.

## Rejeição esperada testada separadamente

Novo arquivo somente no worktree de compatibilidade: tests/hermes_cli/test_kanban_worker_pr1_boundary.py. Dois casos usam schema/funções reais: local-only e natural-local-only. Comprovam claimNone, ready preservado, zero task_runs, evento real de rejeição e Bridge recusandoSTALE_RUN sem ler/admitir fila. ForbiddenQueue é tripwire que falha se chamado, NÃO backendstub/flag de segurança.

Runner oficial, HOMEtemporário,1worker,retries0:
-2testes de fronteira +16natural_safety +7idempotency_fail_closed =25PASS em17.6s.
-19cenários positivos ainda não exercitados comPR1. Não declarar a suíte completa compatível.

Revisão independente em leitura confirmou classificação e ausência de contorno. Não precisou repetir backendcanário: só adicionamos testes de rejeição/documentação, sem mudar contrato funcional da ponte.

## Por que o canário existente não satisfaz PR1

O canário positivo executa Node sintético no HOST, supervisionado por process-tree e limitado por cgroup, com banco e credenciais fictícios. local-only é contrato de entrega, não prova de confinamento. Limites de CPU/RAM/tasks e subreaper não impedem acesso de código hostil ao host. Não houve VM de execução nesse canário. Portanto ele não fornece o backendisolado exigido peloPR1 e NÃO prova integração positiva.

A fonte disponível do piloto Boteco em pilot/executor/boteco_test.py descreve protocolo restrito a wo-os/boteco-labs, job/commit fixos, e recusa start enviado. Essa fonte não constitui implementação comprovada para executar missões WO-OS/Kanban. Não foi consultado socket/estado privilegiado ou contornada leitura negada; não afirmamos estado atual de serviço a partir da proposta histórica.

## Requisito concreto para avançar

1. Backend de execução efetivamente isolado (preferencialmenteVM efêmera), perfil protegido contra escrita do código testado, imagem/commit/repositório permitidos e limites, rede/mounts/segredos explicitamente delimitados. Broker deve fornecer prova/readback verificável de reserva/execução/cleanup; declaração do cliente não vale.
2. Identidade do coordenador validada no canal do broker e lease ligado a task/revisão/request/owner/deadline. Suporte ao repositório WO-OS precisa de aprovação própria se o executor instalado só admite Boteco. Sem sudo geral ou expansão silenciosa.
3. Novo caminho restrito de claim no código do PR1, mantendo claim_task e host spawn atuais fechados. Reserva antes do claim, vínculo durável ao run adquirido por CAS, início somente após readback dessa vinculação; crash entre broker/SQLite exige reconciliação/cancelamento, não run manual ou retry cego. Não é transação atômica entre dois sistemas: definir estados/recibos de recuperação explicitamente.
4. Testar negações reais: backend ausente, prova inválida/vencida, repo/commit/owner/run diferentes, duas reservas/claims concorrentes, resultado incerto e cancelamento. Depois repetir os19cenários positivos sobrePR1 com run obtido por esse caminho real, sob os limites já aprovados.

Esse mecanismo não existe no código consultado. Implementar um booleano backend_available, simular atestado, trocar completion_contract ou abrir acessohost seria contorno. Por isso não adicionamos integração executável fictícia. Próxima decisão é escopo/destino do backendVM e contrato de claim, seguida de implementação isolada verificável; ativação/runtime/credenciais/permissões persistentes continuam sujeitos a aprovação específica.

Estado final: patch da ponte revisável sobre85c2, compatibilidade positiva comPR1 bloqueada por UMA capacidade ausente. Nenhum novo processo de serviço, segredo, push, merge ou mudança de produção nesta classificação.

## Fonte do teste de fronteira (somente composição sobre PR1)

Este teste requer a implementação de isolamento do PR1 e não pertence à suíte da base legada. Executado no worktree de composição com o runner canônico; não substitui os 19 cenários positivos.

```python
"""PR1 compatibility boundary: refused claim must never reach the queue.

This proves rejection only, not execution support. The original 19 failing
positive scenarios remain recorded and are not skipped or rewritten as success.
"""

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_worker_bridge import Bridge


class ForbiddenQueue:
    """Tripwire, never a fake backend or an assertion of isolation."""

    def read(self, *_):
        raise AssertionError("Refused task reached queue read")

    def admit(self, *_):
        raise AssertionError("Refused task reached queue admission")


@pytest.mark.parametrize(
    "contract,policy", [("local-only", None), (None, "natural-local-only")]
)
def test_real_restricted_claim_never_delegates(tmp_path, monkeypatch, contract, policy):
    home = tmp_path / "synthetic-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="synthetic restricted task",
            completion_contract=contract,
            isolation_policy=policy,
        )
        before = kb.get_task(conn, task_id)
        assert before.status == "ready"
        assert kb.claim_task(conn, task_id, claimer="synthetic-coordinator") is None
        task = kb.get_task(conn, task_id)
        assert task.status == before.status and task.current_run_id is None
        assert (
            conn.execute(
                "SELECT count(*) FROM task_runs WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            == 0
        )
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='claim_rejected' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert json.loads(event[0])["reason"] == "local_only_completion_contract"
        bridge = Bridge(
            kb,
            conn,
            ForbiddenQueue(),
            lambda *_: pytest.fail("Verifier reached without run"),
        )
        with pytest.raises(ValueError, match="STALE_RUN"):
            bridge.admit(task_id, None, "a" * 40, ["content"], time.time() + 60, {})
        assert conn.execute("SELECT count(*) FROM synthetic_bridge").fetchone()[0] == 0
        assert kb.get_task(conn, task_id).current_run_id is None

```
